"""Orpheus DLNA — discover LAN renderers (Samsung Music Frame speaks
DLNA/UPnP when on the same network) and push a stream URL to them.

Pure-stdlib SSDP + SOAP. If the Frame is discovered, it appears as a
Constellation node of kind 'dlna' and Orpheus can hand it a stream URL
(our /api/stream proxy or a live radio URL).
"""
from __future__ import annotations

import asyncio
import re
import socket
from typing import Optional
from urllib.parse import urljoin
from xml.etree import ElementTree as ET

import httpx
from fastapi import APIRouter, HTTPException

from .constellation import VirtualNode, hub
from .guest import lan_ip

router = APIRouter(prefix="/api")

_renderers: dict[str, dict] = {}  # location -> info

SSDP_ADDR = ("239.255.255.250", 1900)
MSEARCH = (
    "M-SEARCH * HTTP/1.1\r\n"
    f"HOST: {SSDP_ADDR[0]}:{SSDP_ADDR[1]}\r\n"
    'MAN: "ssdp:discover"\r\n'
    "MX: 2\r\n"
    "ST: urn:schemas-upnp-org:device:MediaRenderer:1\r\n\r\n"
).encode()

_NS = {
    "d": "urn:schemas-upnp-org:device-1-0",
}


def _ssdp_scan(timeout: float = 3.0) -> list[str]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    locations: set[str] = set()
    try:
        for _ in range(2):
            sock.sendto(MSEARCH, SSDP_ADDR)
        while True:
            try:
                data, _addr = sock.recvfrom(8192)
            except socket.timeout:
                break
            m = re.search(rb"(?im)^LOCATION:\s*(\S+)", data)
            if m:
                locations.add(m.group(1).decode())
    finally:
        sock.close()
    return sorted(locations)


def _ssdp_unicast(ip: str, timeout: float = 2.0) -> list[str]:
    """Unicast M-SEARCH straight at a device — dodges multicast filtering,
    which is exactly how the Frame hides from naive scans on Windows."""
    msg = MSEARCH.replace(b"ST: urn:schemas-upnp-org:device:MediaRenderer:1",
                          b"ST: ssdp:all")
    locations: set[str] = set()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(msg, (ip, 1900))
        while True:
            try:
                data, _ = sock.recvfrom(8192)
            except socket.timeout:
                break
            m = re.search(rb"(?im)^LOCATION:\s*(\S+)", data)
            if m:
                locations.add(m.group(1).decode())
    except OSError:
        pass
    finally:
        sock.close()
    return sorted(locations)


# Samsung speakers/frames publish their DMR description at these well-known spots
_SAMSUNG_PROBE_PORTS = [(9197, "/dmr"), (7676, "/smp_2_"), (8001, "/api/v2/"), (56001, "/dmr")]

_MDNS_SERVICES = [
    "_spotify-connect._tcp.local.",   # how Spotify sees the Frame
    "_airplay._tcp.local.",
    "_raop._tcp.local.",
    "_googlecast._tcp.local.",
]


def _mdns_scan(timeout: float = 4.0) -> list[dict]:
    """Find WiFi audio devices the way Spotify does: mDNS service browse."""
    try:
        from zeroconf import ServiceBrowser, ServiceListener, Zeroconf
    except ImportError:
        return []

    found: dict[str, dict] = {}

    class L(ServiceListener):
        def add_service(self, zc, stype, name):
            try:
                info = zc.get_service_info(stype, name, timeout=1500)
                if info and info.addresses:
                    ip = socket.inet_ntoa(info.addresses[0])
                    label = name.split("._")[0].replace("-", " ").strip()
                    entry = found.setdefault(ip, {"ip": ip, "names": set(), "services": set()})
                    entry["names"].add(label)
                    entry["services"].add(stype.split(".")[0].lstrip("_"))
            except Exception:
                pass
        def update_service(self, zc, stype, name): ...
        def remove_service(self, zc, stype, name): ...

    zc = Zeroconf()
    try:
        listener = L()
        for svc in _MDNS_SERVICES:
            ServiceBrowser(zc, svc, listener)
        import time as _t
        _t.sleep(timeout)
    finally:
        zc.close()
    return [
        {"ip": v["ip"], "name": sorted(v["names"])[0] if v["names"] else v["ip"],
         "services": sorted(v["services"])}
        for v in found.values()
    ]


async def _describe(location: str) -> Optional[dict]:
    try:
        async with httpx.AsyncClient(timeout=6.0) as c:
            r = await c.get(location)
            r.raise_for_status()
        root = ET.fromstring(r.text)
        dev = root.find(".//d:device", _NS)
        if dev is None:
            return None
        name = dev.findtext("d:friendlyName", "?", _NS)
        maker = dev.findtext("d:manufacturer", "", _NS)
        # find AVTransport control URL
        ctrl = None
        for svc in dev.findall(".//d:service", _NS):
            stype = svc.findtext("d:serviceType", "", _NS)
            if "AVTransport" in stype:
                ctrl = urljoin(location, svc.findtext("d:controlURL", "", _NS))
                break
        if not ctrl:
            return None
        return {"location": location, "name": name, "maker": maker, "control": ctrl}
    except Exception:
        return None


async def _probe_ip(ip: str) -> list[dict]:
    """All the ways to coax a description doc out of one device IP."""
    infos = []
    # 1. unicast SSDP (works when multicast is filtered)
    for loc in await asyncio.to_thread(_ssdp_unicast, ip):
        info = await _describe(loc)
        if info:
            infos.append(info)
    # 2. Samsung well-known description endpoints
    if not infos:
        for port, path in _SAMSUNG_PROBE_PORTS:
            info = await _describe(f"http://{ip}:{port}{path}")
            if info:
                infos.append(info)
                break
    return infos


def _register(info: dict) -> None:
    _renderers[info["location"]] = info
    node_id = "dlna_" + re.sub(r"\W", "", info["location"])[-12:]
    if node_id not in hub.virtual:
        hub.virtual[node_id] = VirtualNode(node_id, info["name"], "dlna", info)


@router.get("/dlna/scan")
async def dlna_scan():
    """Three-pronged hunt: multicast SSDP + mDNS (Spotify-style) + targeted probes."""
    found: list[dict] = []
    seen_locations: set[str] = set()

    # strategy 1: classic multicast SSDP
    for loc in await asyncio.to_thread(_ssdp_scan):
        info = await _describe(loc)
        if info and info["location"] not in seen_locations:
            seen_locations.add(info["location"])
            found.append(info)

    # strategy 2: mDNS browse (how Spotify Connect discovers the Frame),
    # then hit each discovered IP with unicast SSDP + Samsung port probes
    mdns_devices = await asyncio.to_thread(_mdns_scan)
    for dev in mdns_devices:
        for info in await _probe_ip(dev["ip"]):
            if info["location"] not in seen_locations:
                seen_locations.add(info["location"])
                info["via"] = f"mdns:{','.join(dev['services'])}"
                found.append(info)

    for info in found:
        _register(info)
    if found:
        await hub._nodes_changed()
    return {
        "renderers": found,
        "mdns_seen": mdns_devices,  # even non-DLNA devices, so the UI can explain
        "nodes": [v.info() for v in hub.virtual.values()],
    }


@router.post("/dlna/add")
async def dlna_add(payload: dict):
    """Manual add by IP — for when every discovery protocol is filtered.
    payload: {ip: '192.168.100.x'}"""
    ip = (payload.get("ip") or "").strip()
    if not re.match(r"^\d+\.\d+\.\d+\.\d+$", ip):
        raise HTTPException(400, "give me a plain IPv4 like 192.168.100.50")
    infos = await _probe_ip(ip)
    if not infos:
        raise HTTPException(
            404,
            "no UPnP renderer answered at that IP — the Frame may only expose "
            "Spotify Connect/AirPlay (both closed to third parties); check that "
            "'IP Remote' / network standby is enabled in its settings",
        )
    for info in infos:
        _register(info)
    await hub._nodes_changed()
    return {"ok": True, "renderers": infos}


# ---- server-side driver: when a DLNA node is active, Orpheus pushes to it ----
_last_pushed: dict = {"key": None}


async def _drive_dlna(action: str, state: dict) -> None:
    active = state.get("active_node") or ""
    vnode = hub.virtual.get(active)
    if not vnode:
        return
    info = vnode.meta
    if action in ("play_track", "next", "prev", "station"):
        if state.get("station"):
            # push through our proxy so the renderer gets a clean LAN URL
            from urllib.parse import quote
            url = f"http://{lan_ip()}:7777/api/stations/listen?url={quote(state['station']['url'], safe='')}"
            title = state["station"]["name"]
        elif state.get("track"):
            url = f"http://{lan_ip()}:7777/api/stream/{state['track']['id']}"
            title = state["track"]["title"]
        else:
            return
        _ = title  # kept for DIDL metadata (future: send to renderer)
        key = (active, url)
        if _last_pushed["key"] == key:
            return
        _last_pushed["key"] = key
        try:
            await _soap(
                info["control"], "SetAVTransportURI",
                f"<CurrentURI>{url}</CurrentURI><CurrentURIMetaData></CurrentURIMetaData>",
            )
            await _soap(info["control"], "Play", "<Speed>1</Speed>")
        except Exception:
            _last_pushed["key"] = None
    elif action == "toggle_play":
        try:
            if state["playing"]:
                await _soap(info["control"], "Play", "<Speed>1</Speed>")
            else:
                await _soap(info["control"], "Pause")
        except Exception:
            pass
    elif action == "set_active" and not hub.virtual.get(active):
        _last_pushed["key"] = None


hub.on_change.append(_drive_dlna)


_SOAP_PLAY = """<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"
 s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
 <s:Body>
  <u:{action} xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">
   <InstanceID>0</InstanceID>{body}
  </u:{action}>
 </s:Body>
</s:Envelope>"""


async def _soap(control: str, action: str, body: str = "") -> None:
    envelope = _SOAP_PLAY.format(action=action, body=body)
    headers = {
        "Content-Type": 'text/xml; charset="utf-8"',
        "SOAPACTION": f'"urn:schemas-upnp-org:service:AVTransport:1#{action}"',
    }
    async with httpx.AsyncClient(timeout=10.0) as c:
        r = await c.post(control, content=envelope, headers=headers)
        r.raise_for_status()


@router.post("/dlna/play")
async def dlna_play(payload: dict):
    """payload: {location, url, title?}  — push URL to renderer and hit Play."""
    info = _renderers.get(payload.get("location", ""))
    if not info:
        raise HTTPException(404, "renderer not known — scan first")
    url = payload.get("url")
    if not url:
        raise HTTPException(400, "url required")
    title = (payload.get("title") or "Orpheus").replace("&", "&amp;")
    meta = (
        '&lt;DIDL-Lite xmlns=&quot;urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/&quot; '
        'xmlns:dc=&quot;http://purl.org/dc/elements/1.1/&quot;&gt;'
        '&lt;item id=&quot;0&quot; parentID=&quot;-1&quot; restricted=&quot;1&quot;&gt;'
        f'&lt;dc:title&gt;{title}&lt;/dc:title&gt;'
        '&lt;/item&gt;&lt;/DIDL-Lite&gt;'
    )
    await _soap(
        info["control"], "SetAVTransportURI",
        f"<CurrentURI>{url}</CurrentURI><CurrentURIMetaData>{meta}</CurrentURIMetaData>",
    )
    await _soap(info["control"], "Play", "<Speed>1</Speed>")
    return {"ok": True, "renderer": info["name"]}


@router.post("/dlna/stop")
async def dlna_stop(payload: dict):
    info = _renderers.get(payload.get("location", ""))
    if not info:
        raise HTTPException(404, "renderer not known")
    await _soap(info["control"], "Stop")
    return {"ok": True}

"""Orpheus Party Engine — true multi-room sync via Snapcast.

Architecture (replaces the old browser-drift + Spotify Connect approach):

    deck track/station ──ffmpeg──▶ PCM 48k/16 ──▶ snapserver TCP source
                                                     │ timestamped chunks
                     ┌───────────────────────────────┼──────────────────┐
                snapweb (browser)              snapclient (PC)    snapclient (…)
                ~±50ms via wasm                ~±1ms native

- snapserver runs in Docker (see party/docker-compose.yml), Orpheus feeds it
  one PCM stream over TCP and controls it via JSON-RPC (port 1780 HTTP).
- Browser nodes open Snapweb (bundled with snapserver) — one tap, synced.
- The PC (Edifier out) runs the native snapclient for ~1ms alignment.
- Echo/Frame stay OUT of the sample-sync domain (cloud hardware can't be
  clock-driven); they remain normal single-target nodes outside party mode.

Feeder: ffmpeg pulls the SAME resolved stream the deck plays (vault file if
archived, else the googlevideo URL), decodes to raw PCM, pipes to snapserver.
Track changes = kill ffmpeg, start next. Radio stations work identically.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any, Optional

import httpx
from fastapi import APIRouter, HTTPException

from .constellation import hub
from .music import resolve_stream
from .vault import vault_path

router = APIRouter(prefix="/api")

SNAP_HOST = "127.0.0.1"
SNAP_TCP = 4953          # snapserver pcm source (tcp://)
SNAP_RPC = f"http://{SNAP_HOST}:1780/jsonrpc"
SNAP_STREAM_PORT = 1780  # snapweb + websocket stream for browser clients
SNAPCLIENT_EXE = Path(__file__).resolve().parent.parent / "party" / "snapclient" / "snapclient.exe"

_feed: dict[str, Any] = {"proc": None, "key": None, "task": None}
# local snapclient instances we spawned: hostID -> process
_outputs: dict[str, asyncio.subprocess.Process] = {}


def cleanup_orphans() -> None:
    """Kill stray ffmpeg feeders / spawned snapclients from a crashed run.

    ffmpeg: only processes whose command line points at OUR snapserver port.
    snapclient: only instances WE spawned (hostID PC-*) — a user-launched
    client like EDIFIER-PC is never touched.
    """
    import subprocess
    for pname, needle in (("ffmpeg.exe", str(SNAP_TCP)), ("snapclient.exe", "--hostID PC-")):
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"Get-CimInstance Win32_Process -Filter \"name='{pname}'\" | "
                 f"Where-Object {{ $_.CommandLine -match [regex]::Escape('{needle}') }} | "
                 f"ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force; $_.ProcessId }}"],
                capture_output=True, text=True, timeout=15)
            pids = out.stdout.split()
            if pids:
                print(f"[party] cleaned up orphaned {pname}: {', '.join(pids)}")
        except Exception:
            pass


# ------------------------------------------------------------------ feeder
# One ffmpeg, FOUR outputs from the same decode clock (so they never drift):
#   FULL  4953  whole mix          THUMP 4954  lowpass  160 Hz
#   HEART 4955  160 Hz – 4 kHz     AIR   4956  highpass 4 kHz
# Each snapcast client (star) subscribes to exactly one of these bands —
# that's how a speaker takes a ROLE in the constellation.
BANDS = {
    "FULL":  (4953, "anull"),
    "THUMP": (4954, "lowpass=f=160,volume=1.6"),
    "HEART": (4955, "highpass=f=160,lowpass=f=4000"),
    "AIR":   (4956, "highpass=f=4000,volume=1.4"),
}


async def _spawn_ffmpeg(src: str, is_url: bool, start_s: float = 0.0) -> asyncio.subprocess.Process:
    args = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
    if is_url:
        args += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "4"]
    if start_s > 0.5:
        args += ["-ss", f"{start_s:.2f}"]
    args += ["-re", "-i", src]
    # split the decoded audio into one branch per band
    n = len(BANDS)
    labels = "".join(f"[b{i}]" for i in range(n))
    chains = [f"[0:a]asplit={n}{labels}"]
    for i, (_, (_, filt)) in enumerate(BANDS.items()):
        chains.append(f"[b{i}]{filt}[o{i}]")
    args += ["-filter_complex", ";".join(chains)]
    for i, (_, (port, _)) in enumerate(BANDS.items()):
        args += ["-map", f"[o{i}]",
                 "-f", "s16le", "-acodec", "pcm_s16le", "-ac", "2", "-ar", "48000",
                 f"tcp://{SNAP_HOST}:{port}"]
    return await asyncio.create_subprocess_exec(*args,
                                                stdout=asyncio.subprocess.DEVNULL,
                                                stderr=asyncio.subprocess.PIPE)


async def _stop_feed() -> None:
    p = _feed.get("proc")
    _feed.update(proc=None, key=None)
    if p and p.returncode is None:
        p.terminate()
        try:
            await asyncio.wait_for(p.wait(), 3)
        except asyncio.TimeoutError:
            p.kill()


async def _start_feed_for_state(state: dict, force: bool = False) -> Optional[str]:
    """Point the feeder at whatever the deck should be sounding."""
    start_s = 0.0
    if state.get("station"):
        src, key, is_url = state["station"]["url"], f"st:{state['station']['url']}", True
    elif state.get("track") and state["track"].get("id"):
        vid = state["track"]["id"]
        local = vault_path(vid)
        if local:
            src, is_url = str(local), False
        else:
            src, is_url = await resolve_stream(vid), True
        key = f"tr:{vid}"
        # resume from the deck's current position (seek / mid-track party flip)
        if state.get("playing"):
            start_s = state["position_s"] + (time.time() * 1000 - state["epoch_ms"]) / 1000
        else:
            start_s = state["position_s"]
    else:
        await _stop_feed()
        return None

    if not force and _feed.get("key") == key and _feed.get("proc") and _feed["proc"].returncode is None:
        return key  # already feeding this
    await _stop_feed()
    _feed["proc"] = await _spawn_ffmpeg(src, is_url, start_s)
    _feed["key"] = key
    return key


# ------------------------------------------------------------------ local outputs
# Every audio device on THIS PC (bluetooth speakers, wired, HDMI) can join
# the party as its own snapclient instance — same mechanism for all of them:
# snapserver chunks -> snapclient pinned to that output device. This is how
# the Music Frame joins too (paired to the PC over bluetooth), because on
# WiFi it only exposes Spotify Connect (verified by port/mDNS census) and
# its licensed AirPlay stack is closed.

_SKIP_DEVICES = re.compile(r"steam streaming|virtual|vb-audio|voicemeeter", re.I)


async def _list_audio_devices() -> list[dict]:
    """Parse `snapclient -l` into [{idx, name, joined}]."""
    if not SNAPCLIENT_EXE.exists():
        raise HTTPException(500, "snapclient.exe missing — party/snapclient/")
    p = await asyncio.create_subprocess_exec(
        str(SNAPCLIENT_EXE), "-l",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    out, _ = await asyncio.wait_for(p.communicate(), 15)
    devices = []
    for m in re.finditer(r"^(\d+):\s*(\S.*?)\r?\n(.+?)\r?$", out.decode(errors="replace"), re.M):
        idx, name = int(m.group(1)), m.group(3).strip()
        if idx == 0:  # "default" duplicate of some real device
            continue
        devices.append({
            "idx": idx,
            "name": name,
            "skip_hint": bool(_SKIP_DEVICES.search(name)),
            "joined": _output_id(name) in _outputs and _outputs[_output_id(name)].returncode is None,
        })
    return devices


def _output_id(device_name: str) -> str:
    """Stable snapcast hostID for a local output device."""
    slug = re.sub(r"[^A-Za-z0-9]+", "-", device_name).strip("-").upper()[:24]
    return f"PC-{slug}"


@router.get("/party/outputs")
async def party_outputs():
    """Audio devices on the server PC (BT speakers show up when connected)."""
    return {"devices": await _list_audio_devices()}


def _pretty_device_name(raw: str) -> str:
    """'Speakers (Music Frame)' -> 'Music Frame'."""
    m = re.search(r"\(([^)]+)\)", raw)
    inner = (m.group(1).strip() if m else "").strip()
    outer = raw.split("(")[0].strip()
    # the parenthetical is the actual device for BT/USB gear; generic terms stay outside
    if inner and not re.fullmatch(r"high definition audio( device)?|realtek.*|nvidia.*", inner, re.I):
        return inner[:32]
    return (outer or raw)[:32]


@router.post("/party/outputs/join")
async def party_output_join(payload: dict):
    idx, name = int(payload["idx"]), str(payload["name"])
    oid = _output_id(name)
    old = _outputs.get(oid)
    if old and old.returncode is None:
        return {"ok": True, "id": oid, "note": "already joined"}
    p = await asyncio.create_subprocess_exec(
        str(SNAPCLIENT_EXE), "-h", SNAP_HOST, "-s", str(idx), "--hostID", oid,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    _outputs[oid] = p
    await asyncio.sleep(1.2)
    if p.returncode is not None:
        _outputs.pop(oid, None)
        raise HTTPException(502, f"snapclient exited immediately for '{name}' — device busy?")
    # readable name in the client list (default is the PC hostname for all)
    try:
        await _rpc("Client.SetName", {"id": oid, "name": _pretty_device_name(name) or oid})
    except HTTPException:
        pass  # cosmetic only
    return {"ok": True, "id": oid}


@router.post("/party/outputs/leave")
async def party_output_leave(payload: dict):
    oid = _output_id(str(payload["name"]))
    p = _outputs.pop(oid, None)
    if p and p.returncode is None:
        p.terminate()
        try:
            await asyncio.wait_for(p.wait(), 3)
        except asyncio.TimeoutError:
            p.kill()
    return {"ok": True}


# ------------------------------------------------------------------ rpc
async def _rpc(method: str, params: Optional[dict] = None) -> Any:
    payload = {"id": int(time.time() * 1000) % 100000, "jsonrpc": "2.0", "method": method}
    if params:
        payload["params"] = params
    try:
        # NB: snapserver's embedded HTTP server mishandles keep-alive reuse
        # ("bad method"/"partial message" errors) — always close connections.
        async with httpx.AsyncClient(timeout=6.0, headers={"Connection": "close"}) as c:
            r = await c.post(SNAP_RPC, json=payload)
            r.raise_for_status()
            data = r.json()
    except (httpx.HTTPError, json.JSONDecodeError) as e:
        raise HTTPException(503, f"snapserver unreachable — is the party container up? ({e})") from e
    if "error" in data:
        raise HTTPException(502, f"snapserver: {data['error']}")
    return data.get("result")


# ------------------------------------------------------------------ api
@router.get("/party/status")
async def party_status() -> dict:
    """Snapserver health + connected snap clients (+ band role per client)."""
    try:
        st = await _rpc("Server.GetStatus")
    except HTTPException:
        return {"up": False, "clients": [], "feeding": bool(_feed.get("proc"))}
    clients = []
    for g in st["server"].get("groups", []):
        for c in g.get("clients", []):
            name = c["config"]["name"] or c["host"]["name"]
            # PC-* = an output we spawned; the ID carries the device name and
            # survives snapserver restarts (config names don't, unless saved)
            if c["id"].startswith("PC-") and (not c["config"]["name"] or c["config"]["name"] == c["host"]["name"]):
                name = c["id"][3:].replace("-", " ").title()
            clients.append({
                "id": c["id"],
                "name": name,
                "connected": c["connected"],
                "volume": c["config"]["volume"]["percent"],
                "muted": c["config"]["volume"]["muted"],
                "latency_ms": c["config"]["latency"],
                "os": c["host"]["os"],
                "group": g["id"],
                "band": g.get("stream_id") or "FULL",
            })
    return {
        "up": True,
        "clients": clients,
        "bands": list(BANDS.keys()),
        "feeding": bool(_feed.get("proc") and _feed["proc"].returncode is None),
    }


@router.post("/party/client/{cid}/band")
async def party_client_band(cid: str, payload: dict):
    """Give a star a ROLE: subscribe it to FULL / THUMP / HEART / AIR.

    Snapcast subscribes GROUPS to streams, so the client must sit alone in
    its group first (isolate), then the group is pointed at the band.
    """
    band = str(payload.get("band", "FULL")).upper()
    if band not in BANDS:
        raise HTTPException(400, f"unknown band {band!r} — one of {list(BANDS)}")
    st = await _rpc("Server.GetStatus")
    groups = st["server"].get("groups", [])
    mine = next((g for g in groups if any(c["id"] == cid for c in g["clients"])), None)
    if mine is None:
        raise HTTPException(404, f"client {cid!r} not found")
    if len(mine["clients"]) > 1:
        # isolate: shrink the shared group to everyone-but-us; snapserver
        # re-homes us into a fresh group automatically
        others = [c["id"] for c in mine["clients"] if c["id"] != cid]
        await _rpc("Group.SetClients", {"id": mine["id"], "clients": others})
        st = await _rpc("Server.GetStatus")
        mine = next((g for g in st["server"].get("groups", [])
                     if any(c["id"] == cid for c in g["clients"])), None)
        if mine is None:
            raise HTTPException(500, "client vanished during group isolate")
    await _rpc("Group.SetStream", {"id": mine["id"], "stream_id": band})
    return {"ok": True, "band": band}


@router.post("/party/client/{cid}/volume")
async def party_client_volume(cid: str, payload: dict):
    pct = max(0, min(100, int(payload.get("percent", 100))))
    await _rpc("Client.SetVolume", {"id": cid, "volume": {"percent": pct, "muted": False}})
    return {"ok": True}


@router.post("/party/client/{cid}/latency")
async def party_client_latency(cid: str, payload: dict):
    ms = max(-2000, min(2000, int(payload.get("ms", 0))))
    await _rpc("Client.SetLatency", {"id": cid, "latency": ms})
    return {"ok": True}


@router.post("/party/client/{cid}/name")
async def party_client_name(cid: str, payload: dict):
    await _rpc("Client.SetName", {"id": cid, "name": (payload.get("name") or "node")[:32]})
    return {"ok": True}


# ------------------------------------------------------------------ hub hook
async def _drive_party(action: str, state: dict) -> None:
    """Keep the snapcast feed following the deck while party mode is on."""
    if not state.get("party"):
        if _feed.get("proc"):
            await _stop_feed()
        return
    if action in ("play_track", "next", "prev", "station", "party"):
        try:
            await _start_feed_for_state(state)
        except Exception:
            pass  # resolution failure surfaces via deck playback too
    elif action == "seek":
        try:
            await _start_feed_for_state(state, force=True)  # same track, new position
        except Exception:
            pass
    elif action == "toggle_play":
        # snapcast has no pause; stop/restart the feed (clients fade in sync)
        if state.get("playing"):
            try:
                await _start_feed_for_state(state)
            except Exception:
                pass
        else:
            await _stop_feed()


hub.on_change.append(_drive_party)

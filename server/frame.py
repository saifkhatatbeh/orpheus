"""Orpheus ⇄ Samsung Music Frame via Spotify Connect.

Evidence from this LAN: the Frame exposes NO open DLNA/UPnP ports — it
announces only `_spotify-connect._tcp` over mDNS. That's how the Spotify
app drives it: discovery via mDNS, but playback control goes through the
Spotify Connect *cloud* API. So we do the same.

Requires the Spotify source to be linked (sp_dc cookie → web token,
already handled in sources.py). The web-player token carries the
`streaming` scope, so /me/player endpoints work with Premium.

The Frame registers as a Constellation node (kind='spotify'). When it's
the active node, Orpheus mirrors the current deck track onto it by
searching Spotify's catalog and starting playback on that device.
"""
from __future__ import annotations

import time
from typing import Any, Optional

import httpx
from fastapi import APIRouter, HTTPException

from .constellation import VirtualNode, hub
from .sources import _spotify_token
from .store import setting

router = APIRouter(prefix="/api")

_token_cache: dict = {"tok": None, "exp": 0}


async def _token() -> str:
    if not setting("spotify_refresh_token"):
        raise HTTPException(428, "link Spotify first (Library → Connect Sources)")
    if _token_cache["tok"] and _token_cache["exp"] > time.time() + 60:
        return _token_cache["tok"]
    tok = await _spotify_token()
    _token_cache.update(tok=tok, exp=time.time() + 45 * 60)
    return tok


async def _sp(method: str, path: str, **kw) -> httpx.Response:
    tok = await _token()
    async with httpx.AsyncClient(timeout=12.0) as c:
        r = await c.request(method, f"https://api.spotify.com/v1{path}",
                            headers={"Authorization": f"Bearer {tok}"}, **kw)
    if r.status_code == 401:
        _token_cache["tok"] = None
        raise HTTPException(401, "spotify token expired — re-link sp_dc")
    return r


@router.get("/frame/devices")
async def frame_devices() -> dict[str, Any]:
    """List Spotify Connect devices and register them as Constellation nodes."""
    r = await _sp("GET", "/me/player/devices")
    r.raise_for_status()
    devices = r.json().get("devices", [])
    for d in devices:
        node_id = "sp_" + d["id"][:12]
        if node_id not in hub.virtual:
            hub.virtual[node_id] = VirtualNode(
                node_id, d.get("name", "Spotify device"), "spotify",
                {"device_id": d["id"], "type": d.get("type")},
            )
        else:
            hub.virtual[node_id].meta["device_id"] = d["id"]
    if devices:
        await hub._nodes_changed()
    return {"devices": [
        {"name": d.get("name"), "type": d.get("type"), "id": d.get("id"),
         "active": d.get("is_active"), "volume": d.get("volume_percent")}
        for d in devices
    ]}


async def _search_uri(track: dict) -> Optional[str]:
    title = (track.get("title") or "").strip()
    artist = (track.get("artist") or "").split(",")[0].strip()
    if not title:
        return None
    q = f"track:{title} artist:{artist}" if artist else title
    r = await _sp("GET", "/search", params={"q": q, "type": "track", "limit": 1})
    items = (r.json().get("tracks") or {}).get("items") or []
    if not items:  # loosen
        r = await _sp("GET", "/search", params={"q": f"{title} {artist}", "type": "track", "limit": 1})
        items = (r.json().get("tracks") or {}).get("items") or []
    return items[0]["uri"] if items else None


async def _play_on(device_id: str, uri: str) -> None:
    r = await _sp("PUT", "/me/player/play", params={"device_id": device_id},
                  json={"uris": [uri]})
    if r.status_code == 404:
        # device asleep — poke it with a transfer first
        await _sp("PUT", "/me/player", json={"device_ids": [device_id], "play": False})
        r = await _sp("PUT", "/me/player/play", params={"device_id": device_id},
                      json={"uris": [uri]})
    if r.status_code >= 400:
        raise HTTPException(502, f"spotify refused playback ({r.status_code}): {r.text[:120]}")


# ---- driver: mirror deck actions onto Connect devices ----
# HARD RULE: drive exactly ONE Connect device at a time. Spotify allows a
# single playback session per account — pushing play to two devices makes
# them steal the session from each other (the "PC → Echo → Frame 20s later"
# cascade). For multi-speaker cloud sync, the user makes an Alexa speaker
# group (Echo + Frame both speak Alexa) — it appears in Connect as ONE
# device and Amazon sample-syncs its members in hardware.
_last: dict[str, tuple] = {}  # device_id -> (track_id,) dedupe


def _target_device(state: dict) -> Optional[str]:
    """The single Connect device to drive for the current state.

    Party mode is now owned by the Snapcast engine (server/party.py) —
    Connect devices are cloud-buffered and can't join a sample-locked
    domain, so in party mode we drive NONE of them.
    """
    if state.get("party"):
        return None
    active = state.get("active_node") or ""
    vnode = hub.virtual.get(active)
    if vnode and vnode.kind == "spotify":
        return vnode.meta.get("device_id")
    return None


async def _drive_spotify(action: str, state: dict) -> None:
    if action == "party":
        _last.clear()  # force a re-push when party flips
    dev = _target_device(state)
    if not dev:
        return
    try:
        if action in ("play_track", "next", "prev", "set_active", "party", "party_device") and state.get("track"):
            tid = state["track"].get("id")
            if _last.get(dev) == (tid,):
                return
            uri = await _search_uri(state["track"])
            if uri:
                await _play_on(dev, uri)
                _last[dev] = (tid,)
        elif action == "toggle_play":
            await _sp("PUT", "/me/player/pause" if not state["playing"] else "/me/player/play",
                      params={"device_id": dev})
        elif action == "volume":
            await _sp("PUT", "/me/player/volume",
                      params={"volume_percent": int(state["volume"] * 100), "device_id": dev})
    except HTTPException:
        pass  # linked-account issues surface via the devices endpoint instead


hub.on_change.append(_drive_spotify)

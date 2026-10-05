"""Orpheus music engine — YouTube Music catalog + stream resolution/proxy.

ytmusicapi: free, keyless search/metadata/radio.
yt-dlp: resolves a videoId to a direct audio URL, which we proxy with
Range support so any browser on the LAN can seek. Resolved URLs are
IP-locked by Google to *this* machine, hence the proxy (phones/iPads
stream through us).
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from ytmusicapi import YTMusic

router = APIRouter(prefix="/api")

_yt = YTMusic()  # anonymous, no auth needed
_resolve_cache: dict[str, tuple[float, str]] = {}  # videoId -> (expiry, url)
_resolve_locks: dict[str, asyncio.Lock] = {}
_client = httpx.AsyncClient(timeout=30.0, follow_redirects=True)

RESOLVE_TTL = 60 * 40  # google URLs live ~6h; refresh well before


def _thumb(item: dict, size: int = 544) -> Optional[str]:
    # ytmusicapi is inconsistent: search results use "thumbnails",
    # watch-playlist (song radio) tracks use "thumbnail" (both are lists).
    thumbs = item.get("thumbnails") or item.get("thumbnail") or []
    if not thumbs:
        return None
    url = thumbs[-1].get("url", "")
    # ytimg thumbs encode size as =w544-h544; upgrade small ones
    if "googleusercontent" in url and "=w" in url:
        url = url.split("=w")[0] + f"=w{size}-h{size}-l90-rj"
    return url


def _artists(item: dict) -> str:
    return ", ".join(a["name"] for a in (item.get("artists") or []) if a.get("name")) or (
        item.get("author") or ""
    )


def _norm_song(item: dict) -> dict:
    vid = item.get("videoId")
    return {
        "id": vid,
        "title": item.get("title"),
        "artist": _artists(item),
        "album": (item.get("album") or {}).get("name") if isinstance(item.get("album"), dict) else None,
        "duration": item.get("duration_seconds") or item.get("lengthSeconds"),
        "art": _thumb(item) or (f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg" if vid else None),
        "explicit": bool(item.get("isExplicit")),
        "source": "ytmusic",
    }


@router.get("/search")
async def search(q: str, kind: str = "songs", limit: int = 24) -> dict[str, Any]:
    if not q.strip():
        return {"results": []}
    valid = {"songs", "albums", "artists", "playlists", "videos"}
    if kind not in valid:
        raise HTTPException(400, f"kind must be one of {sorted(valid)}")
    raw = await asyncio.to_thread(_yt.search, q, filter=kind, limit=limit)
    results = []
    for item in raw:
        rt = item.get("resultType")
        if rt in ("song", "video"):
            s = _norm_song(item)
            if s["id"]:
                results.append(s)
        elif rt == "album":
            results.append({
                "kind": "album",
                "browseId": item.get("browseId"),
                "title": item.get("title"),
                "artist": _artists(item),
                "year": item.get("year"),
                "art": _thumb(item),
            })
        elif rt == "artist":
            results.append({
                "kind": "artist",
                "browseId": item.get("browseId"),
                "name": item.get("artist") or item.get("title"),
                "art": _thumb(item),
            })
        elif rt == "playlist":
            results.append({
                "kind": "playlist",
                "browseId": item.get("browseId"),
                "title": item.get("title"),
                "art": _thumb(item),
            })
    return {"results": results}


@router.get("/album/{browse_id}")
async def album(browse_id: str) -> dict[str, Any]:
    data = await asyncio.to_thread(_yt.get_album, browse_id)
    tracks = []
    for t in data.get("tracks", []):
        s = _norm_song(t)
        s["art"] = s["art"] or _thumb(data)
        s["album"] = s["album"] or data.get("title")
        if s["id"]:
            tracks.append(s)
    return {
        "title": data.get("title"),
        "artist": _artists(data),
        "year": data.get("year"),
        "art": _thumb(data),
        "tracks": tracks,
    }


@router.get("/artist/{browse_id}")
async def artist(browse_id: str) -> dict[str, Any]:
    data = await asyncio.to_thread(_yt.get_artist, browse_id)
    songs = [(_norm_song(t)) for t in (data.get("songs") or {}).get("results", [])]
    albums = [
        {
            "kind": "album",
            "browseId": a.get("browseId"),
            "title": a.get("title"),
            "year": a.get("year"),
            "art": _thumb(a),
        }
        for a in (data.get("albums") or {}).get("results", [])
    ]
    return {
        "name": data.get("name"),
        "description": data.get("description"),
        "art": _thumb(data, 880),
        "songs": [s for s in songs if s["id"]],
        "albums": albums,
    }


@router.get("/radio/{video_id}")
async def radio(video_id: str, limit: int = 25) -> dict[str, Any]:
    """'Song radio' — endless queue seeded from one track."""
    data = await asyncio.to_thread(
        _yt.get_watch_playlist, video_id, None, limit, True
    )
    tracks = []
    for t in data.get("tracks", []):
        vid = t.get("videoId")
        s = {
            "id": vid,
            "title": t.get("title"),
            "artist": ", ".join(a["name"] for a in (t.get("artists") or [])),
            "album": (t.get("album") or {}).get("name") if isinstance(t.get("album"), dict) else None,
            "duration": t.get("lengthSeconds") or _dur_to_s(t.get("length")),
            "art": _thumb(t) or (f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg" if vid else None),
            "source": "ytmusic",
        }
        if s["id"] and s["id"] != video_id:
            tracks.append(s)
    return {"tracks": tracks}


def _dur_to_s(length: Optional[str]) -> Optional[int]:
    if not length:
        return None
    try:
        parts = [int(p) for p in length.split(":")]
        s = 0
        for p in parts:
            s = s * 60 + p
        return s
    except ValueError:
        return None


@router.get("/home")
async def home() -> dict[str, Any]:
    """Landing shelves (charts / moods) for the Discover view."""
    try:
        raw = await asyncio.to_thread(_yt.get_home, 4)
    except Exception:
        return {"shelves": []}
    shelves = []
    for shelf in raw:
        items = []
        for it in shelf.get("contents", []):
            if it.get("videoId"):
                items.append(_norm_song(it))
            elif it.get("browseId") and (it.get("title") or it.get("name")):
                items.append({
                    "kind": "album" if str(it.get("browseId", "")).startswith("MPRE") else "playlist",
                    "browseId": it.get("browseId"),
                    "title": it.get("title") or it.get("name"),
                    "artist": _artists(it),
                    "art": _thumb(it),
                })
        if items:
            shelves.append({"title": shelf.get("title"), "items": items})
    return {"shelves": shelves}


async def resolve_stream(video_id: str) -> str:
    """videoId -> direct googlevideo audio URL, cached."""
    now = time.time()
    hit = _resolve_cache.get(video_id)
    if hit and hit[0] > now:
        return hit[1]
    lock = _resolve_locks.setdefault(video_id, asyncio.Lock())
    async with lock:
        hit = _resolve_cache.get(video_id)
        if hit and hit[0] > time.time():
            return hit[1]
        url = await asyncio.to_thread(_resolve_blocking, video_id)
        _resolve_cache[video_id] = (time.time() + RESOLVE_TTL, url)
        return url


def _resolve_blocking(video_id: str) -> str:
    import yt_dlp

    opts = {
        "format": "bestaudio[ext=m4a]/bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "extractor_args": {"youtube": {"player_client": ["android", "web"]}},
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"https://music.youtube.com/watch?v={video_id}", download=False)
    url = info.get("url")
    if not url:
        raise RuntimeError(f"no stream url for {video_id}")
    return url


@router.get("/stream/{video_id}")
async def stream(video_id: str, request: Request):
    """Range-aware audio proxy. Vault-first: if we've archived this track,
    serve it straight from disk (works offline, zero latency)."""
    from .vault import vault_path

    local = vault_path(video_id)
    if local:
        return FileResponse(local, media_type="audio/mp4")

    try:
        upstream_url = await resolve_stream(video_id)
    except Exception as e:  # resolution failed (age gate, region, etc.)
        raise HTTPException(502, f"resolve failed: {e}") from e

    headers = {}
    if rng := request.headers.get("range"):
        headers["Range"] = rng

    upstream = await _client.send(
        _client.build_request("GET", upstream_url, headers=headers), stream=True
    )
    if upstream.status_code >= 400:
        await upstream.aclose()
        # stale cached URL? retry once with fresh resolve
        _resolve_cache.pop(video_id, None)
        upstream_url = await resolve_stream(video_id)
        upstream = await _client.send(
            _client.build_request("GET", upstream_url, headers=headers), stream=True
        )
        if upstream.status_code >= 400:
            await upstream.aclose()
            raise HTTPException(502, "upstream refused stream")

    passthrough = {
        k: v
        for k, v in upstream.headers.items()
        if k.lower() in ("content-type", "content-length", "content-range", "accept-ranges")
    }
    passthrough.setdefault("Accept-Ranges", "bytes")
    passthrough["Cache-Control"] = "no-store"

    async def body():
        try:
            async for chunk in upstream.aiter_bytes(64 * 1024):
                yield chunk
        finally:
            await upstream.aclose()

    return StreamingResponse(body(), status_code=upstream.status_code, headers=passthrough)

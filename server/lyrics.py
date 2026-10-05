"""Orpheus lyrics — synced (LRC) lyrics via lrclib.net, free + keyless."""
from __future__ import annotations

import re
from typing import Optional

import httpx
from fastapi import APIRouter

router = APIRouter(prefix="/api")

_cache: dict[str, dict] = {}


def _parse_lrc(lrc: str) -> list[dict]:
    lines = []
    for raw in lrc.splitlines():
        for m in re.finditer(r"\[(\d+):(\d+(?:\.\d+)?)\]", raw):
            t = int(m.group(1)) * 60 + float(m.group(2))
            text = re.sub(r"\[\d+:\d+(?:\.\d+)?\]", "", raw).strip()
            lines.append({"t": t, "text": text})
    lines.sort(key=lambda x: x["t"])
    return lines


@router.get("/lyrics")
async def lyrics(title: str, artist: str = "", album: str = "", duration: Optional[int] = None):
    key = f"{title}|{artist}".lower()
    if key in _cache:
        return _cache[key]
    params = {"track_name": title, "artist_name": artist}
    if album:
        params["album_name"] = album
    if duration:
        params["duration"] = duration
    result = {"synced": None, "plain": None}
    try:
        async with httpx.AsyncClient(timeout=10.0, headers={"User-Agent": "Orpheus/1.0"}) as c:
            r = await c.get("https://lrclib.net/api/get", params=params)
            if r.status_code == 404:
                r = await c.get("https://lrclib.net/api/search",
                                params={"track_name": title, "artist_name": artist})
                items = r.json() if r.status_code == 200 else []
                data = items[0] if items else {}
            else:
                r.raise_for_status()
                data = r.json()
        if data.get("syncedLyrics"):
            result["synced"] = _parse_lrc(data["syncedLyrics"])
        if data.get("plainLyrics"):
            result["plain"] = data["plainLyrics"]
    except Exception:
        pass
    _cache[key] = result
    return result

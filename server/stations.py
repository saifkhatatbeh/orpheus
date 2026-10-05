"""Orpheus live radio — radio-browser.info (free, open, 30k+ stations)."""
from __future__ import annotations

import random
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

router = APIRouter(prefix="/api")

_MIRRORS = [
    "https://de1.api.radio-browser.info",
    "https://fi1.api.radio-browser.info",
    "https://at1.api.radio-browser.info",
]
_HEADERS = {"User-Agent": "Orpheus/1.0 (personal LAN music deck)"}


def _norm(st: dict) -> dict:
    return {
        "uuid": st.get("stationuuid"),
        "name": (st.get("name") or "").strip(),
        "url": st.get("url_resolved") or st.get("url"),
        "art": st.get("favicon") or None,
        "tags": (st.get("tags") or "")[:120],
        "country": st.get("country"),
        "codec": st.get("codec"),
        "bitrate": st.get("bitrate"),
        "source": "radio",
    }


async def _get(path: str, params: dict) -> Any:
    last = None
    mirrors = random.sample(_MIRRORS, len(_MIRRORS))
    async with httpx.AsyncClient(timeout=12.0, headers=_HEADERS) as c:
        for base in mirrors:
            try:
                r = await c.get(base + path, params=params)
                r.raise_for_status()
                return r.json()
            except Exception as e:  # try next mirror
                last = e
    raise last  # type: ignore[misc]


@router.get("/stations/search")
async def stations_search(q: str = "", tag: str = "", limit: int = 24):
    params: dict[str, Any] = {
        "limit": limit, "hidebroken": "true", "order": "clickcount", "reverse": "true",
    }
    if q:
        params["name"] = q
    if tag:
        params["tag"] = tag
    data = await _get("/json/stations/search", params)
    seen, out = set(), []
    for st in data:
        n = _norm(st)
        if n["url"] and n["name"].lower() not in seen:
            seen.add(n["name"].lower())
            out.append(n)
    return {"stations": out}


@router.get("/stations/featured")
async def stations_featured():
    """A curated spread for the radio dial: top by votes across vibes."""
    tags = ["jazz", "lofi", "electronic", "classical", "rock", "hip hop", "news", "arabic"]
    shelves = []
    for tag in tags:
        try:
            data = await _get("/json/stations/search", {
                "tag": tag, "limit": 8, "hidebroken": "true",
                "order": "votes", "reverse": "true",
            })
            sts = [_norm(s) for s in data if s.get("url")]
            if sts:
                shelves.append({"tag": tag, "stations": sts})
        except Exception:
            continue
    return {"shelves": shelves}


_station_client = httpx.AsyncClient(timeout=20.0, follow_redirects=True, headers=_HEADERS)


@router.get("/stations/listen")
async def stations_listen(url: str, request: Request):
    """Proxy a live radio stream through Orpheus.

    Why: many stations are plain http:// (mixed-content blocked in browsers),
    send Icecast headers browsers choke on, or lack CORS. Proxying through
    our own origin fixes all three — and lets DLNA renderers fetch a clean
    LAN URL too. Streams are infinite; we just relay bytes.
    """
    if not url.lower().startswith(("http://", "https://")):
        raise HTTPException(400, "bad url")
    upstream = await _station_client.send(
        _station_client.build_request(
            "GET", url, headers={"Icy-MetaData": "0", "User-Agent": _HEADERS["User-Agent"]}
        ),
        stream=True,
    )
    if upstream.status_code >= 400:
        await upstream.aclose()
        raise HTTPException(502, f"station refused ({upstream.status_code})")
    ctype = upstream.headers.get("content-type", "audio/mpeg").split(";")[0]
    if ctype in ("application/octet-stream", "audio/x-mpegurl", "audio/mpegurl"):
        ctype = "audio/mpeg"

    async def body():
        try:
            async for chunk in upstream.aiter_bytes(32 * 1024):
                yield chunk
        finally:
            await upstream.aclose()

    return StreamingResponse(body(), media_type=ctype, headers={"Cache-Control": "no-store"})

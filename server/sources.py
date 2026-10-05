"""Orpheus Sources — link Spotify / Apple Music / YouTube Music accounts
and import playlists + liked songs into the local library.

Philosophy: providers are *catalogs*, not gatekeepers. We import your
metadata locally (title/artist/album/art), and playback resolves each
track lazily to a playable stream (YT Music match) — so a playlist you
built on Spotify plays fine even without Spotify.

Linking (one-time, personal-use):
  spotify  → sp_dc cookie from open.spotify.com (DevTools → Cookies)
  apple    → media-user-token from music.apple.com (DevTools → Storage)
  ytmusic  → full request headers from music.youtube.com (ytmusicapi)
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Optional

import httpx
from fastapi import APIRouter, HTTPException
from ytmusicapi import YTMusic

from .music import _yt as _yt_anon, _norm_song
from .store import db, set_setting, setting

router = APIRouter(prefix="/api")

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"


# ---------------------------------------------------------------- helpers
def _playlist_upsert(source: str, external_id: str, name: str, art: Optional[str],
                     tracks: list[dict]) -> int:
    c = db()
    c.execute(
        "INSERT INTO playlists(source,external_id,name,art,imported_ts) VALUES(?,?,?,?,?) "
        "ON CONFLICT(source,external_id) DO UPDATE SET name=excluded.name, art=excluded.art, imported_ts=excluded.imported_ts",
        (source, external_id, name, art, int(time.time())),
    )
    pid = c.execute(
        "SELECT id FROM playlists WHERE source=? AND external_id=?", (source, external_id)
    ).fetchone()["id"]
    c.execute("DELETE FROM playlist_tracks WHERE playlist_id=?", (pid,))
    for i, t in enumerate(tracks):
        c.execute(
            "INSERT INTO playlist_tracks(playlist_id,pos,title,artist,album,art,duration,yt_id) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (pid, i, t.get("title"), t.get("artist"), t.get("album"), t.get("art"),
             t.get("duration"), t.get("yt_id")),
        )
    c.commit()
    return pid


# ---------------------------------------------------------------- spotify
# Official OAuth 2.0 Authorization Code + PKCE. No client secret (public
# client, safe to run locally). The old sp_dc web-token trick was killed by
# Spotify (endpoint now 400s: "not permitted under Developer Terms").
#
# One-time setup by the user (documented in the UI):
#   1. developer.spotify.com/dashboard → Create app
#   2. Redirect URI: http://127.0.0.1:7777/api/sources/spotify/callback
#      (Spotify requires loopback 127.0.0.1 — NOT "localhost" — for http)
#   3. paste the Client ID into Orpheus → LOGIN WITH SPOTIFY
SPOTIFY_SCOPES = (
    "user-library-read playlist-read-private playlist-read-collaborative "
    "user-read-playback-state user-modify-playback-state "
    "user-read-private streaming"
)
SPOTIFY_REDIRECT = "http://127.0.0.1:7777/api/sources/spotify/callback"

_pkce_pending: dict[str, str] = {}  # state -> code_verifier
_sp_token_cache: dict[str, Any] = {"tok": None, "exp": 0.0}


def _pkce_pair() -> tuple[str, str]:
    import base64
    import hashlib
    import secrets

    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).decode().rstrip("=")
    return verifier, challenge


async def _spotify_token() -> str:
    """Mint a fresh access token from the stored refresh token (cached)."""
    if _sp_token_cache["tok"] and _sp_token_cache["exp"] > time.time() + 60:
        return _sp_token_cache["tok"]
    refresh = setting("spotify_refresh_token")
    client_id = setting("spotify_client_id")
    if not refresh or not client_id:
        raise HTTPException(428, "spotify not linked")
    async with httpx.AsyncClient(timeout=15.0) as c:
        r = await c.post(
            "https://accounts.spotify.com/api/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh,
                "client_id": client_id,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    if r.status_code != 200:
        raise HTTPException(401, f"spotify token refresh failed — re-link ({r.status_code})")
    data = r.json()
    tok = data["access_token"]
    _sp_token_cache.update(tok=tok, exp=time.time() + data.get("expires_in", 3600))
    if data.get("refresh_token"):  # Spotify may rotate it
        set_setting("spotify_refresh_token", data["refresh_token"])
    return tok


@router.post("/sources/spotify/config")
async def spotify_config(payload: dict):
    """Store the user's Spotify app Client ID (from developer dashboard)."""
    cid = (payload.get("client_id") or "").strip()
    if not cid or len(cid) < 16:
        raise HTTPException(400, "valid client_id required")
    set_setting("spotify_client_id", cid)
    return {"ok": True, "redirect_uri": SPOTIFY_REDIRECT}


@router.get("/sources/spotify/login")
async def spotify_login():
    """Build the Spotify consent URL and hand it to the front-end to open."""
    import secrets
    import urllib.parse

    client_id = setting("spotify_client_id")
    if not client_id:
        raise HTTPException(428, "set your Spotify Client ID first")
    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(16)
    _pkce_pending[state] = verifier
    # keep the pending map small
    if len(_pkce_pending) > 8:
        for k in list(_pkce_pending)[:-4]:
            _pkce_pending.pop(k, None)
    params = urllib.parse.urlencode({
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": SPOTIFY_REDIRECT,
        "scope": SPOTIFY_SCOPES,
        "code_challenge_method": "S256",
        "code_challenge": challenge,
        "state": state,
    })
    return {"url": f"https://accounts.spotify.com/authorize?{params}"}


@router.get("/sources/spotify/callback")
async def spotify_callback(code: str | None = None, state: str | None = None,
                           error: str | None = None):
    """Spotify redirects here after consent. Exchange code → refresh token."""
    from fastapi.responses import HTMLResponse

    def _page(msg: str, ok: bool) -> HTMLResponse:
        color = "#c8ff00" if ok else "#ff5c5c"
        return HTMLResponse(
            f"<!doctype html><meta charset=utf-8><body style='background:#0000f2;"
            f"color:{color};font-family:ui-monospace,monospace;display:grid;"
            f"place-items:center;height:100vh;margin:0;text-align:center'>"
            f"<div><h1 style='letter-spacing:2px'>{msg}</h1>"
            f"<p style='color:#fff;opacity:.7'>you can close this tab and return to Orpheus</p></div>"
            f"<script>try{{window.opener&&window.opener.postMessage("
            f"{{orpheus_spotify:{'true' if ok else 'false'}}},'*');"
            f"setTimeout(()=>window.close(),1200)}}catch(e){{}}</script></body>"
        )

    if error:
        return _page(f"SPOTIFY DENIED: {error}", False)
    if not code or not state or state not in _pkce_pending:
        return _page("LINK FAILED — STALE OR INVALID REQUEST", False)
    verifier = _pkce_pending.pop(state)
    client_id = setting("spotify_client_id")
    async with httpx.AsyncClient(timeout=15.0) as c:
        r = await c.post(
            "https://accounts.spotify.com/api/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": SPOTIFY_REDIRECT,
                "client_id": client_id,
                "code_verifier": verifier,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    if r.status_code != 200:
        return _page(f"TOKEN EXCHANGE FAILED ({r.status_code})", False)
    data = r.json()
    refresh = data.get("refresh_token")
    if not refresh:
        return _page("NO REFRESH TOKEN RETURNED", False)
    set_setting("spotify_refresh_token", refresh)
    _sp_token_cache.update(tok=data.get("access_token"),
                           exp=time.time() + data.get("expires_in", 3600))
    return _page("SPOTIFY LINKED ✓", True)


@router.post("/sources/spotify/import")
async def spotify_import():
    if not setting("spotify_refresh_token"):
        raise HTTPException(428, "spotify not linked")
    token = await _spotify_token()
    hdr = {"Authorization": f"Bearer {token}"}
    imported = []
    async with httpx.AsyncClient(timeout=20.0, headers=hdr) as c:
        # liked songs
        liked, url = [], "https://api.spotify.com/v1/me/tracks?limit=50"
        while url and len(liked) < 500:
            r = await c.get(url)
            r.raise_for_status()
            data = r.json()
            for it in data.get("items", []):
                t = it.get("track") or {}
                liked.append(_sp_norm(t))
            url = data.get("next")
        if liked:
            pid = _playlist_upsert("spotify", "liked", "Liked Songs (Spotify)",
                                   liked[0].get("art"), liked)
            imported.append({"id": pid, "name": "Liked Songs (Spotify)", "tracks": len(liked)})
        # playlists
        url = "https://api.spotify.com/v1/me/playlists?limit=50"
        r = await c.get(url)
        r.raise_for_status()
        for pl in r.json().get("items", []):
            tracks, turl = [], pl["tracks"]["href"] + "?limit=100"
            while turl and len(tracks) < 400:
                tr = await c.get(turl)
                if tr.status_code != 200:
                    break
                td = tr.json()
                for it in td.get("items", []):
                    t = it.get("track") or {}
                    if t.get("name"):
                        tracks.append(_sp_norm(t))
                turl = td.get("next")
            if tracks:
                art = (pl.get("images") or [{}])[0].get("url")
                pid = _playlist_upsert("spotify", pl["id"], pl["name"], art, tracks)
                imported.append({"id": pid, "name": pl["name"], "tracks": len(tracks)})
    return {"ok": True, "imported": imported}


def _sp_norm(t: dict) -> dict:
    imgs = (t.get("album") or {}).get("images") or [{}]
    return {
        "title": t.get("name"),
        "artist": ", ".join(a["name"] for a in t.get("artists", [])),
        "album": (t.get("album") or {}).get("name"),
        "art": imgs[0].get("url") if imgs else None,
        "duration": (t.get("duration_ms") or 0) // 1000 or None,
    }


# ---------------------------------------------------------------- apple music
async def _apple_dev_token() -> str:
    cached = setting("apple_dev_token")
    if cached:
        tok, exp = cached.split("|", 1)
        if float(exp) > time.time() + 3600:
            return tok
    import re
    async with httpx.AsyncClient(timeout=20.0, headers={"User-Agent": _UA}, follow_redirects=True) as c:
        page = await c.get("https://music.apple.com/us/browse")
        m = re.search(r'/assets/index[^"]*?\.js', page.text)
        if not m:
            raise HTTPException(502, "could not locate apple bundle")
        js = await c.get("https://music.apple.com" + m.group(0))
        jm = re.search(r'"(eyJh[\w-]+\.[\w-]+\.[\w-]+)"', js.text)
        if not jm:
            raise HTTPException(502, "could not extract apple developer token")
        tok = jm.group(1)
    set_setting("apple_dev_token", f"{tok}|{time.time() + 12 * 3600}")
    return tok


@router.post("/sources/apple/link")
async def apple_link(payload: dict):
    mut = (payload.get("media_user_token") or "").strip()
    if not mut:
        raise HTTPException(400, "media_user_token required")
    dev = await _apple_dev_token()
    async with httpx.AsyncClient(timeout=20.0) as c:
        r = await c.get(
            "https://amp-api.music.apple.com/v1/me/library/playlists?limit=1",
            headers={"Authorization": f"Bearer {dev}", "Media-User-Token": mut, "Origin": "https://music.apple.com"},
        )
        if r.status_code in (401, 403):
            raise HTTPException(401, "apple rejected media-user-token")
    set_setting("apple_mut", mut)
    return {"ok": True}


@router.post("/sources/apple/import")
async def apple_import():
    mut = setting("apple_mut")
    if not mut:
        raise HTTPException(428, "apple music not linked")
    dev = await _apple_dev_token()
    hdr = {"Authorization": f"Bearer {dev}", "Media-User-Token": mut, "Origin": "https://music.apple.com"}
    imported = []
    async with httpx.AsyncClient(timeout=25.0, headers=hdr) as c:
        r = await c.get("https://amp-api.music.apple.com/v1/me/library/playlists?limit=100")
        r.raise_for_status()
        for pl in r.json().get("data", []):
            plid = pl["id"]
            name = (pl.get("attributes") or {}).get("name", "?")
            tracks, url = [], f"https://amp-api.music.apple.com/v1/me/library/playlists/{plid}/tracks?limit=100"
            while url and len(tracks) < 400:
                tr = await c.get(url)
                if tr.status_code != 200:
                    break
                td = tr.json()
                for it in td.get("data", []):
                    a = it.get("attributes") or {}
                    art = ((a.get("artwork") or {}).get("url") or "").replace("{w}", "600").replace("{h}", "600")
                    tracks.append({
                        "title": a.get("name"), "artist": a.get("artistName"),
                        "album": a.get("albumName"), "art": art or None,
                        "duration": (a.get("durationInMillis") or 0) // 1000 or None,
                    })
                nxt = td.get("next")
                url = ("https://amp-api.music.apple.com" + nxt) if nxt else None
            if tracks:
                pid = _playlist_upsert("apple", plid, name, tracks[0].get("art"), tracks)
                imported.append({"id": pid, "name": name, "tracks": len(tracks)})
    return {"ok": True, "imported": imported}


# ---------------------------------------------------------------- yt music
@router.post("/sources/ytmusic/link")
async def ytmusic_link(payload: dict):
    """payload: {headers: 'raw request headers copied from music.youtube.com'}"""
    raw = (payload.get("headers") or "").strip()
    if not raw:
        raise HTTPException(400, "headers required")
    import ytmusicapi
    try:
        auth_str = ytmusicapi.setup(filepath=None, headers_raw=raw)
        yt = YTMusic(auth_str)
        pls = await asyncio.to_thread(yt.get_library_playlists, 1)
    except Exception as e:
        raise HTTPException(401, f"ytmusic setup failed: {e}") from e
    set_setting("ytmusic_auth", auth_str if isinstance(auth_str, str) else json.dumps(auth_str))
    return {"ok": True, "playlists_visible": len(pls)}


@router.post("/sources/ytmusic/import")
async def ytmusic_import():
    auth = setting("ytmusic_auth")
    if not auth:
        raise HTTPException(428, "ytmusic not linked")
    yt = YTMusic(auth)
    imported = []
    liked = await asyncio.to_thread(yt.get_liked_songs, 300)
    ltracks = [_norm_song(t) | {"yt_id": t.get("videoId")} for t in liked.get("tracks", [])]
    if ltracks:
        pid = _playlist_upsert("ytmusic", "liked", "Liked Music (YT)",
                               ltracks[0].get("art"), ltracks)
        imported.append({"id": pid, "name": "Liked Music (YT)", "tracks": len(ltracks)})
    pls = await asyncio.to_thread(yt.get_library_playlists, 50)
    for pl in pls:
        plid = pl.get("playlistId")
        if not plid or plid in ("LM",):
            continue
        try:
            data = await asyncio.to_thread(yt.get_playlist, plid, 400)
        except Exception:
            continue
        tracks = [_norm_song(t) | {"yt_id": t.get("videoId")} for t in data.get("tracks", []) if t.get("videoId")]
        if tracks:
            pid = _playlist_upsert("ytmusic", plid, pl.get("title", "?"),
                                   tracks[0].get("art"), tracks)
            imported.append({"id": pid, "name": pl.get("title"), "tracks": len(tracks)})
    return {"ok": True, "imported": imported}


# ---------------------------------------------------------------- library
@router.get("/sources/status")
async def sources_status():
    return {
        "spotify": setting("spotify_refresh_token") is not None,
        "apple": setting("apple_mut") is not None,
        "ytmusic": setting("ytmusic_auth") is not None,
    }


@router.get("/library")
async def library():
    rows = db().execute(
        "SELECT p.id, p.source, p.name, p.art, p.imported_ts, COUNT(t.pos) n "
        "FROM playlists p LEFT JOIN playlist_tracks t ON t.playlist_id=p.id "
        "GROUP BY p.id ORDER BY p.imported_ts DESC"
    ).fetchall()
    return {"playlists": [dict(r) for r in rows]}


@router.get("/library/{pid}")
async def library_playlist(pid: int):
    pl = db().execute("SELECT * FROM playlists WHERE id=?", (pid,)).fetchone()
    if not pl:
        raise HTTPException(404, "no such playlist")
    rows = db().execute(
        "SELECT * FROM playlist_tracks WHERE playlist_id=? ORDER BY pos", (pid,)
    ).fetchall()
    return {
        "id": pl["id"], "name": pl["name"], "source": pl["source"], "art": pl["art"],
        "tracks": [
            {"title": r["title"], "artist": r["artist"], "album": r["album"],
             "art": r["art"], "duration": r["duration"], "id": r["yt_id"],
             "pos": r["pos"], "source": pl["source"]}
            for r in rows
        ],
    }


@router.post("/library/{pid}/resolve/{pos}")
async def resolve_track(pid: int, pos: int):
    """Lazily match an imported track to a playable YT Music id."""
    r = db().execute(
        "SELECT * FROM playlist_tracks WHERE playlist_id=? AND pos=?", (pid, pos)
    ).fetchone()
    if not r:
        raise HTTPException(404, "no such track")
    if r["yt_id"]:
        return {"id": r["yt_id"]}
    q = f'{r["title"]} {r["artist"] or ""}'.strip()
    hits = await asyncio.to_thread(_yt_anon.search, q, "songs", None, 5)
    best = None
    want_dur = r["duration"]
    for h in hits:
        if not h.get("videoId"):
            continue
        if want_dur and h.get("duration_seconds") and abs(h["duration_seconds"] - want_dur) > 15:
            continue
        best = h
        break
    if not best and hits:
        best = next((h for h in hits if h.get("videoId")), None)
    if not best:
        raise HTTPException(404, "no playable match found")
    db().execute(
        "UPDATE playlist_tracks SET yt_id=? WHERE playlist_id=? AND pos=?",
        (best["videoId"], pid, pos),
    )
    db().commit()
    return {"id": best["videoId"]}

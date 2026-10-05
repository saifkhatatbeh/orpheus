"""Orpheus Sources — link a Spotify account (official OAuth, PKCE) and import
your playlists and liked songs as *metadata*: title, artist, album, art.

Playback stays local: each imported row is matched lazily against the files
in your library folder. A playlist you built on Spotify plays here with the
copies you already own; rows with no local match simply don't play. Spotify
Connect playback of the same playlists is handled by frame.py.
"""
from __future__ import annotations

import time
from typing import Any, Optional

import httpx
from fastapi import APIRouter, HTTPException
from .library import match_track
from .store import db, set_setting, setting

router = APIRouter(prefix="/api")


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
            "INSERT INTO playlist_tracks(playlist_id,pos,title,artist,album,art,duration,match_id) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (pid, i, t.get("title"), t.get("artist"), t.get("album"), t.get("art"),
             t.get("duration"), t.get("match_id")),
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


@router.get("/sources/status")
async def sources_status():
    return {"spotify": setting("spotify_refresh_token") is not None}


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
             "art": r["art"], "duration": r["duration"], "id": r["match_id"],
             "pos": r["pos"], "source": pl["source"]}
            for r in rows
        ],
    }


@router.post("/library/{pid}/resolve/{pos}")
async def resolve_track(pid: int, pos: int):
    """Lazily match an imported row to a file in the library folder."""
    r = db().execute(
        "SELECT * FROM playlist_tracks WHERE playlist_id=? AND pos=?", (pid, pos)
    ).fetchone()
    if not r:
        raise HTTPException(404, "no such track")
    if r["match_id"]:
        return {"id": r["match_id"]}
    hit = match_track(r["title"], r["artist"], r["duration"])
    if not hit:
        raise HTTPException(404, "not in your library")
    db().execute(
        "UPDATE playlist_tracks SET match_id=? WHERE playlist_id=? AND pos=?",
        (hit["id"], pid, pos),
    )
    db().commit()
    return {"id": hit["id"], "art": hit["art"]}

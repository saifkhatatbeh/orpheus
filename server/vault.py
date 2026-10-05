"""Orpheus Vault — every track you play is archived to disk, forever.

The feature no streaming app will ever ship: your listening history as
a *permanent local collection*. Tracks get downloaded (m4a + metadata)
the first time you play them. If a song is ever pulled from streaming,
or your internet dies, the Vault serves it. Orpheus becomes more
self-sufficient the more you use it.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from .constellation import hub
from .store import VAULT_DIR, db, log_play

router = APIRouter(prefix="/api")


async def _on_hub_change(action: str, state: dict) -> None:
    """Every track that starts playing gets logged + archived automatically."""
    if action in ("play_track", "next", "prev") and state.get("track"):
        t = state["track"]
        log_play("track", t)
        enqueue_archive(t)
    elif action == "station" and state.get("station"):
        log_play("station", state["station"])


hub.on_change.append(_on_hub_change)

_queue: asyncio.Queue[dict] = asyncio.Queue()
_active: set[str] = set()


def vault_path(video_id: str) -> Path | None:
    row = db().execute("SELECT path FROM vault WHERE video_id=?", (video_id,)).fetchone()
    if row:
        p = Path(row["path"])
        if p.exists():
            return p
        db().execute("DELETE FROM vault WHERE video_id=?", (video_id,))
        db().commit()
    return None


def enqueue_archive(track: dict) -> None:
    vid = track.get("id")
    if not vid or vid in _active or vault_path(vid):
        return
    _active.add(vid)
    _queue.put_nowait(track)


async def archive_worker() -> None:
    while True:
        track = await _queue.get()
        vid = track["id"]
        try:
            path = await asyncio.to_thread(_download_blocking, vid)
            db().execute(
                "INSERT OR REPLACE INTO vault(video_id,title,artist,album,art,duration,path,added_ts) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (vid, track.get("title"), track.get("artist"), track.get("album"),
                 track.get("art"), track.get("duration"), str(path), int(time.time())),
            )
            db().commit()
        except Exception:
            pass  # transient — will retry next time it's played
        finally:
            _active.discard(vid)
            _queue.task_done()


def _download_blocking(video_id: str) -> Path:
    import yt_dlp

    out = VAULT_DIR / f"{video_id}.m4a"
    opts = {
        "format": "bestaudio[ext=m4a]/bestaudio/best*",
        "outtmpl": str(VAULT_DIR / f"{video_id}.%(ext)s"),
        "quiet": True, "no_warnings": True, "noplaylist": True,
        "extractor_args": {"youtube": {"player_client": ["android", "web", "ios"]}},
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "m4a"}],
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([f"https://music.youtube.com/watch?v={video_id}"])
    if not out.exists():
        cand = list(VAULT_DIR.glob(f"{video_id}.*"))
        if not cand:
            raise RuntimeError("download produced no file")
        out = cand[0]
    return out


@router.get("/vault")
async def vault_list(limit: int = 200):
    rows = db().execute(
        "SELECT video_id,title,artist,album,art,duration,added_ts FROM vault ORDER BY added_ts DESC LIMIT ?",
        (limit,),
    ).fetchall()
    total = db().execute("SELECT COUNT(*) n, COALESCE(SUM(duration),0) s FROM vault").fetchone()
    size = sum(f.stat().st_size for f in VAULT_DIR.glob("*") if f.is_file())
    return {
        "tracks": [
            {**dict(r), "id": r["video_id"], "source": "vault"} for r in rows
        ],
        "count": total["n"], "seconds": total["s"], "bytes": size,
    }


@router.get("/vault/file/{video_id}")
async def vault_file(video_id: str):
    p = vault_path(video_id)
    if not p:
        raise HTTPException(404, "not in vault")
    return FileResponse(p, media_type="audio/mp4")


@router.post("/played")
async def played(payload: dict):
    """Called by the hub/front-end when a track actually starts."""
    track = payload.get("track") or {}
    kind = payload.get("kind", "track")
    log_play(kind, track)
    if kind == "track" and track.get("id"):
        enqueue_archive(track)
    return {"ok": True}


@router.get("/rewind")
async def rewind():
    """Shelves from your own history: this day last week / month / year."""
    now = int(time.time())
    day = 86400
    shelves = []
    for label, ago, window in (
        ("this day last week", 7 * day, day),
        ("this day last month", 30 * day, 2 * day),
        ("this day last year", 365 * day, 3 * day),
        ("your first plays", None, None),
    ):
        if ago is None:
            rows = db().execute(
                "SELECT track FROM plays WHERE kind='track' ORDER BY ts ASC LIMIT 12"
            ).fetchall()
        else:
            rows = db().execute(
                "SELECT track FROM plays WHERE kind='track' AND ts BETWEEN ? AND ? ORDER BY ts DESC LIMIT 12",
                (now - ago - (window or 0), now - ago + (window or 0)),
            ).fetchall()
        seen, tracks = set(), []
        for r in rows:
            t = json.loads(r["track"])
            if t.get("id") and t["id"] not in seen:
                seen.add(t["id"])
                tracks.append(t)
        if tracks:
            shelves.append({"title": label, "items": tracks})
    return {"shelves": shelves}

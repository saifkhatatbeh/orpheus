"""Orpheus library — your own audio files, indexed and served on the LAN.

Point ORPHEUS_LIBRARY at a folder (default: ./library). The scanner walks it,
reads tags and duration with ffprobe, pulls embedded cover art out with
ffmpeg, and keeps an index in sqlite. Everything the deck plays comes from
here or from a live radio station; nothing is fetched from a streaming
service and nothing is downloaded.

Endpoints keep the shapes the front end already speaks (search / album /
artist / radio / home / stream), so the UI did not need to change when the
catalog moved from the cloud to a folder.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from .constellation import hub
from .store import ROOT, db, log_play

router = APIRouter(prefix="/api")

LIBRARY_DIR = Path(os.environ.get("ORPHEUS_LIBRARY", ROOT / "library")).expanduser()
ART_DIR = ROOT / ".art"  # extracted cover art, gitignored
AUDIO_EXT = {".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wav", ".aiff", ".aif", ".wma", ".alac"}
MEDIA_TYPE = {
    ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".aac": "audio/aac", ".flac": "audio/flac",
    ".ogg": "audio/ogg", ".opus": "audio/ogg", ".wav": "audio/wav", ".aiff": "audio/aiff",
    ".aif": "audio/aiff", ".wma": "audio/x-ms-wma", ".alac": "audio/mp4",
}
FOLDER_ART = ("cover.jpg", "cover.png", "folder.jpg", "folder.png", "front.jpg", "album.jpg")

_scan_lock = asyncio.Lock()
_scan_state: dict[str, Any] = {"running": False, "last": None, "added": 0, "removed": 0}


# ------------------------------------------------------------------ schema
def init() -> None:
    db().executescript(
        """
        CREATE TABLE IF NOT EXISTS tracks(
            id TEXT PRIMARY KEY,
            path TEXT NOT NULL UNIQUE,
            title TEXT, artist TEXT, album TEXT, album_artist TEXT,
            track_no INTEGER, year TEXT,
            duration INTEGER, art TEXT,
            mtime REAL, added_ts INTEGER
        );
        CREATE INDEX IF NOT EXISTS tracks_artist ON tracks(artist);
        CREATE INDEX IF NOT EXISTS tracks_album ON tracks(album);
        """
    )
    db().commit()
    LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
    ART_DIR.mkdir(exist_ok=True)


def _id(path: Path) -> str:
    return hashlib.sha1(str(path.resolve()).encode()).hexdigest()[:16]


def _key(*parts: Optional[str]) -> str:
    return hashlib.sha1("|".join((p or "").lower().strip() for p in parts).encode()).hexdigest()[:16]


# ------------------------------------------------------------------ scanning
def _ffprobe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, timeout=30,
    ).stdout
    return json.loads(out or "{}")


def _tag(tags: dict, *names: str) -> Optional[str]:
    lower = {k.lower(): v for k, v in tags.items()}
    for n in names:
        if v := lower.get(n.lower()):
            return str(v).strip() or None
    return None


def _extract_art(path: Path, tid: str, info: dict) -> Optional[str]:
    """Embedded picture → .art/<id>.jpg; else a cover file beside the track."""
    target = ART_DIR / f"{tid}.jpg"
    if target.exists():
        return f"/api/art/{tid}"
    has_pic = any(
        s.get("codec_type") == "video" and (s.get("disposition", {}).get("attached_pic") or s.get("codec_name") in ("mjpeg", "png"))
        for s in info.get("streams", [])
    )
    if has_pic:
        subprocess.run(
            ["ffmpeg", "-y", "-v", "quiet", "-i", str(path), "-an", "-map", "0:v:0", "-frames:v", "1", str(target)],
            capture_output=True, timeout=30,
        )
        if target.exists() and target.stat().st_size > 0:
            return f"/api/art/{tid}"
    for name in FOLDER_ART:
        side = path.parent / name
        if side.exists():
            try:
                target.write_bytes(side.read_bytes())
                return f"/api/art/{tid}"
            except OSError:
                break
    return None


def _index_file(path: Path) -> dict:
    info = _ffprobe(path)
    fmt = info.get("format", {})
    tags = fmt.get("tags") or {}
    if not tags:  # some containers put tags on the stream
        for s in info.get("streams", []):
            if s.get("tags"):
                tags = s["tags"]
                break
    tid = _id(path)
    title = _tag(tags, "title") or path.stem
    artist = _tag(tags, "artist", "album_artist", "performer") or "Unknown Artist"
    album = _tag(tags, "album")
    track_no = _tag(tags, "track")
    try:
        track_no = int(str(track_no).split("/")[0]) if track_no else None
    except ValueError:
        track_no = None
    duration = fmt.get("duration")
    return {
        "id": tid,
        "path": str(path),
        "title": title,
        "artist": artist,
        "album": album,
        "album_artist": _tag(tags, "album_artist") or artist,
        "track_no": track_no,
        "year": (_tag(tags, "date", "year") or "")[:4] or None,
        "duration": int(float(duration)) if duration else None,
        "art": _extract_art(path, tid, info),
        "mtime": path.stat().st_mtime,
    }


def scan_blocking() -> dict:
    """Walk the library folder; index new/changed files, forget missing ones."""
    c = db()
    known = {r["path"]: r["mtime"] for r in c.execute("SELECT path, mtime FROM tracks")}
    seen, added = set(), 0
    for p in sorted(LIBRARY_DIR.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in AUDIO_EXT or p.name.startswith("."):
            continue
        seen.add(str(p))
        if known.get(str(p)) == p.stat().st_mtime:
            continue
        try:
            row = _index_file(p)
        except Exception:
            continue  # unreadable file: skip, try again next scan
        c.execute(
            "INSERT INTO tracks(id,path,title,artist,album,album_artist,track_no,year,duration,art,mtime,added_ts) "
            "VALUES(:id,:path,:title,:artist,:album,:album_artist,:track_no,:year,:duration,:art,:mtime,:added_ts) "
            "ON CONFLICT(path) DO UPDATE SET title=excluded.title, artist=excluded.artist, album=excluded.album, "
            "album_artist=excluded.album_artist, track_no=excluded.track_no, year=excluded.year, "
            "duration=excluded.duration, art=excluded.art, mtime=excluded.mtime",
            {**row, "added_ts": int(time.time())},
        )
        added += 1
    gone = [p for p in known if p not in seen]
    for p in gone:
        c.execute("DELETE FROM tracks WHERE path=?", (p,))
    c.commit()
    return {"added": added, "removed": len(gone), "total": _count()}


async def scan() -> dict:
    if _scan_state["running"]:
        return {"running": True}
    async with _scan_lock:
        _scan_state["running"] = True
        try:
            res = await asyncio.to_thread(scan_blocking)
            _scan_state.update(res, last=int(time.time()))
            await hub.broadcast({"type": "library", **res})
            return res
        finally:
            _scan_state["running"] = False


def _count() -> int:
    return db().execute("SELECT COUNT(*) n FROM tracks").fetchone()["n"]


# ------------------------------------------------------------------ shapes
def _song(r) -> dict:
    return {
        "id": r["id"], "title": r["title"], "artist": r["artist"], "album": r["album"],
        "duration": r["duration"], "art": r["art"], "source": "library",
    }


def track_path(tid: str) -> Path | None:
    row = db().execute("SELECT path FROM tracks WHERE id=?", (tid,)).fetchone()
    if row and Path(row["path"]).exists():
        return Path(row["path"])
    return None


_WORD = re.compile(r"[^a-z0-9]+")


def _norm(s: Optional[str]) -> str:
    s = (s or "").lower()
    s = re.sub(r"\(.*?\)|\[.*?\]|feat\..*|ft\..*", " ", s)  # drop "(remaster)", "feat. x"
    return _WORD.sub(" ", s).strip()


def match_track(title: str, artist: Optional[str], duration: Optional[int] = None) -> Optional[dict]:
    """Best local file for an imported playlist row: title match, then artist overlap, then duration."""
    t = _norm(title)
    if not t:
        return None
    rows = db().execute(
        "SELECT * FROM tracks WHERE lower(title) LIKE ? LIMIT 50", (f"%{t.split(' ')[0]}%",)
    ).fetchall()
    want_artist = set(_norm(artist).split())
    best, best_score = None, 0.0
    for r in rows:
        score = 0.0
        if _norm(r["title"]) == t:
            score += 3
        elif t in _norm(r["title"]) or _norm(r["title"]) in t:
            score += 1.5
        else:
            continue
        have = set(_norm(r["artist"]).split()) | set(_norm(r["album_artist"]).split())
        if want_artist and have & want_artist:
            score += 2
        if duration and r["duration"] and abs(r["duration"] - duration) <= 5:
            score += 1
        if score > best_score:
            best, best_score = r, score
    return _song(best) if best and best_score >= 3 else None


# ------------------------------------------------------------------ routes
@router.get("/search")
async def search(q: str, kind: str = "songs", limit: int = 24) -> dict[str, Any]:
    q = q.strip()
    if not q:
        return {"results": []}
    like = f"%{q.lower()}%"
    c = db()
    if kind == "songs":
        rows = c.execute(
            "SELECT * FROM tracks WHERE lower(title) LIKE ? OR lower(artist) LIKE ? OR lower(album) LIKE ? "
            "ORDER BY (lower(title) LIKE ?) DESC, artist, album, track_no LIMIT ?",
            (like, like, like, like, limit),
        ).fetchall()
        return {"results": [_song(r) for r in rows]}
    if kind == "albums":
        rows = c.execute(
            "SELECT album, album_artist, year, MIN(art) art, COUNT(*) n FROM tracks WHERE album IS NOT NULL "
            "AND (lower(album) LIKE ? OR lower(album_artist) LIKE ?) GROUP BY lower(album), lower(album_artist) "
            "ORDER BY album LIMIT ?",
            (like, like, limit),
        ).fetchall()
        return {"results": [
            {"kind": "album", "browseId": "al:" + _key(r["album_artist"], r["album"]), "title": r["album"],
             "artist": r["album_artist"], "year": r["year"], "art": r["art"]}
            for r in rows
        ]}
    if kind == "artists":
        rows = c.execute(
            "SELECT artist, MIN(art) art, COUNT(*) n FROM tracks WHERE lower(artist) LIKE ? "
            "GROUP BY lower(artist) ORDER BY n DESC LIMIT ?",
            (like, limit),
        ).fetchall()
        return {"results": [
            {"kind": "artist", "browseId": "ar:" + _key(r["artist"]), "name": r["artist"], "art": r["art"]}
            for r in rows
        ]}
    raise HTTPException(400, "kind must be one of ['albums', 'artists', 'songs']")


@router.get("/album/{browse_id}")
async def album(browse_id: str) -> dict[str, Any]:
    key = browse_id.removeprefix("al:")
    rows = [r for r in db().execute("SELECT * FROM tracks WHERE album IS NOT NULL ORDER BY track_no, title")
            if _key(r["album_artist"], r["album"]) == key]
    if not rows:
        raise HTTPException(404, "no such album")
    head = rows[0]
    return {
        "title": head["album"], "artist": head["album_artist"], "year": head["year"],
        "art": next((r["art"] for r in rows if r["art"]), None),
        "tracks": [_song(r) for r in rows],
    }


@router.get("/artist/{browse_id}")
async def artist(browse_id: str) -> dict[str, Any]:
    key = browse_id.removeprefix("ar:")
    rows = [r for r in db().execute("SELECT * FROM tracks ORDER BY album, track_no, title")
            if _key(r["artist"]) == key]
    if not rows:
        raise HTTPException(404, "no such artist")
    albums, seen = [], set()
    for r in rows:
        if r["album"] and r["album"].lower() not in seen:
            seen.add(r["album"].lower())
            albums.append({"kind": "album", "browseId": "al:" + _key(r["album_artist"], r["album"]),
                           "title": r["album"], "year": r["year"], "art": r["art"]})
    return {
        "name": rows[0]["artist"], "description": None,
        "art": next((r["art"] for r in rows if r["art"]), None),
        "songs": [_song(r) for r in rows], "albums": albums,
    }


@router.get("/radio/{track_id}")
async def radio(track_id: str, limit: int = 25) -> dict[str, Any]:
    """'Song radio' from your own shelves: same artist first, then the rest, shuffled."""
    seed = db().execute("SELECT * FROM tracks WHERE id=?", (track_id,)).fetchone()
    if not seed:
        raise HTTPException(404, "no such track")
    same = db().execute("SELECT * FROM tracks WHERE id!=? AND lower(artist)=lower(?)", (track_id, seed["artist"])).fetchall()
    rest = db().execute("SELECT * FROM tracks WHERE id!=? AND lower(artist)!=lower(?)", (track_id, seed["artist"])).fetchall()
    random.shuffle(same)
    random.shuffle(rest)
    picks = (same[: max(3, limit // 3)] + rest)[:limit]
    return {"tracks": [_song(r) for r in picks]}


@router.get("/home")
async def home() -> dict[str, Any]:
    """Landing shelves built from your library and your own plays."""
    c = db()
    shelves = []
    recent = c.execute("SELECT * FROM tracks ORDER BY added_ts DESC, artist LIMIT 14").fetchall()
    if recent:
        shelves.append({"title": "recently added", "items": [_song(r) for r in recent]})
    played = c.execute("SELECT track FROM plays WHERE kind='track' ORDER BY ts DESC LIMIT 60").fetchall()
    seen, items = set(), []
    for p in played:
        t = json.loads(p["track"])
        if t.get("id") and t["id"] not in seen and track_path(t["id"]):
            seen.add(t["id"])
            items.append(t)
    if items:
        shelves.append({"title": "recently played", "items": items[:14]})
    most = c.execute(
        "SELECT json_extract(track,'$.id') id, COUNT(*) n FROM plays WHERE kind='track' "
        "GROUP BY id ORDER BY n DESC LIMIT 14"
    ).fetchall()
    rows = [c.execute("SELECT * FROM tracks WHERE id=?", (m["id"],)).fetchone() for m in most]
    rows = [r for r in rows if r]
    if rows:
        shelves.append({"title": "on repeat", "items": [_song(r) for r in rows]})
    mix = c.execute("SELECT * FROM tracks ORDER BY RANDOM() LIMIT 14").fetchall()
    if mix:
        shelves.append({"title": "shuffle the shelves", "items": [_song(r) for r in mix]})
    return {"shelves": shelves}


@router.get("/stream/{track_id}")
async def stream(track_id: str):
    """Serve the file itself. Starlette's FileResponse honours Range, so every browser on the LAN can seek."""
    p = track_path(track_id)
    if not p:
        raise HTTPException(404, "not in library")
    return FileResponse(p, media_type=MEDIA_TYPE.get(p.suffix.lower(), "application/octet-stream"))


@router.get("/art/{track_id}")
async def art(track_id: str):
    p = ART_DIR / f"{track_id}.jpg"
    if not p.exists():
        raise HTTPException(404, "no art")
    return FileResponse(p, media_type="image/jpeg", headers={"Cache-Control": "max-age=86400"})


@router.get("/files")
async def files(limit: int = 500):
    rows = db().execute("SELECT * FROM tracks ORDER BY added_ts DESC, artist, album, track_no LIMIT ?", (limit,)).fetchall()
    total = db().execute("SELECT COUNT(*) n, COALESCE(SUM(duration),0) s FROM tracks").fetchone()
    size = 0
    for r in db().execute("SELECT path FROM tracks"):
        try:
            size += Path(r["path"]).stat().st_size
        except OSError:
            pass
    return {
        "tracks": [_song(r) for r in rows],
        "count": total["n"], "seconds": total["s"], "bytes": size,
        "folder": str(LIBRARY_DIR), "scan": _scan_state,
    }


@router.post("/files/rescan")
async def rescan():
    return await scan()


# ------------------------------------------------------------------ history
async def _on_hub_change(action: str, state: dict) -> None:
    if action in ("play_track", "next", "prev") and state.get("track"):
        log_play("track", state["track"])
    elif action == "station" and state.get("station"):
        log_play("station", state["station"])


hub.on_change.append(_on_hub_change)


@router.post("/played")
async def played(payload: dict):
    """Called by the front end when a track actually starts."""
    log_play(payload.get("kind", "track"), payload.get("track") or {})
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
            rows = db().execute("SELECT track FROM plays WHERE kind='track' ORDER BY ts ASC LIMIT 12").fetchall()
        else:
            rows = db().execute(
                "SELECT track FROM plays WHERE kind='track' AND ts BETWEEN ? AND ? ORDER BY ts DESC LIMIT 12",
                (now - ago - (window or 0), now - ago + (window or 0)),
            ).fetchall()
        seen, tracks = set(), []
        for r in rows:
            t = json.loads(r["track"])
            if t.get("id") and t["id"] not in seen and track_path(t["id"]):
                seen.add(t["id"])
                tracks.append(t)
        if tracks:
            shelves.append({"title": label, "items": tracks})
    return {"shelves": shelves}

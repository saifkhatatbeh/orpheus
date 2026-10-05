"""Orpheus vinyl mode — recognize what's spinning on the turntable.

The browser (any node — phone works great since it's near the Edifier)
records ~8s of mic audio and POSTs it here. We transcode with ffmpeg and
identify via Shazam (shazamio, free/unofficial). Result: title, artist,
album art — pushed to all nodes so the deck flips into vinyl view.
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from fastapi import APIRouter, HTTPException, UploadFile

from .constellation import hub

router = APIRouter(prefix="/api")


@router.post("/vinyl/recognize")
async def recognize(audio: UploadFile):
    from shazamio import Shazam

    raw = await audio.read()
    if len(raw) < 10_000:
        raise HTTPException(400, "clip too short — record at least ~4s")

    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / ("clip" + (Path(audio.filename or "c.webm").suffix or ".webm"))
        src.write_bytes(raw)
        wav = Path(td) / "clip.wav"
        # normalize to what shazamio digests happily
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", str(src), "-ac", "1", "-ar", "44100",
            "-t", "12", str(wav),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        if not wav.exists():
            raise HTTPException(500, "ffmpeg transcode failed")

        shazam = Shazam()
        data = await shazam.recognize(str(wav))

    track = data.get("track") or {}
    if not track:
        return {"match": None}

    images = track.get("images") or {}
    meta = {}
    for sec in track.get("sections", []):
        for item in sec.get("metadata", []) or []:
            meta[item.get("title", "").lower()] = item.get("text")

    result = {
        "title": track.get("title"),
        "artist": track.get("subtitle"),
        "album": meta.get("album"),
        "year": meta.get("released"),
        "label": meta.get("label"),
        "art": images.get("coverarthq") or images.get("coverart"),
        "shazam_url": track.get("url"),
    }
    await hub.apply("vinyl_result", {"result": result}, None)
    return {"match": result}

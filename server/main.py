"""Orpheus — local-first sound system. Run: python -m server.main"""
from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import store

store.init()  # before routers import db state

from . import constellation, dlna, frame, guest, library, lyrics, party, sources, stations  # noqa: E402

library.init()

app = FastAPI(title="Orpheus", docs_url="/api/docs", openapi_url="/api/openapi.json")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # LAN-only personal app
    allow_methods=["*"],
    allow_headers=["*"],
)

for mod in (library, constellation, stations, dlna, sources, lyrics, guest, frame, party):
    app.include_router(mod.router)

WEB = Path(__file__).resolve().parent.parent / "web"
app.mount("/assets", StaticFiles(directory=WEB / "assets"), name="assets")


@app.on_event("startup")
async def _startup():
    party.cleanup_orphans()  # stray ffmpeg feeders from a crashed run
    asyncio.create_task(library.scan())  # index the library folder in the background


@app.get("/")
async def index():
    return FileResponse(WEB / "index.html")


@app.get("/api/health")
async def health():
    return {"ok": True, "app": "orpheus"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server.main:app", host="0.0.0.0", port=7777, log_level="info")

"""Orpheus local store — sqlite for plays, the library index and imported playlists."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "orpheus.db"

_local = threading.local()


def db() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        _local.conn = conn
    return conn


def init() -> None:
    c = db()
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS plays(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER NOT NULL,
            kind TEXT NOT NULL,            -- track | station
            track TEXT NOT NULL            -- json blob
        );
        CREATE TABLE IF NOT EXISTS playlists(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,          -- spotify
            external_id TEXT,
            name TEXT NOT NULL,
            art TEXT,
            imported_ts INTEGER,
            UNIQUE(source, external_id)
        );
        CREATE TABLE IF NOT EXISTS playlist_tracks(
            playlist_id INTEGER NOT NULL,
            pos INTEGER NOT NULL,
            title TEXT, artist TEXT, album TEXT, art TEXT,
            duration INTEGER,
            match_id TEXT,                 -- local track id, matched lazily
            PRIMARY KEY(playlist_id, pos)
        );
        CREATE TABLE IF NOT EXISTS settings(
            key TEXT PRIMARY KEY, value TEXT
        );
        """
    )
    c.commit()


def setting(key: str, default: str | None = None) -> str | None:
    row = db().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    db().execute(
        "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    db().commit()


def log_play(kind: str, track: dict) -> None:
    db().execute(
        "INSERT INTO plays(ts,kind,track) VALUES(?,?,?)",
        (int(time.time()), kind, json.dumps(track)),
    )
    db().commit()

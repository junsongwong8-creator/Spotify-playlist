"""SQLite key-value store (organiser.db). Holds the Spotify token and app state."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

DEFAULTS: dict = {
    "token": None,
    "last_seen_added_at": None,  # newest Liked Songs added_at already processed
    "proposals": {},  # id -> {track, vibe, genres, suggestions[], status}
    "moods": {},  # id -> mood playlist draft
    "artist_genres": {},  # artist id -> [genres]
    "playlist_profiles": {},  # playlist id -> profile (cached by snapshot_id)
    "last_scan": None,
    "last_scan_error": None,
}


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.RLock()
        with self._conn() as c:
            c.execute("CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT)")

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self.path))

    def __getitem__(self, key: str) -> Any:
        with self.lock, self._conn() as c:
            row = c.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
        if row is None:
            return json.loads(json.dumps(DEFAULTS.get(key)))
        return json.loads(row[0])

    def __setitem__(self, key: str, value: Any) -> None:
        with self.lock, self._conn() as c:
            c.execute("INSERT OR REPLACE INTO kv VALUES(?, ?)", (key, json.dumps(value)))

    def get(self, key: str, default: Any = None) -> Any:
        v = self[key]
        return default if v is None else v

    @staticmethod
    def new_id() -> str:
        return uuid.uuid4().hex[:12]

    @staticmethod
    def now() -> float:
        return time.time()

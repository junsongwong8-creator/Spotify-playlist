"""Tiny JSON-file store for bot state: proposals, mood drafts, caches."""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any

DEFAULT_STATE: dict[str, Any] = {
    "last_seen_added_at": None,  # newest Liked Songs added_at already processed
    "proposals": {},  # id -> playlist-add proposal for one liked track
    "moods": {},  # id -> mood playlist draft
    "artist_genres": {},  # artist id -> [genres]
    "playlist_profiles": {},  # playlist id -> {snapshot_id, profile}
    "last_scan": None,
    "last_scan_error": None,
}


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.RLock()
        self.state: dict[str, Any] = json.loads(json.dumps(DEFAULT_STATE))
        if path.exists():
            self.state.update(json.loads(path.read_text()))

    def save(self) -> None:
        with self.lock:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.state, indent=1))
            tmp.replace(self.path)

    def __getitem__(self, key: str) -> Any:
        return self.state[key]

    def __setitem__(self, key: str, value: Any) -> None:
        with self.lock:
            self.state[key] = value
            self.save()

    @staticmethod
    def new_id() -> str:
        return uuid.uuid4().hex[:12]

    @staticmethod
    def now() -> float:
        return time.time()

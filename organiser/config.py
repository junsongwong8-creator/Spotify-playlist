"""Settings from environment variables or a local .env file."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Claude's Python library needs 3.10+. On older Pythons we fall back to simple matching.
CLAUDE_SUPPORTED = sys.version_info >= (3, 10)


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass
class Settings:
    client_id: str
    redirect_uri: str
    anthropic_api_key: str
    scan_interval_minutes: int
    first_scan_days: int
    max_playlists_per_song: int
    min_match_confidence: float
    db_path: Path
    claude_model: str = "claude-opus-5-5"

    @property
    def use_claude(self) -> bool:
        return bool(self.anthropic_api_key) and CLAUDE_SUPPORTED

    @property
    def claude_note(self) -> str:
        if self.use_claude:
            return ""
        if self.anthropic_api_key and not CLAUDE_SUPPORTED:
            return "Claude matching needs Python 3.10+; using simple matching."
        return "Add ANTHROPIC_API_KEY for much better vibe matching."


def load_settings() -> Settings:
    _load_dotenv(ROOT / ".env")
    env = os.environ.get
    return Settings(
        client_id=env("SPOTIFY_CLIENT_ID", ""),
        redirect_uri=env("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8765/callback"),
        anthropic_api_key=env("ANTHROPIC_API_KEY", ""),
        scan_interval_minutes=int(env("SCAN_INTERVAL_MINUTES", "60")),
        first_scan_days=int(env("FIRST_SCAN_DAYS", "14")),
        max_playlists_per_song=max(1, int(env("MAX_PLAYLISTS_PER_SONG", "1"))),
        min_match_confidence=float(env("MIN_MATCH_CONFIDENCE", "0.7")),
        db_path=Path(env("ORGANISER_DB", str(ROOT / "organiser.db"))),
        claude_model=env("CLAUDE_MODEL", "claude-opus-5-5"),
    )

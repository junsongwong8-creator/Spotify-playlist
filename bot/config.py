"""Settings loaded from environment variables or a local .env file."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)


@dataclass
class Settings:
    spotify_client_id: str
    spotify_client_secret: str
    spotify_redirect_uri: str
    anthropic_api_key: str
    watch_interval_minutes: int
    first_scan_days: int
    min_match_confidence: float
    data_dir: Path
    claude_model: str = "claude-opus-5-5"

    @property
    def use_claude(self) -> bool:
        return bool(self.anthropic_api_key)

    @property
    def spotify_ready(self) -> bool:
        return bool(self.spotify_client_id and self.spotify_client_secret)


def load_settings() -> Settings:
    _load_dotenv(ROOT / ".env")
    data_dir = Path(os.environ.get("BOT_DATA_DIR", ROOT / "data"))
    data_dir.mkdir(parents=True, exist_ok=True)
    return Settings(
        spotify_client_id=os.environ.get("SPOTIFY_CLIENT_ID", ""),
        spotify_client_secret=os.environ.get("SPOTIFY_CLIENT_SECRET", ""),
        spotify_redirect_uri=os.environ.get(
            "SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback"
        ),
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
        watch_interval_minutes=int(os.environ.get("WATCH_INTERVAL_MINUTES", "15")),
        first_scan_days=int(os.environ.get("FIRST_SCAN_DAYS", "7")),
        min_match_confidence=float(os.environ.get("MIN_MATCH_CONFIDENCE", "0.6")),
        data_dir=data_dir,
        claude_model=os.environ.get("CLAUDE_MODEL", "claude-opus-5-5"),
    )

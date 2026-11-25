"""Local web app: approval UI + background Liked Songs watcher."""

from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel

from .config import Settings, load_settings
from .cover import make_cover
from .engine import Engine
from .llm import Claude
from .spotify import SpotifyAuthError, SpotifyClient
from .store import Store

log = logging.getLogger("bot")
STATIC = Path(__file__).parent / "static"


def build_engine(settings: Settings) -> Engine:
    spotify = SpotifyClient(
        settings.spotify_client_id,
        settings.spotify_client_secret,
        settings.spotify_redirect_uri,
        settings.data_dir / "spotify_token.json",
    )
    store = Store(settings.data_dir / "state.json")
    claude = Claude(settings.anthropic_api_key, settings.claude_model) if settings.use_claude else None
    return Engine(settings, spotify, store, claude)


class ApproveProposal(BaseModel):
    playlist_ids: list[str]


class MoodRequest(BaseModel):
    mood: str
    length: int = 25


class ApproveMood(BaseModel):
    name: str
    description: str
    track_ids: list[str]
    public: bool = False


def create_app(engine: Engine | None = None, start_watcher: bool = True) -> FastAPI:
    settings = engine.settings if engine else load_settings()
    engine = engine or build_engine(settings)
    stop = threading.Event()

    def watcher() -> None:
        while not stop.wait(1 if engine.store["last_scan"] is None else settings.watch_interval_minutes * 60):
            if not engine.spotify.logged_in:
                continue
            try:
                engine.scan()
            except Exception:
                pass  # error is recorded in state and shown in the UI

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        t = None
        if start_watcher:
            t = threading.Thread(target=watcher, daemon=True)
            t.start()
        yield
        stop.set()

    app = FastAPI(title="Spotify Playlist Bot", lifespan=lifespan)
    app.state.engine = engine

    @app.get("/", response_class=HTMLResponse)
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/login")
    def login():
        if not settings.spotify_ready:
            raise HTTPException(400, "Add SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET to .env first.")
        return RedirectResponse(engine.spotify.authorize_url())

    @app.get("/callback")
    def callback(code: str | None = None, state: str | None = None, error: str | None = None):
        if error or not code:
            return HTMLResponse(f"<p>Spotify login was cancelled ({error}). <a href='/'>Back</a></p>", 400)
        try:
            engine.spotify.handle_callback(code, state)
        except SpotifyAuthError as e:
            return HTMLResponse(f"<p>{e} <a href='/login'>Try again</a></p>", 400)
        return RedirectResponse("/")

    @app.post("/logout")
    def logout():
        engine.spotify.logout()
        return {"ok": True}

    @app.get("/api/status")
    def status():
        user = None
        if engine.spotify.logged_in:
            try:
                me = engine.spotify.me()
                user = {"name": me.get("display_name") or me.get("id"), "id": me.get("id")}
            except Exception as e:
                return {"logged_in": False, "error": str(e), "spotify_ready": settings.spotify_ready}
        return {
            "spotify_ready": settings.spotify_ready,
            "logged_in": user is not None,
            "user": user,
            "matching": "claude" if engine.claude else "genres",
            "last_scan": engine.store["last_scan"],
            "last_scan_error": engine.store["last_scan_error"],
            "watch_interval_minutes": settings.watch_interval_minutes,
            "scanning": engine.scan_lock.locked(),
        }

    def _guard():
        if not engine.spotify.logged_in:
            raise HTTPException(401, "Log in to Spotify first.")

    @app.post("/api/scan")
    def scan(backfill: int | None = None):
        _guard()
        try:
            return engine.scan(backfill=backfill)
        except Exception as e:
            raise HTTPException(502, str(e))

    @app.get("/api/proposals")
    def proposals():
        items = sorted(engine.store["proposals"].values(), key=lambda p: p["track"].get("added_at") or "", reverse=True)
        return items

    @app.post("/api/proposals/{pid}/approve")
    def approve(pid: str, body: ApproveProposal):
        _guard()
        if pid not in engine.store["proposals"]:
            raise HTTPException(404)
        try:
            return engine.approve_proposal(pid, body.playlist_ids)
        except Exception as e:
            raise HTTPException(502, str(e))

    @app.post("/api/proposals/{pid}/dismiss")
    def dismiss(pid: str):
        if pid not in engine.store["proposals"]:
            raise HTTPException(404)
        engine.dismiss_proposal(pid)
        return {"ok": True}

    @app.get("/api/moods")
    def moods():
        return sorted(engine.store["moods"].values(), key=lambda m: m["created_at"], reverse=True)

    @app.post("/api/moods")
    def new_mood(body: MoodRequest):
        _guard()
        if not body.mood.strip():
            raise HTTPException(400, "Describe a mood first.")
        try:
            return engine.curate_mood(body.mood.strip(), max(5, min(body.length, 60)))
        except Exception as e:
            raise HTTPException(502, str(e))

    @app.get("/api/moods/{mid}/cover.jpg")
    def mood_cover(mid: str):
        d = engine.store["moods"].get(mid)
        if not d:
            raise HTTPException(404)
        jpeg = make_cover(d.get("cover_images", []), engine.spotify.http)
        if not jpeg:
            raise HTTPException(404)
        return Response(jpeg, media_type="image/jpeg", headers={"Cache-Control": "max-age=3600"})

    @app.post("/api/moods/{mid}/approve")
    def approve_mood(mid: str, body: ApproveMood):
        _guard()
        if mid not in engine.store["moods"]:
            raise HTTPException(404)
        try:
            return engine.approve_mood(mid, body.name, body.description, body.track_ids, body.public)
        except ValueError as e:
            raise HTTPException(400, str(e))
        except Exception as e:
            raise HTTPException(502, str(e))

    @app.post("/api/moods/{mid}/discard")
    def discard_mood(mid: str):
        if mid not in engine.store["moods"]:
            raise HTTPException(404)
        engine.discard_mood(mid)
        return {"ok": True}

    return app

"""Flask app: the review page, a small JSON API, and the background scanner."""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Optional

from flask import Flask, jsonify, redirect, request, send_file

from .config import Settings, load_settings
from .engine import Engine
from .spotify import SpotifyClient, SpotifyError
from .store import Store

log = logging.getLogger("organiser")
STATIC = Path(__file__).parent / "static"


def build_engine(settings: Settings, http=None) -> Engine:
    store = Store(settings.db_path)
    spotify = SpotifyClient(settings.client_id, settings.redirect_uri, store, http=http)
    claude = None
    if settings.use_claude:
        from .llm import Claude  # imported lazily: needs Python 3.10+

        claude = Claude(settings.anthropic_api_key, settings.claude_model)
    return Engine(settings, spotify, store, claude)


def start_scanner(engine: Engine) -> threading.Event:
    stop = threading.Event()

    def loop() -> None:
        while not stop.wait(30):
            if not engine.spotify.logged_in:
                continue
            last = engine.store["last_scan"] or 0
            if engine.store.now() - last < engine.settings.scan_interval_minutes * 60:
                continue
            try:
                engine.scan()
            except Exception as e:  # recorded in the store and shown on the page
                log.warning("Scheduled scan failed: %s", e)

    threading.Thread(target=loop, daemon=True).start()
    return stop


def create_app(engine: Optional[Engine] = None) -> Flask:
    engine = engine or build_engine(load_settings())
    s = engine.settings
    app = Flask(__name__)

    def err(msg: str, code: int):
        return jsonify({"error": msg}), code

    def need_login():
        return None if engine.spotify.logged_in else err("Connect Spotify first.", 401)

    @app.get("/")
    def index():
        return send_file(STATIC / "index.html")

    @app.get("/login")
    def login():
        if not s.client_id:
            return "Set SPOTIFY_CLIENT_ID in your .env file (or environment) first, then restart.", 400
        return redirect(engine.spotify.authorize_url())

    @app.get("/callback")
    def callback():
        if request.args.get("error") or not request.args.get("code"):
            return redirect("/?msg=Spotify+login+was+cancelled")
        try:
            engine.spotify.handle_callback(request.args["code"], request.args.get("state", ""))
        except SpotifyError as e:
            return "<p>%s</p><p><a href='/login'>Try again</a></p>" % e, 400
        return redirect("/?msg=connected")

    @app.post("/api/logout")
    def logout():
        engine.spotify.logout()
        return jsonify(ok=True)

    @app.get("/api/status")
    def status():
        user = None
        if engine.spotify.logged_in:
            try:
                me = engine.spotify.me()
                user = {"name": me.get("display_name") or me.get("id"), "id": me.get("id")}
            except Exception as e:
                return jsonify(logged_in=False, configured=bool(s.client_id), error=str(e))
        return jsonify(
            configured=bool(s.client_id),
            logged_in=user is not None,
            user=user,
            matching="claude" if engine.claude is not None else "simple",
            matching_note=s.claude_note,
            last_scan=engine.store["last_scan"],
            last_scan_error=engine.store["last_scan_error"],
            scan_interval_minutes=s.scan_interval_minutes,
            scanning=engine.scan_lock.locked(),
        )

    @app.post("/api/scan")
    def scan():
        blocked = need_login()
        if blocked:
            return blocked
        try:
            return jsonify(engine.scan(backfill=request.args.get("backfill", type=int)))
        except Exception as e:
            return err(str(e), 502)

    @app.get("/api/proposals")
    def proposals():
        items = sorted(engine.store["proposals"].values(), key=lambda p: p["track"].get("added_at") or "", reverse=True)
        return jsonify(items)

    @app.post("/api/proposals/<pid>/approve")
    def approve(pid: str):
        blocked = need_login()
        if blocked:
            return blocked
        if pid not in engine.store["proposals"]:
            return err("Not found", 404)
        try:
            return jsonify(engine.approve_proposal(pid, (request.get_json() or {}).get("playlist_ids", [])))
        except Exception as e:
            return err(str(e), 502)

    @app.post("/api/proposals/<pid>/dismiss")
    def dismiss(pid: str):
        if pid not in engine.store["proposals"]:
            return err("Not found", 404)
        engine.dismiss_proposal(pid)
        return jsonify(ok=True)

    @app.get("/api/moods")
    def moods():
        return jsonify(sorted(engine.store["moods"].values(), key=lambda m: m["created_at"], reverse=True))

    @app.post("/api/moods")
    def new_mood():
        blocked = need_login()
        if blocked:
            return blocked
        body = request.get_json() or {}
        mood = (body.get("mood") or "").strip()
        if not mood:
            return err("Tell me a mood first.", 400)
        try:
            return jsonify(engine.curate_mood(mood, max(5, min(int(body.get("length", 25)), 60))))
        except Exception as e:
            return err(str(e), 502)

    @app.post("/api/moods/<mid>/approve")
    def approve_mood(mid: str):
        blocked = need_login()
        if blocked:
            return blocked
        if mid not in engine.store["moods"]:
            return err("Not found", 404)
        b = request.get_json() or {}
        try:
            return jsonify(engine.approve_mood(mid, b.get("name", ""), b.get("description", ""), b.get("track_ids", []), bool(b.get("public"))))
        except ValueError as e:
            return err(str(e), 400)
        except Exception as e:
            return err(str(e), 502)

    @app.post("/api/moods/<mid>/discard")
    def discard_mood(mid: str):
        if mid not in engine.store["moods"]:
            return err("Not found", 404)
        engine.discard_mood(mid)
        return jsonify(ok=True)

    app.config["engine"] = engine
    return app

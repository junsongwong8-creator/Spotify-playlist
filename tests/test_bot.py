from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bot.app import create_app
from bot.config import Settings
from bot.engine import Engine
from bot.spotify import SpotifyClient
from bot.store import Store
from tests.fakes import FakeClaude, FakeSpotify, logged_in_token


def make(tmp_path: Path, claude=True):
    fake = FakeSpotify()
    settings = Settings("cid", "secret", "http://127.0.0.1:8888/callback", "key" if claude else "",
                        15, 7, 0.6, tmp_path)
    logged_in_token(tmp_path / "tok.json")
    sp = SpotifyClient("cid", "secret", settings.spotify_redirect_uri, tmp_path / "tok.json", http=fake.client())
    engine = Engine(settings, sp, Store(tmp_path / "state.json"), FakeClaude() if claude else None)
    return fake, engine


def test_first_scan_uses_window_and_creates_proposals_without_writing(tmp_path, monkeypatch):
    fake, engine = make(tmp_path)
    # Pretend "now" is just after the newest like so the 7-day window excludes t_old
    import bot.engine as eng
    from datetime import datetime, timezone

    class FixedDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 25, 12, tzinfo=timezone.utc)

    monkeypatch.setattr(eng, "datetime", FixedDT)
    res = engine.scan()
    assert res == {"status": "ok", "new_tracks": 2}
    assert fake.writes == []  # nothing written before approval
    props = {p["track"]["id"]: p for p in engine.store["proposals"].values()}
    assert set(props) == {"t_new1", "t_new2"}
    late = props["t_new1"]["suggestions"]
    assert [s["playlist_id"] for s in late] == ["pl_late"]  # low-confidence + bogus ids dropped
    assert props["t_new1"]["vibe"] == "hazy late-night slow jam"
    # only owned playlists were offered to Claude
    _, playlists_text, _ = engine.claude.calls[0]
    assert "pl_other" not in playlists_text
    # second scan finds nothing new
    assert engine.scan()["new_tracks"] == 0


def test_approve_adds_only_selected(tmp_path):
    fake, engine = make(tmp_path)
    engine.scan(backfill=2)
    pid = next(p["id"] for p in engine.store["proposals"].values() if p["track"]["id"] == "t_new2")
    out = engine.approve_proposal(pid, ["pl_gym"])
    assert out == {"added_to": ["Gym Rage"]}
    assert fake.writes == [("add", "pl_gym", ["spotify:track:t_new2"])]


def test_skips_playlists_that_already_have_the_track(tmp_path):
    fake, engine = make(tmp_path)
    fake.playlists["pl_late"]["tracks"].append(fake.liked[0][1])
    engine.scan(backfill=1)
    p = next(iter(engine.store["proposals"].values()))
    assert p["suggestions"] == [] and p["status"] == "no_match"


def test_heuristic_matching_without_claude(tmp_path):
    fake, engine = make(tmp_path, claude=False)
    engine.scan(backfill=2)
    props = {p["track"]["id"]: p for p in engine.store["proposals"].values()}
    assert props["t_new1"]["suggestions"][0]["playlist_id"] == "pl_late"
    assert props["t_new2"]["suggestions"][0]["playlist_id"] == "pl_gym"


def test_mood_draft_then_approve_creates_playlist_with_cover(tmp_path):
    fake, engine = make(tmp_path)
    d = engine.curate_mood("late night", 10)
    assert d["name"] == "Blue Hour"
    assert [p["source"] for p in d["picks"]] == ["library", "new", "library"]
    assert fake.writes == []
    keep = [d["picks"][0]["track"]["id"], d["picks"][1]["track"]["id"]]
    out = engine.approve_mood(d["id"], "Blue Hour", "Slow,\nwarm R&B", keep)
    assert out["status"] == "created"
    kinds = [w[0] for w in fake.writes]
    assert kinds == ["create", "add", "cover"]
    assert fake.writes[0][1]["description"] == "Slow, warm R&B"
    assert fake.writes[1][2] == ["spotify:track:t_new1", "spotify:track:t_found"]
    assert fake.writes[2][3] == "image/jpeg" and fake.writes[2][2] < 256_000


def test_http_api(tmp_path):
    fake, engine = make(tmp_path)
    client = TestClient(create_app(engine, start_watcher=False))
    s = client.get("/api/status").json()
    assert s["logged_in"] and s["matching"] == "claude"
    assert client.post("/api/scan?backfill=3").json()["new_tracks"] == 3
    props = client.get("/api/proposals").json()
    assert len(props) == 3
    p = next(p for p in props if p["track"]["id"] == "t_new1")
    assert client.post(f"/api/proposals/{p['id']}/approve", json={"playlist_ids": ["pl_late"]}).status_code == 200
    m = client.post("/api/moods", json={"mood": "late night", "length": 10}).json()
    r = client.get(f"/api/moods/{m['id']}/cover.jpg")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert client.get("/").status_code == 200


def test_login_redirect_and_state_check(tmp_path):
    fake, engine = make(tmp_path)
    client = TestClient(create_app(engine, start_watcher=False), follow_redirects=False)
    r = client.get("/login")
    assert r.status_code == 307 and "accounts.spotify.com/authorize" in r.headers["location"]
    assert "ugc-image-upload" in r.headers["location"]
    assert client.get("/callback?code=x&state=wrong").status_code == 400

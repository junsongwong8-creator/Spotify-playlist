from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import organiser.engine as eng
from organiser.config import Settings
from organiser.engine import Engine
from organiser.spotify import SpotifyClient
from organiser.store import Store
from organiser.web import create_app
from tests.fakes import FakeClaude, FakeSpotify, connect


def make(tmp_path, claude=True, max_per_song=1, logged_in=True):
    fake = FakeSpotify()
    settings = Settings("cid", "http://127.0.0.1:8765/callback", "key" if claude else "", 60, 14, max_per_song, 0.7, tmp_path / "o.db")
    store = Store(settings.db_path)
    if logged_in:
        connect(store)
    sp = SpotifyClient("cid", settings.redirect_uri, store, http=fake)
    return fake, Engine(settings, sp, store, FakeClaude() if claude else None)


def by_track(engine):
    return {p["track"]["id"]: p for p in engine.store["proposals"].values()}


def test_first_scan_window_and_no_writes(tmp_path, monkeypatch):
    fake, engine = make(tmp_path)

    class Fixed(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 25, 12, tzinfo=timezone.utc)

    monkeypatch.setattr(eng, "datetime", Fixed)
    assert engine.scan() == {"status": "ok", "new_songs": 3, "with_suggestions": 2}
    assert fake.writes == []
    props = by_track(engine)
    assert "t_old" not in props  # older than FIRST_SCAN_DAYS
    # one playlist per song by default, even though Claude also rated pl_chill 0.72
    assert [s["playlist_id"] for s in props["t_new1"]["suggestions"]] == ["pl_late"]
    assert props["t_new3"]["status"] == "no_match"
    assert "pl_other" not in engine.claude.calls[0][1]
    # next scan only looks at likes newer than the last one seen
    assert engine.scan()["new_songs"] == 0


def test_two_per_song_when_configured(tmp_path):
    _, engine = make(tmp_path, max_per_song=2)
    engine.scan(backfill=3)
    assert [s["playlist_id"] for s in by_track(engine)["t_new1"]["suggestions"]] == ["pl_late", "pl_chill"]


def test_simple_matching_is_selective(tmp_path):
    """Regression: the old version suggested every song for any playlist with a mood word in its name."""
    _, engine = make(tmp_path, claude=False)
    engine.scan(backfill=3)
    props = by_track(engine)
    assert [s["playlist_id"] for s in props["t_new1"]["suggestions"]] == ["pl_late"]
    assert [s["playlist_id"] for s in props["t_new2"]["suggestions"]] == ["pl_gym"]
    assert props["t_new3"]["suggestions"] == []
    assert not any(s["playlist_id"] == "pl_chill" for p in props.values() for s in p["suggestions"])


def test_approve_and_skip(tmp_path):
    fake, engine = make(tmp_path)
    engine.scan(backfill=3)
    props = by_track(engine)
    assert engine.approve_proposal(props["t_new2"]["id"], ["pl_gym"]) == {"added_to": ["Gym Rage"]}
    engine.dismiss_proposal(props["t_new1"]["id"])
    assert fake.writes == [("add", "pl_gym", ["spotify:track:t_new2"])]
    assert by_track(engine)["t_new1"]["status"] == "dismissed"


def test_mood_draft_then_create_with_order_and_cover(tmp_path):
    fake, engine = make(tmp_path)
    d = engine.curate_mood("late night", 10)
    assert [p["source"] for p in d["picks"]] == ["library", "new", "library"]
    assert fake.writes == []
    out = engine.approve_mood(d["id"], "Blue Hour", "Slow,\nwarm R&B", ["t_found", "t_new1"])  # reordered, one removed
    assert [w[0] for w in fake.writes] == ["create", "add", "cover"]
    assert fake.writes[0][1]["description"] == "Slow, warm R&B"
    assert fake.writes[1][2] == ["spotify:track:t_found", "spotify:track:t_new1"]
    assert fake.writes[2][3] == "image/jpeg" and fake.writes[2][2] < 256_000
    assert out["status"] == "created" and out["playlist_url"]


def test_simple_mood_without_claude(tmp_path):
    _, engine = make(tmp_path, claude=False)
    d = engine.curate_mood("gym", 10)
    assert [p["track"]["id"] for p in d["picks"]] == ["t_new2"]
    assert d["name"] == "Gym" and d["vibe_tags"] == ["metalcore"]


def test_pkce_login_flow_without_secret(tmp_path):
    fake, engine = make(tmp_path, logged_in=False)
    client = create_app(engine).test_client()
    assert client.get("/api/status").get_json()["logged_in"] is False
    loc = client.get("/login").headers["Location"]
    q = parse_qs(urlparse(loc).query)
    assert q["code_challenge_method"] == ["S256"] and "ugc-image-upload" in q["scope"][0]
    assert client.get("/callback?code=abc&state=wrong").status_code == 400
    r = client.get("/callback?code=abc&state=" + q["state"][0])
    assert r.status_code == 302 and engine.spotify.logged_in


def test_http_api_end_to_end(tmp_path):
    fake, engine = make(tmp_path)
    c = create_app(engine).test_client()
    st = c.get("/api/status").get_json()
    assert st["logged_in"] and st["matching"] == "claude"
    assert c.post("/api/scan?backfill=4").get_json()["new_songs"] == 4
    pend = [p for p in c.get("/api/proposals").get_json() if p["status"] == "pending"]
    assert {p["track"]["id"] for p in pend} == {"t_new1", "t_new2"}  # t_old: no clear fit
    p = next(p for p in pend if p["track"]["id"] == "t_new1")
    assert c.post("/api/proposals/%s/approve" % p["id"], json={"playlist_ids": ["pl_late"]}).status_code == 200
    m = c.post("/api/moods", json={"mood": "late night", "length": 10}).get_json()
    r = c.post("/api/moods/%s/approve" % m["id"], json={"name": "X", "description": "y", "track_ids": [], "public": False})
    assert r.status_code == 400
    assert c.post("/api/moods/%s/discard" % m["id"]).status_code == 200
    assert c.get("/").status_code == 200

"""In-memory fake of the Spotify endpoints the organiser uses, plus a fake Claude."""

from __future__ import annotations

import io
import json
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

from PIL import Image


def img_bytes(color: str) -> bytes:
    buf = io.BytesIO()
    try:
        Image.new("RGB", (300, 300), color).save(buf, "JPEG")
    except ValueError:
        Image.new("RGB", (300, 300), "gray").save(buf, "JPEG")
    return buf.getvalue()


def track(tid, name, artist_id, artist, album, year="2020", color="red", img_host="https://i.scdn.co/image/"):
    return {
        "id": tid, "uri": "spotify:track:" + tid, "name": name, "type": "track",
        "artists": [{"id": artist_id, "name": artist}],
        "album": {"name": album, "release_date": year + "-01-01", "images": [{"url": img_host + color}]},
        "duration_ms": 200000, "external_urls": {"spotify": "https://open.spotify.com/track/" + tid},
    }


class Resp:
    def __init__(self, status=200, body=None, content=b"", headers=None):
        self.status_code = status
        self._body = body
        self.content = content if body is None else json.dumps(body).encode()
        self.headers = headers or {}
        self.text = self.content.decode(errors="replace")

    def json(self):
        return self._body


class FakeSpotify:
    """Pass `fake` as the `http` object: it implements .request(method, url, **kw)."""

    def __init__(self, img_host="https://i.scdn.co/image/"):
        T = lambda *a, **k: track(*a, img_host=img_host, **k)  # noqa: E731
        self.liked = [  # newest first
            ("2026-11-25T10:00:00Z", T("t_new1", "Nights Glow", "a_rnb", "Velvet Hour", "Blue Rooms", color="navy")),
            ("2026-11-24T10:00:00Z", T("t_new2", "Iron Lungs", "a_metal", "Crushfield", "Weight", color="black")),
            ("2026-11-23T10:00:00Z", T("t_new3", "Paper Kites", "a_folk", "Linen", "Field Notes", color="peru")),
            ("2026-11-01T10:00:00Z", T("t_old", "Old Song", "a_rnb", "Velvet Hour", "Earlier", color="teal")),
        ]
        self.playlists = {
            "pl_late": {"name": "Late Night R&B", "owner": "me", "snap": "s1", "tracks": [
                T("t_x1", "Slow Burn", "a_rnb", "Velvet Hour", "Blue", color="purple"),
                T("t_x2", "Moonlit", "a_soul", "Soul Kid", "Moon", color="blue")]},
            "pl_gym": {"name": "Gym Rage", "owner": "me", "snap": "s2", "tracks": [
                T("t_y1", "Smash", "a_metal2", "Bonecrusher", "Hits", color="gray")]},
            # Name has a mood word; the old version suggested *every* song for this one.
            "pl_chill": {"name": "chill vibes", "owner": "me", "snap": "s4", "tracks": [
                T("t_z1", "Drift", "a_lofi", "Tapeworm", "Loops", color="green")]},
            "pl_other": {"name": "Someone else's", "owner": "friend", "snap": "s3", "tracks": []},
        }
        self.genres = {"a_rnb": ["alternative r&b", "neo soul"], "a_soul": ["neo soul"], "a_metal": ["metalcore"],
                       "a_metal2": ["metalcore", "metal"], "a_lofi": ["lo-fi beats"], "a_folk": ["indie folk"]}
        self.writes: list = []
        self.calls: list = []

    def request(self, method, url, params=None, json=None, data=None, headers=None, timeout=None):
        u = urlparse(url)
        self.calls.append((method, u.path))
        if u.netloc == "i.scdn.co" or u.netloc.startswith("127.0.0.1"):
            return Resp(content=img_bytes(u.path.rsplit("/", 1)[-1]))
        if u.netloc == "accounts.spotify.com":
            assert "client_secret" not in (data or {})
            return Resp(body={"access_token": "AT", "refresh_token": "RT", "expires_in": 3600})
        p = u.path.replace("/v1", "", 1)
        if p == "/me":
            return Resp(body={"id": "me", "display_name": "YK"})
        if p == "/me/tracks":
            return Resp(body={"items": [{"added_at": a, "track": t} for a, t in self.liked], "next": None})
        if p == "/me/playlists" and method == "GET":
            items = [{"id": pid, "name": d["name"], "description": "", "owner": {"id": d["owner"]}, "snapshot_id": d["snap"],
                      "images": [{"url": "https://i.scdn.co/image/" + pid}], "items": {"total": len(d["tracks"])},
                      "external_urls": {"spotify": "https://open.spotify.com/playlist/" + pid}} for pid, d in self.playlists.items()]
            return Resp(body={"items": items, "next": None})
        if p == "/me/playlists" and method == "POST":
            self.writes.append(("create", json))
            return Resp(201, {"id": "pl_new", "external_urls": {"spotify": "https://open.spotify.com/playlist/pl_new"}})
        if p.startswith("/playlists/") and p.endswith("/items"):
            pid = p.split("/")[2]
            if method == "GET":
                return Resp(body={"items": [{"added_at": "2026-01-01T00:00:00Z", "item": t} for t in self.playlists[pid]["tracks"]], "next": None})
            self.writes.append(("add", pid, json["uris"]))
            return Resp(201, {"snapshot_id": "new"})
        if p.startswith("/playlists/") and p.endswith("/images"):
            self.writes.append(("cover", p.split("/")[2], len(data), headers["Content-Type"]))
            return Resp(202, content=b"")
        if p.startswith("/artists/"):
            return Resp(body={"genres": self.genres.get(p.split("/")[2], [])})
        if p == "/search":
            assert int(params["limit"]) <= 10
            return Resp(body={"tracks": {"items": [track("t_found", "Found Gem", "a_rnb", "Velvet Hour", "Gems", color="olive")]}})
        return Resp(404, {"error": "unhandled %s %s" % (method, p)})


class FakeClaude:
    """Stands in for organiser.llm.Claude; returns objects shaped like its pydantic models."""

    def __init__(self):
        self.calls = []

    def match_tracks(self, playlists_text, tracks_text):
        self.calls.append(("match", playlists_text, tracks_text))
        M = lambda pid, c, r: SimpleNamespace(playlist_id=pid, confidence=c, reason=r)  # noqa: E731
        return SimpleNamespace(tracks=[
            SimpleNamespace(track_id="t_new1", vibe="hazy late-night slow jam", matches=[
                M("pl_late", 0.92, "Same hazy R&B as Slow Burn"), M("pl_chill", 0.72, "Mellow enough"),
                M("pl_gym", 0.1, "no"), M("pl_bogus", 0.9, "hallucinated id")]),
            SimpleNamespace(track_id="t_new2", vibe="crushing breakdowns", matches=[M("pl_gym", 0.88, "Heavy like Smash")]),
            SimpleNamespace(track_id="t_new3", vibe="quiet porch folk", matches=[]),
        ])

    def curate_mood(self, library_text, mood, length):
        self.calls.append(("mood", library_text, mood, length))
        P = lambda tid, title, reason: SimpleNamespace(library_track_id=tid, title=title, artist="Velvet Hour", reason=reason)  # noqa: E731
        return SimpleNamespace(name="Blue Hour", description="Slow, warm R&B for after midnight.", blurb="Starts soft and stays close.",
                               vibe_tags=["late night", "warm"],
                               picks=[P("t_new1", "Nights Glow", "Opens soft"), P(None, "Found Gem", "A new find"), P("t_old", "Old Song", "Closes it")])


def connect(store):
    store["token"] = {"access_token": "AT", "refresh_token": "RT", "expires_at": time.time() + 3600}

"""In-memory fake of the Spotify endpoints the bot uses, plus a fake Claude."""

from __future__ import annotations

import io
import json
import time

import httpx
from PIL import Image

from bot.llm import MatchResult, MoodPick, MoodPlaylist, PlaylistMatch, TrackMatches


def _img_bytes(color):
    buf = io.BytesIO()
    Image.new("RGB", (300, 300), color).save(buf, "JPEG")
    return buf.getvalue()


def track(tid, name, artist_id, artist, album, year="2020", color="red"):
    return {
        "id": tid, "uri": f"spotify:track:{tid}", "name": name, "type": "track",
        "artists": [{"id": artist_id, "name": artist}],
        "album": {"name": album, "release_date": f"{year}-01-01",
                  "images": [{"url": f"https://i.scdn.co/image/{color}"}]},
        "duration_ms": 200000, "external_urls": {"spotify": f"https://open.spotify.com/track/{tid}"},
    }


class FakeSpotify:
    def __init__(self):
        self.liked = [  # newest first
            ("2026-11-25T10:00:00Z", track("t_new1", "Nights Glow", "a_rnb", "Velvet Hour", "Blue Rooms", color="navy")),
            ("2026-11-24T10:00:00Z", track("t_new2", "Iron Lungs", "a_metal", "Crushfield", "Weight", color="black")),
            ("2026-11-01T10:00:00Z", track("t_old", "Old Song", "a_rnb", "Velvet Hour", "Earlier", color="teal")),
        ]
        self.playlists = {
            "pl_late": {"name": "Late Night R&B", "owner": "me", "snap": "s1",
                        "tracks": [track("t_x1", "Slow Burn", "a_rnb", "Velvet Hour", "Blue", color="purple"),
                                   track("t_x2", "Moonlit", "a_soul", "Soul Kid", "Moon", color="blue")]},
            "pl_gym": {"name": "Gym Rage", "owner": "me", "snap": "s2",
                       "tracks": [track("t_y1", "Smash", "a_metal2", "Bonecrusher", "Hits", color="gray")]},
            "pl_other": {"name": "Someone else's", "owner": "friend", "snap": "s3", "tracks": []},
        }
        self.genres = {"a_rnb": ["alternative r&b", "neo soul"], "a_soul": ["neo soul"],
                       "a_metal": ["metalcore"], "a_metal2": ["metalcore", "metal"]}
        self.writes: list[tuple] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        url, m, path = request.url, request.method, request.url.path
        if url.host == "i.scdn.co":
            return httpx.Response(200, content=_img_bytes(path.rsplit("/", 1)[-1]))
        if url.host == "accounts.spotify.com":
            return httpx.Response(200, json={"access_token": "AT", "refresh_token": "RT", "expires_in": 3600})
        p = path.removeprefix("/v1")
        if p == "/me":
            return httpx.Response(200, json={"id": "me", "display_name": "YK"})
        if p == "/me/tracks":
            return httpx.Response(200, json={"items": [{"added_at": a, "track": t} for a, t in self.liked], "next": None})
        if p == "/me/playlists" and m == "GET":
            items = [{"id": pid, "name": d["name"], "description": "", "owner": {"id": d["owner"]},
                      "snapshot_id": d["snap"], "images": [{"url": f"https://i.scdn.co/image/{pid}"}],
                      "items": {"total": len(d["tracks"])}, "external_urls": {"spotify": f"https://x/{pid}"}}
                     for pid, d in self.playlists.items()]
            return httpx.Response(200, json={"items": items, "next": None})
        if p == "/me/playlists" and m == "POST":
            body = json.loads(request.content)
            self.writes.append(("create", body))
            return httpx.Response(201, json={"id": "pl_new", "external_urls": {"spotify": "https://x/pl_new"}})
        if p.startswith("/playlists/") and p.endswith("/items"):
            pid = p.split("/")[2]
            if m == "GET":
                rows = [{"added_at": "2026-01-01T00:00:00Z", "item": t} for t in self.playlists[pid]["tracks"]]
                return httpx.Response(200, json={"items": rows, "next": None})
            self.writes.append(("add", pid, json.loads(request.content)["uris"]))
            return httpx.Response(201, json={"snapshot_id": "new"})
        if p.startswith("/playlists/") and p.endswith("/images"):
            self.writes.append(("cover", p.split("/")[2], len(request.content), request.headers["content-type"]))
            return httpx.Response(202)
        if p.startswith("/artists/"):
            return httpx.Response(200, json={"genres": self.genres.get(p.split("/")[2], [])})
        if p == "/search":
            return httpx.Response(200, json={"tracks": {"items": [
                track("t_found", "Found Gem", "a_rnb", "Velvet Hour", "Gems", color="olive")]}})
        return httpx.Response(404, json={"error": f"unhandled {m} {p}"})

    def client(self):
        return httpx.Client(transport=httpx.MockTransport(self.handler))


class FakeClaude:
    def __init__(self):
        self.calls = []

    def match_tracks(self, playlists_text, tracks_text):
        self.calls.append(("match", playlists_text, tracks_text))
        return MatchResult(tracks=[
            TrackMatches(track_id="t_new1", vibe="hazy late-night slow jam", matches=[
                PlaylistMatch(playlist_id="pl_late", confidence=0.92, reason="Same hazy R&B as Slow Burn"),
                PlaylistMatch(playlist_id="pl_gym", confidence=0.1, reason="nope"),
                PlaylistMatch(playlist_id="pl_bogus", confidence=0.9, reason="hallucinated id")]),
            TrackMatches(track_id="t_new2", vibe="crushing breakdowns", matches=[
                PlaylistMatch(playlist_id="pl_gym", confidence=0.88, reason="Heavy like Smash")]),
        ])

    def curate_mood(self, library_text, mood, length):
        self.calls.append(("mood", library_text, mood, length))
        return MoodPlaylist(
            name="Blue Hour", description="Slow, warm R&B for after midnight.",
            blurb="Starts soft and stays close.", vibe_tags=["late night", "warm"],
            picks=[MoodPick(library_track_id="t_new1", title="Nights Glow", artist="Velvet Hour", reason="Opens soft"),
                   MoodPick(library_track_id=None, title="Found Gem", artist="Velvet Hour", reason="A new find"),
                   MoodPick(library_track_id="t_old", title="Old Song", artist="Velvet Hour", reason="Closes it")])


def logged_in_token(path):
    path.write_text(json.dumps({"access_token": "AT", "refresh_token": "RT", "expires_at": time.time() + 3600}))

"""Minimal Spotify Web API client.

Uses the endpoint names in force after Spotify's February 2026 Development
Mode changes (e.g. /playlists/{id}/items instead of /tracks, POST
/me/playlists instead of /users/{id}/playlists, search limit max 10).
"""

from __future__ import annotations

import base64
import json
import secrets
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any, Iterator

import httpx

API = "https://api.spotify.com/v1"
AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"

SCOPES = [
    "user-library-read",
    "playlist-read-private",
    "playlist-read-collaborative",
    "playlist-modify-private",
    "playlist-modify-public",
    "ugc-image-upload",
]


class SpotifyAuthError(RuntimeError):
    pass


def normalize_track(t: dict[str, Any], added_at: str | None = None) -> dict[str, Any]:
    """Flatten a Spotify track object into the fields the bot uses."""
    album = t.get("album") or {}
    images = album.get("images") or []
    return {
        "id": t.get("id"),
        "uri": t.get("uri"),
        "name": t.get("name", ""),
        "artists": [
            {"id": a.get("id"), "name": a.get("name", "")} for a in t.get("artists") or []
        ],
        "album": {
            "name": album.get("name", ""),
            "image": images[0]["url"] if images else None,
            "release_date": album.get("release_date", ""),
        },
        "duration_ms": t.get("duration_ms"),
        "explicit": t.get("explicit", False),
        "url": (t.get("external_urls") or {}).get("spotify"),
        "added_at": added_at,
    }


def artist_line(track: dict[str, Any]) -> str:
    return ", ".join(a["name"] for a in track["artists"])


class SpotifyClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        token_path: Path,
        http: httpx.Client | None = None,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.token_path = token_path
        self.http = http or httpx.Client(timeout=30)
        self._lock = threading.Lock()
        self._token: dict[str, Any] | None = None
        self._state: str | None = None
        self._me: dict[str, Any] | None = None
        if token_path.exists():
            self._token = json.loads(token_path.read_text())

    # ---------- OAuth ----------
    @property
    def logged_in(self) -> bool:
        return bool(self._token and self._token.get("refresh_token"))

    def authorize_url(self) -> str:
        self._state = secrets.token_urlsafe(16)
        query = {
            "client_id": self.client_id,
            "response_type": "code",
            "redirect_uri": self.redirect_uri,
            "scope": " ".join(SCOPES),
            "state": self._state,
        }
        return f"{AUTH_URL}?{urllib.parse.urlencode(query)}"

    def _basic_auth(self) -> dict[str, str]:
        raw = f"{self.client_id}:{self.client_secret}".encode()
        return {"Authorization": "Basic " + base64.b64encode(raw).decode()}

    def handle_callback(self, code: str, state: str | None) -> None:
        if self._state is None or state != self._state:
            raise SpotifyAuthError("Login state did not match. Please try logging in again.")
        r = self.http.post(
            TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.redirect_uri,
            },
            headers=self._basic_auth(),
        )
        if r.status_code != 200:
            raise SpotifyAuthError(f"Spotify rejected the login: {r.text}")
        self._save_token(r.json())

    def _save_token(self, payload: dict[str, Any]) -> None:
        token = dict(self._token or {})
        token.update(payload)
        token["expires_at"] = time.time() + int(payload.get("expires_in", 3600)) - 60
        self._token = token
        self.token_path.write_text(json.dumps(token))
        try:
            self.token_path.chmod(0o600)
        except OSError:
            pass

    def _access_token(self) -> str:
        with self._lock:
            if not self._token:
                raise SpotifyAuthError("Not logged in to Spotify yet.")
            if time.time() >= self._token.get("expires_at", 0):
                r = self.http.post(
                    TOKEN_URL,
                    data={
                        "grant_type": "refresh_token",
                        "refresh_token": self._token["refresh_token"],
                    },
                    headers=self._basic_auth(),
                )
                if r.status_code != 200:
                    raise SpotifyAuthError(f"Could not refresh Spotify login: {r.text}")
                self._save_token(r.json())
            return self._token["access_token"]

    def logout(self) -> None:
        self._token = None
        self._me = None
        if self.token_path.exists():
            self.token_path.unlink()

    # ---------- HTTP ----------
    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        url = path if path.startswith("http") else f"{API}{path}"
        extra_headers = kwargs.pop("headers", {}) or {}
        r: httpx.Response | None = None
        for attempt in range(5):
            headers = extra_headers | {"Authorization": f"Bearer {self._access_token()}"}
            r = self.http.request(method, url, headers=headers, **kwargs)
            if r.status_code == 429 and attempt < 4:
                time.sleep(min(int(r.headers.get("Retry-After", "2")), 30))
                continue
            if r.status_code >= 500 and attempt < 4:
                time.sleep(1 + attempt)
                continue
            break
        assert r is not None
        if r.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"Spotify {method} {path} failed ({r.status_code}): {r.text[:300]}",
                request=r.request,
                response=r,
            )
        return r

    def _get(self, path: str, **params: Any) -> dict[str, Any]:
        return self._request("GET", path, params=params or None).json()

    def _paginate(self, path: str, **params: Any) -> Iterator[dict[str, Any]]:
        page = self._get(path, **params)
        while True:
            yield from page.get("items") or []
            nxt = page.get("next")
            if not nxt:
                return
            page = self._request("GET", nxt).json()

    # ---------- Reads ----------
    def me(self) -> dict[str, Any]:
        if self._me is None:
            self._me = self._get("/me")
        return self._me

    def liked_tracks(self, since: str | None = None, limit: int | None = None) -> list[dict]:
        """Liked Songs, newest first. Stops at `since` (ISO added_at, exclusive)."""
        out: list[dict] = []
        for item in self._paginate("/me/tracks", limit=50):
            added = item.get("added_at")
            if since and added and added <= since:
                break
            track = item.get("track") or item.get("item")
            if not track or not track.get("id"):
                continue
            out.append(normalize_track(track, added))
            if limit and len(out) >= limit:
                break
        return out

    def my_playlists(self) -> list[dict]:
        """Playlists the user owns or collaborates on (the ones we can add to)."""
        my_id = self.me()["id"]
        out = []
        for p in self._paginate("/me/playlists", limit=50):
            if not p:
                continue
            owner = (p.get("owner") or {}).get("id")
            if owner != my_id and not p.get("collaborative"):
                continue
            images = p.get("images") or []
            count_obj = p.get("items") or p.get("tracks") or {}
            out.append(
                {
                    "id": p["id"],
                    "name": p.get("name", ""),
                    "description": p.get("description") or "",
                    "image": images[0]["url"] if images else None,
                    "snapshot_id": p.get("snapshot_id"),
                    "total": count_obj.get("total", 0),
                    "url": (p.get("external_urls") or {}).get("spotify"),
                }
            )
        return out

    def playlist_tracks(self, playlist_id: str, max_items: int = 500) -> list[dict]:
        out = []
        for row in self._paginate(f"/playlists/{playlist_id}/items", limit=50):
            t = row.get("item") or row.get("track")
            if not t or t.get("type", "track") != "track" or not t.get("id"):
                continue
            out.append(normalize_track(t, row.get("added_at")))
            if len(out) >= max_items:
                break
        return out

    def artist_genres(self, artist_id: str) -> list[str]:
        try:
            return self._get(f"/artists/{artist_id}").get("genres") or []
        except httpx.HTTPStatusError:
            return []

    def search_track(self, title: str, artist: str) -> dict | None:
        q = f'track:"{title}" artist:"{artist}"'
        res = self._get("/search", q=q, type="track", limit=5)
        items = (res.get("tracks") or {}).get("items") or []
        if not items:
            res = self._get("/search", q=f"{title} {artist}", type="track", limit=5)
            items = (res.get("tracks") or {}).get("items") or []
        return normalize_track(items[0]) if items else None

    # ---------- Writes (only ever called after the user approves) ----------
    def add_to_playlist(self, playlist_id: str, uris: list[str]) -> None:
        for i in range(0, len(uris), 100):
            self._request("POST", f"/playlists/{playlist_id}/items", json={"uris": uris[i : i + 100]})

    def create_playlist(self, name: str, description: str, public: bool = False) -> dict:
        r = self._request(
            "POST",
            "/me/playlists",
            json={"name": name, "description": description[:300], "public": public},
        )
        return r.json()

    def upload_cover(self, playlist_id: str, jpeg_bytes: bytes) -> None:
        self._request(
            "PUT",
            f"/playlists/{playlist_id}/images",
            content=base64.b64encode(jpeg_bytes),
            headers={"Content-Type": "image/jpeg"},
        )

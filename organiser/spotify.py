"""Spotify Web API client: PKCE login (no client secret) and the calls the organiser uses.

Uses the endpoint names in force after Spotify's February 2026 Development Mode
changes: /playlists/{id}/items, POST /me/playlists, search limit <= 10.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import threading
import time
from typing import Any, Dict, Iterator, List, Optional
from urllib.parse import urlencode

import requests

from .store import Store

API = "https://api.spotify.com/v1"
AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
SCOPES = " ".join(
    [
        "user-library-read",
        "playlist-read-private",
        "playlist-read-collaborative",
        "playlist-modify-private",
        "playlist-modify-public",
        "ugc-image-upload",  # to set the album-art cover on mood playlists you approve
    ]
)


class SpotifyError(RuntimeError):
    pass


def normalize_track(t: Dict[str, Any], added_at: Optional[str] = None) -> Dict[str, Any]:
    album = t.get("album") or {}
    images = album.get("images") or []
    return {
        "id": t.get("id"),
        "uri": t.get("uri") or "spotify:track:%s" % t.get("id"),
        "name": t.get("name", ""),
        "artists": [{"id": a.get("id"), "name": a.get("name", "")} for a in t.get("artists") or []],
        "album": {
            "name": album.get("name", ""),
            "image": images[0]["url"] if images else None,
            "release_date": album.get("release_date", ""),
        },
        "duration_ms": t.get("duration_ms"),
        "url": (t.get("external_urls") or {}).get("spotify"),
        "added_at": added_at,
    }


def artist_line(track: Dict[str, Any]) -> str:
    return ", ".join(a["name"] for a in track["artists"])


class SpotifyClient:
    def __init__(self, client_id: str, redirect_uri: str, store: Store, http: Any = None) -> None:
        self.client_id = client_id
        self.redirect_uri = redirect_uri
        self.store = store
        self.http = http or requests.Session()
        self._lock = threading.Lock()
        self._pending: Dict[str, str] = {}  # state -> PKCE verifier
        self._me: Optional[Dict[str, Any]] = None

    # ---------- login (PKCE) ----------
    @property
    def logged_in(self) -> bool:
        t = self.store["token"]
        return bool(t and t.get("refresh_token"))

    def authorize_url(self) -> str:
        verifier = secrets.token_urlsafe(64)
        state = secrets.token_urlsafe(24)
        self._pending[state] = verifier
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        return AUTH_URL + "?" + urlencode(
            {
                "client_id": self.client_id,
                "response_type": "code",
                "redirect_uri": self.redirect_uri,
                "scope": SCOPES,
                "state": state,
                "code_challenge_method": "S256",
                "code_challenge": challenge,
            }
        )

    def handle_callback(self, code: str, state: str) -> None:
        verifier = self._pending.pop(state or "", None)
        if not verifier:
            raise SpotifyError("Login expired or didn't match. Please click Connect Spotify again.")
        r = self.http.request(
            "POST",
            TOKEN_URL,
            data={
                "client_id": self.client_id,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.redirect_uri,
                "code_verifier": verifier,
            },
            timeout=20,
        )
        if r.status_code != 200:
            raise SpotifyError("Spotify rejected the login: %s" % r.text[:200])
        self._save_token(r.json())

    def _save_token(self, payload: Dict[str, Any]) -> None:
        token = dict(self.store["token"] or {})
        token.update(payload)
        token["expires_at"] = time.time() + int(payload.get("expires_in", 3600)) - 90
        self.store["token"] = token

    def _access_token(self) -> str:
        with self._lock:
            t = self.store["token"]
            if not t:
                raise SpotifyError("Connect Spotify first.")
            if time.time() >= t.get("expires_at", 0):
                r = self.http.request(
                    "POST",
                    TOKEN_URL,
                    data={"grant_type": "refresh_token", "refresh_token": t["refresh_token"], "client_id": self.client_id},
                    timeout=20,
                )
                if r.status_code != 200:
                    raise SpotifyError("Spotify login expired. Please reconnect. (%s)" % r.text[:150])
                self._save_token(r.json())
                t = self.store["token"]
            return t["access_token"]

    def logout(self) -> None:
        self.store["token"] = None
        self._me = None

    # ---------- HTTP ----------
    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        url = path if path.startswith("http") else API + path
        extra = kwargs.pop("headers", None) or {}
        r = None
        for attempt in range(5):
            headers = dict(extra)
            headers["Authorization"] = "Bearer " + self._access_token()
            r = self.http.request(method, url, headers=headers, timeout=30, **kwargs)
            if r.status_code == 429 and attempt < 4:
                time.sleep(min(int(r.headers.get("Retry-After", "2")), 30))
                continue
            if r.status_code >= 500 and attempt < 4:
                time.sleep(1 + attempt)
                continue
            break
        if r.status_code >= 400:
            raise SpotifyError("Spotify %s %s failed (%s): %s" % (method, path.split("?")[0], r.status_code, r.text[:200]))
        return r

    def _get(self, path: str, **params: Any) -> Dict[str, Any]:
        return self._request("GET", path, params=params or None).json()

    def _paginate(self, path: str, **params: Any) -> Iterator[Dict[str, Any]]:
        page = self._get(path, **params)
        while True:
            for item in page.get("items") or []:
                yield item
            nxt = page.get("next")
            if not nxt:
                return
            page = self._request("GET", nxt).json()

    # ---------- reads ----------
    def me(self) -> Dict[str, Any]:
        if self._me is None:
            self._me = self._get("/me")
        return self._me

    def liked_tracks(self, since: Optional[str] = None, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """Liked Songs, newest first, stopping at `since` (an added_at timestamp, exclusive)."""
        out = []
        for item in self._paginate("/me/tracks", limit=50):
            added = item.get("added_at")
            if since and added and added <= since:
                break
            t = item.get("track") or item.get("item")
            if not t or not t.get("id"):
                continue
            out.append(normalize_track(t, added))
            if limit and len(out) >= limit:
                break
        return out

    def my_playlists(self) -> List[Dict[str, Any]]:
        """Playlists you own or collaborate on (the only ones Spotify lets us add to)."""
        my_id = self.me()["id"]
        out = []
        for p in self._paginate("/me/playlists", limit=50):
            if not p:
                continue
            if (p.get("owner") or {}).get("id") != my_id and not p.get("collaborative"):
                continue
            images = p.get("images") or []
            counts = p.get("items") or p.get("tracks") or {}
            out.append(
                {
                    "id": p["id"],
                    "name": p.get("name", ""),
                    "description": p.get("description") or "",
                    "image": images[0]["url"] if images else None,
                    "snapshot_id": p.get("snapshot_id"),
                    "total": counts.get("total", 0),
                    "url": (p.get("external_urls") or {}).get("spotify"),
                }
            )
        return out

    def playlist_tracks(self, playlist_id: str, max_items: int = 500) -> List[Dict[str, Any]]:
        out = []
        for row in self._paginate("/playlists/%s/items" % playlist_id, limit=50):
            t = row.get("item") or row.get("track")
            if not t or t.get("type", "track") != "track" or not t.get("id"):
                continue
            out.append(normalize_track(t, row.get("added_at")))
            if len(out) >= max_items:
                break
        return out

    def artist_genres(self, artist_id: str) -> List[str]:
        try:
            return self._get("/artists/%s" % artist_id).get("genres") or []
        except SpotifyError:
            return []

    def search_track(self, title: str, artist: str) -> Optional[Dict[str, Any]]:
        for q in ('track:"%s" artist:"%s"' % (title, artist), "%s %s" % (title, artist)):
            items = (self._get("/search", q=q, type="track", limit=5).get("tracks") or {}).get("items") or []
            if items:
                return normalize_track(items[0])
        return None

    # ---------- writes: only ever called after you click Approve ----------
    def add_to_playlist(self, playlist_id: str, uris: List[str]) -> None:
        for i in range(0, len(uris), 100):
            self._request("POST", "/playlists/%s/items" % playlist_id, json={"uris": uris[i : i + 100]})

    def create_playlist(self, name: str, description: str, public: bool = False) -> Dict[str, Any]:
        return self._request(
            "POST", "/me/playlists", json={"name": name, "description": description[:300], "public": public}
        ).json()

    def upload_cover(self, playlist_id: str, jpeg_bytes: bytes) -> None:
        self._request(
            "PUT",
            "/playlists/%s/images" % playlist_id,
            data=base64.b64encode(jpeg_bytes),
            headers={"Content-Type": "image/jpeg"},
        )

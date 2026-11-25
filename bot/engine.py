"""Core bot logic: scan Liked Songs, propose playlist adds, curate mood playlists.

Nothing in this module writes to Spotify except `approve_*`, which the web UI
only calls when the user clicks Approve.
"""

from __future__ import annotations

import logging
import re
import threading
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import Settings
from .cover import make_cover
from .llm import Claude
from .spotify import SpotifyClient, artist_line
from .store import Store

log = logging.getLogger("bot")

MAX_TRACKS_PER_MATCH_CALL = 25
MAX_LIBRARY_FOR_MOOD = 2500
PROFILE_SAMPLE = 30


def _year(track: dict) -> str:
    return (track["album"].get("release_date") or "")[:4]


def track_line(track: dict) -> str:
    year = _year(track)
    album = track["album"]["name"]
    return f'{track["name"]} - {artist_line(track)} | {album}{f" ({year})" if year else ""}'


class Engine:
    def __init__(self, settings: Settings, spotify: SpotifyClient, store: Store, claude: Claude | None):
        self.settings = settings
        self.spotify = spotify
        self.store = store
        self.claude = claude
        self.scan_lock = threading.Lock()

    # ---------- genres ----------
    def genres_for(self, artist_ids: list[str], fetch_limit: int = 40) -> dict[str, list[str]]:
        cache = self.store["artist_genres"]
        fetched = 0
        out = {}
        for aid in artist_ids:
            if not aid:
                continue
            if aid not in cache and fetched < fetch_limit:
                cache[aid] = self.spotify.artist_genres(aid)
                fetched += 1
            out[aid] = cache.get(aid, [])
        if fetched:
            self.store["artist_genres"] = cache
        return out

    def track_genres(self, track: dict) -> list[str]:
        g = self.genres_for([a["id"] for a in track["artists"]])
        return sorted({x for a in track["artists"] for x in g.get(a["id"], [])})

    # ---------- playlist profiles ----------
    def playlist_profiles(self) -> list[dict]:
        """Summaries of each owned playlist, cached by snapshot_id."""
        cached = self.store["playlist_profiles"]
        profiles = []
        changed = False
        for p in self.spotify.my_playlists():
            hit = cached.get(p["id"])
            if hit and hit.get("snapshot_id") == p["snapshot_id"]:
                profiles.append(hit)
                continue
            tracks = self.spotify.playlist_tracks(p["id"])
            artist_counts = Counter()
            artist_names = {}
            for t in tracks:
                for a in t["artists"]:
                    artist_counts[a["id"]] += 1
                    artist_names[a["id"]] = a["name"]
            top_ids = [aid for aid, _ in artist_counts.most_common(8)]
            genres = self.genres_for(top_ids)
            genre_counts = Counter()
            for aid in top_ids:
                for g in genres.get(aid, []):
                    genre_counts[g] += artist_counts[aid]
            step = max(1, len(tracks) // PROFILE_SAMPLE)
            sample = [track_line(t) for t in tracks[::step][:PROFILE_SAMPLE]]
            prof = {
                **p,
                "track_ids": [t["id"] for t in tracks],
                "artist_ids": list(artist_counts),
                "top_artists": [artist_names[a] for a in top_ids],
                "top_genres": [g for g, _ in genre_counts.most_common(10)],
                "genre_weights": dict(genre_counts.most_common(25)),
                "sample": sample,
            }
            cached[p["id"]] = prof
            profiles.append(prof)
            changed = True
        live_ids = {p["id"] for p in profiles}
        for pid in list(cached):
            if pid not in live_ids:
                del cached[pid]
                changed = True
        if changed:
            self.store["playlist_profiles"] = cached
        return profiles

    @staticmethod
    def profiles_text(profiles: list[dict]) -> str:
        blocks = []
        for p in profiles:
            if not p["sample"]:
                continue
            blocks.append(
                "\n".join(
                    [
                        f'playlist_id: {p["id"]}',
                        f'name: {p["name"]}',
                        f'description: {p["description"] or "(none)"}',
                        f'songs: {p["total"]}',
                        f'top artists: {", ".join(p["top_artists"]) or "(unknown)"}',
                        f'top genres: {", ".join(p["top_genres"]) or "(unknown)"}',
                        "sample songs:",
                        *[f"  - {s}" for s in p["sample"]],
                    ]
                )
            )
        return "\n\n".join(blocks)

    # ---------- matching ----------
    def heuristic_matches(self, track: dict, profiles: list[dict]) -> list[dict]:
        genres = set(self.track_genres(track))
        artist_ids = {a["id"] for a in track["artists"]}
        out = []
        for p in profiles:
            weights = p.get("genre_weights") or {}
            total = sum(weights.values()) or 1
            genre_score = sum(w for g, w in weights.items() if g in genres) / total
            shared_artist = bool(artist_ids & set(p.get("artist_ids", [])))
            score = min(1.0, genre_score * 1.6 + (0.45 if shared_artist else 0))
            if score <= 0:
                continue
            bits = []
            if shared_artist:
                bits.append("already has songs by this artist")
            hits = [g for g in p.get("top_genres", []) if g in genres][:3]
            if hits:
                bits.append("shares genres: " + ", ".join(hits))
            out.append(
                {"playlist_id": p["id"], "confidence": round(score, 2), "reason": "; ".join(bits).capitalize()}
            )
        return out

    def match(self, tracks: list[dict], profiles: list[dict]) -> dict[str, dict]:
        """Returns track_id -> {"vibe": str, "matches": [...]}."""
        results: dict[str, dict] = {}
        usable = [p for p in profiles if p["sample"]]
        if not usable:
            return results
        if self.claude:
            pt = self.profiles_text(usable)
            valid_ids = {p["id"] for p in usable}
            for i in range(0, len(tracks), MAX_TRACKS_PER_MATCH_CALL):
                batch = tracks[i : i + MAX_TRACKS_PER_MATCH_CALL]
                lines = []
                for t in batch:
                    g = self.track_genres(t)
                    lines.append(f'track_id: {t["id"]} | {track_line(t)}' + (f' | genres: {", ".join(g)}' if g else ""))
                res = self.claude.match_tracks(pt, "\n".join(lines))
                for tm in res.tracks:
                    results[tm.track_id] = {
                        "vibe": tm.vibe,
                        "matches": [
                            {"playlist_id": m.playlist_id, "confidence": max(0.0, min(1.0, m.confidence)), "reason": m.reason}
                            for m in tm.matches
                            if m.playlist_id in valid_ids
                        ],
                    }
        else:
            for t in tracks:
                results[t["id"]] = {"vibe": ", ".join(self.track_genres(t)[:3]), "matches": self.heuristic_matches(t, usable)}
        return results

    # ---------- scanning ----------
    def scan(self, backfill: int | None = None) -> dict[str, Any]:
        """Check Liked Songs for new tracks and create proposals. Never writes to Spotify."""
        if not self.scan_lock.acquire(blocking=False):
            return {"status": "busy"}
        try:
            since = self.store["last_seen_added_at"]
            if backfill:
                tracks = self.spotify.liked_tracks(limit=backfill)
            elif since:
                tracks = self.spotify.liked_tracks(since=since)
            else:
                cutoff = datetime.now(timezone.utc) - timedelta(days=self.settings.first_scan_days)
                cutoff_iso = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
                tracks = self.spotify.liked_tracks(since=cutoff_iso, limit=200)
            proposals = self.store["proposals"]
            known = {p["track"]["id"] for p in proposals.values()}
            new_tracks = [t for t in tracks if t["id"] not in known]
            created = 0
            if new_tracks:
                profiles = self.playlist_profiles()
                by_id = {p["id"]: p for p in profiles}
                matched = self.match(new_tracks, profiles)
                for t in new_tracks:
                    m = matched.get(t["id"], {"vibe": "", "matches": []})
                    suggestions = []
                    for s in sorted(m["matches"], key=lambda x: -x["confidence"]):
                        p = by_id.get(s["playlist_id"])
                        if not p or s["confidence"] < self.settings.min_match_confidence:
                            continue
                        if t["id"] in p.get("track_ids", []):
                            continue  # already in that playlist
                        suggestions.append(
                            {
                                **s,
                                "playlist_name": p["name"],
                                "playlist_image": p["image"],
                                "playlist_url": p["url"],
                                "status": "pending",
                            }
                        )
                    pid = self.store.new_id()
                    proposals[pid] = {
                        "id": pid,
                        "track": t,
                        "genres": self.track_genres(t),
                        "vibe": m["vibe"],
                        "suggestions": suggestions,
                        "status": "pending" if suggestions else "no_match",
                        "created_at": self.store.now(),
                    }
                    created += 1
                self.store["proposals"] = proposals
            if tracks:
                newest = max(t["added_at"] for t in tracks if t["added_at"])
                if not since or newest > since:
                    self.store["last_seen_added_at"] = newest
            self.store["last_scan"] = self.store.now()
            self.store["last_scan_error"] = None
            return {"status": "ok", "new_tracks": created}
        except Exception as e:  # surfaced in the UI
            log.exception("scan failed")
            self.store["last_scan_error"] = str(e)
            raise
        finally:
            self.scan_lock.release()

    # ---------- approvals: playlist adds ----------
    def approve_proposal(self, proposal_id: str, playlist_ids: list[str]) -> dict:
        proposals = self.store["proposals"]
        p = proposals[proposal_id]
        added = []
        for s in p["suggestions"]:
            if s["status"] != "pending":
                continue
            if s["playlist_id"] in playlist_ids:
                self.spotify.add_to_playlist(s["playlist_id"], [p["track"]["uri"]])
                s["status"] = "added"
                added.append(s["playlist_name"])
            else:
                s["status"] = "skipped"
        p["status"] = "done"
        self.store["proposals"] = proposals
        # playlist snapshots changed; profiles refresh lazily on next scan
        return {"added_to": added}

    def dismiss_proposal(self, proposal_id: str) -> None:
        proposals = self.store["proposals"]
        p = proposals[proposal_id]
        p["status"] = "dismissed"
        for s in p["suggestions"]:
            if s["status"] == "pending":
                s["status"] = "skipped"
        self.store["proposals"] = proposals

    # ---------- mood playlists ----------
    MOOD_GENRE_HINTS = {
        "chill": ["chill", "lo-fi", "ambient", "acoustic", "indie folk", "bedroom pop"],
        "sad": ["sad", "singer-songwriter", "indie folk", "slowcore", "piano"],
        "happy": ["pop", "funk", "disco", "dance pop", "k-pop"],
        "workout": ["edm", "hip hop", "trap", "drum and bass", "house", "rock"],
        "focus": ["ambient", "lo-fi", "classical", "post-rock", "instrumental", "jazz"],
        "party": ["dance", "edm", "house", "pop", "hip hop", "reggaeton"],
        "romantic": ["r&b", "soul", "neo soul", "bossa nova"],
        "night": ["r&b", "synthwave", "trip hop", "alternative r&b", "jazz"],
        "angry": ["metal", "punk", "hardcore", "rap", "rock"],
        "sleep": ["ambient", "piano", "classical", "new age"],
    }

    def _heuristic_mood(self, library: list[dict], mood: str, length: int) -> dict:
        words = set(re.findall(r"[a-z&\-]+", mood.lower()))
        hints = set(words)
        for key, genres in self.MOOD_GENRE_HINTS.items():
            if key in words:
                hints.update(genres)
        scored = []
        for t in library[:600]:
            g = " ".join(self.track_genres(t)).lower()
            text = f'{t["name"]} {t["album"]["name"]} {g}'.lower()
            score = sum(1 for h in hints if h in text)
            if score:
                scored.append((score, t))
        scored.sort(key=lambda x: -x[0])
        picks = [
            {"track": t, "reason": "Genres/keywords match your mood", "source": "library"} for _, t in scored[:length]
        ]
        title = mood.strip().title()[:40] or "Mood Mix"
        return {
            "name": title,
            "description": f"A {mood.strip()} mix pulled from your Liked Songs.",
            "blurb": "Built with simple genre matching. Add an Anthropic API key for much better vibe-based curation.",
            "vibe_tags": sorted(hints & set(sum(self.MOOD_GENRE_HINTS.values(), [])))[:6],
            "picks": picks,
        }

    def curate_mood(self, mood: str, length: int = 25) -> dict:
        library = self.spotify.liked_tracks(limit=MAX_LIBRARY_FOR_MOOD)
        by_id = {t["id"]: t for t in library}
        if self.claude:
            lib_text = "\n".join(f'{t["id"]} | {track_line(t)}' for t in library)
            plan = self.claude.curate_mood(lib_text, mood, length)
            picks = []
            seen = set()
            for pick in plan.picks:
                t = by_id.get(pick.library_track_id or "")
                source = "library"
                if t is None:
                    t = self.spotify.search_track(pick.title, pick.artist)
                    source = "new"
                if t is None or t["id"] in seen:
                    continue
                seen.add(t["id"])
                picks.append({"track": t, "reason": pick.reason, "source": source})
            draft = {
                "name": plan.name,
                "description": plan.description[:300],
                "blurb": plan.blurb,
                "vibe_tags": plan.vibe_tags,
                "picks": picks,
            }
        else:
            draft = self._heuristic_mood(library, mood, length)
        mid = self.store.new_id()
        draft.update(
            {
                "id": mid,
                "mood": mood,
                "status": "draft",
                "created_at": self.store.now(),
                "cover_images": [p["track"]["album"]["image"] for p in draft["picks"] if p["track"]["album"]["image"]][:4],
            }
        )
        moods = self.store["moods"]
        moods[mid] = draft
        self.store["moods"] = moods
        return draft

    def approve_mood(self, mood_id: str, name: str, description: str, track_ids: list[str], public: bool = False) -> dict:
        moods = self.store["moods"]
        d = moods[mood_id]
        keep = [p for p in d["picks"] if p["track"]["id"] in set(track_ids)]
        if not keep:
            raise ValueError("Pick at least one song.")
        clean_desc = " ".join(description.split())[:300]
        created = self.spotify.create_playlist(name.strip() or d["name"], clean_desc, public=public)
        self.spotify.add_to_playlist(created["id"], [p["track"]["uri"] for p in keep])
        images = [p["track"]["album"]["image"] for p in keep if p["track"]["album"]["image"]]
        cover_ok = False
        try:
            jpeg = make_cover(images, self.spotify.http)
            if jpeg:
                self.spotify.upload_cover(created["id"], jpeg)
                cover_ok = True
        except Exception:
            log.exception("cover upload failed; playlist still created")
        d.update(
            {
                "status": "created",
                "name": name,
                "description": clean_desc,
                "picks": keep,
                "playlist_id": created["id"],
                "playlist_url": (created.get("external_urls") or {}).get("spotify"),
                "cover_uploaded": cover_ok,
            }
        )
        self.store["moods"] = moods
        return d

    def discard_mood(self, mood_id: str) -> None:
        moods = self.store["moods"]
        moods[mood_id]["status"] = "discarded"
        self.store["moods"] = moods

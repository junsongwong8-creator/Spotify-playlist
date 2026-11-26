"""Core logic: scan new Liked Songs, suggest playlists, curate mood playlists.

Nothing here writes to Spotify except approve_* methods, which the page only
calls when you click Add / Create.
"""

from __future__ import annotations

import logging
import re
import threading
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from .config import Settings
from .cover import make_cover
from .spotify import SpotifyClient, artist_line
from .store import Store

log = logging.getLogger("organiser")

MATCH_BATCH = 25
MAX_LIBRARY_FOR_MOOD = 2500
PROFILE_SAMPLE = 30

# Mood words -> genre hints, for the simple (no-Claude) mood curator.
MOOD_HINTS = {
    "chill": ["chill", "lo-fi", "lofi", "ambient", "acoustic", "bedroom pop", "indie folk", "soft"],
    "calm": ["ambient", "acoustic", "piano", "chill", "folk"],
    "study": ["lo-fi", "lofi", "ambient", "classical", "instrumental", "post-rock", "jazz"],
    "focus": ["lo-fi", "lofi", "ambient", "classical", "instrumental", "post-rock", "jazz"],
    "sleep": ["ambient", "piano", "classical", "new age", "sleep"],
    "sad": ["sad", "singer-songwriter", "indie folk", "slowcore", "piano", "emo", "ballad"],
    "heartbreak": ["sad", "singer-songwriter", "r&b", "ballad", "soul"],
    "rain": ["indie folk", "singer-songwriter", "lo-fi", "jazz", "acoustic"],
    "happy": ["pop", "funk", "disco", "dance pop", "k-pop", "indie pop"],
    "summer": ["pop", "reggaeton", "tropical", "funk", "dance pop", "afrobeats"],
    "gym": ["edm", "hip hop", "rap", "trap", "drum and bass", "metal", "rock", "phonk"],
    "workout": ["edm", "hip hop", "rap", "trap", "drum and bass", "metal", "rock", "phonk"],
    "hype": ["edm", "hip hop", "rap", "trap", "house", "phonk"],
    "party": ["dance", "edm", "house", "pop", "hip hop", "reggaeton", "disco"],
    "romantic": ["r&b", "soul", "neo soul", "bossa nova", "love"],
    "love": ["r&b", "soul", "neo soul", "bossa nova", "love"],
    "night": ["r&b", "synthwave", "trip hop", "alternative r&b", "jazz", "dark"],
    "drive": ["synthwave", "indie rock", "alternative", "r&b", "city pop"],
    "angry": ["metal", "punk", "hardcore", "rap", "rock"],
    "throwback": ["classic", "oldies", "retro", "80s", "90s", "disco"],
}


def _year(track: Dict) -> str:
    return (track["album"].get("release_date") or "")[:4]


def track_line(track: Dict) -> str:
    year = _year(track)
    return "%s - %s | %s%s" % (track["name"], artist_line(track), track["album"]["name"], " (%s)" % year if year else "")


class Engine:
    def __init__(self, settings: Settings, spotify: SpotifyClient, store: Store, claude: Any = None) -> None:
        self.settings = settings
        self.spotify = spotify
        self.store = store
        self.claude = claude
        self.scan_lock = threading.Lock()

    # ---------- genres (one artist per call since Spotify removed the batch endpoint) ----------
    def genres_for(self, artist_ids: List[str], fetch_limit: int = 40) -> Dict[str, List[str]]:
        with self.store.lock:
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

    def track_genres(self, track: Dict) -> List[str]:
        g = self.genres_for([a["id"] for a in track["artists"]])
        return sorted({x for a in track["artists"] for x in g.get(a["id"], [])})

    # ---------- playlist profiles ----------
    def playlist_profiles(self) -> List[Dict]:
        cached = self.store["playlist_profiles"]
        profiles = []
        for p in self.spotify.my_playlists():
            hit = cached.get(p["id"])
            if hit and hit.get("snapshot_id") == p["snapshot_id"]:
                hit.update({"name": p["name"], "image": p["image"], "url": p["url"]})
                profiles.append(hit)
                continue
            tracks = self.spotify.playlist_tracks(p["id"])
            artist_counts: Counter = Counter()
            names = {}
            for t in tracks:
                for a in t["artists"]:
                    artist_counts[a["id"]] += 1
                    names[a["id"]] = a["name"]
            top_ids = [aid for aid, _ in artist_counts.most_common(8)]
            genres = self.genres_for(top_ids)
            genre_counts: Counter = Counter()
            for aid in top_ids:
                for g in genres.get(aid, []):
                    genre_counts[g] += artist_counts[aid]
            step = max(1, len(tracks) // PROFILE_SAMPLE)
            prof = dict(p)
            prof.update(
                {
                    "track_ids": [t["id"] for t in tracks],
                    "artist_ids": list(artist_counts),
                    "top_artists": [names[a] for a in top_ids],
                    "top_genres": [g for g, _ in genre_counts.most_common(10)],
                    "genre_weights": dict(genre_counts.most_common(25)),
                    "sample": [track_line(t) for t in tracks[::step][:PROFILE_SAMPLE]],
                }
            )
            cached[p["id"]] = prof
            profiles.append(prof)
        live = {p["id"] for p in profiles}
        self.store["playlist_profiles"] = {k: v for k, v in cached.items() if k in live}
        return profiles

    @staticmethod
    def profiles_text(profiles: List[Dict]) -> str:
        blocks = []
        for p in profiles:
            blocks.append(
                "\n".join(
                    [
                        "playlist_id: %s" % p["id"],
                        "name: %s" % p["name"],
                        "description: %s" % (p["description"] or "(none)"),
                        "songs: %s" % p["total"],
                        "top artists: %s" % (", ".join(p["top_artists"]) or "(unknown)"),
                        "top genres: %s" % (", ".join(p["top_genres"]) or "(unknown)"),
                        "sample songs:",
                    ]
                    + ["  - " + s for s in p["sample"]]
                )
            )
        return "\n\n".join(blocks)

    # ---------- matching ----------
    def simple_matches(self, track: Dict, profiles: List[Dict]) -> List[Dict]:
        """Shared genres (weighted by how central they are to the playlist) plus shared artists."""
        genres = set(self.track_genres(track))
        artist_ids = {a["id"] for a in track["artists"]}
        out = []
        for p in profiles:
            weights = p.get("genre_weights") or {}
            total = float(sum(weights.values()) or 1)
            genre_score = sum(w for g, w in weights.items() if g in genres) / total
            shared_artist = bool(artist_ids & set(p.get("artist_ids", [])))
            score = min(1.0, genre_score * 1.4 + (0.35 if shared_artist else 0.0))
            if score <= 0:
                continue
            bits = []
            if shared_artist:
                bits.append("already has songs by %s" % track["artists"][0]["name"])
            hits = [g for g in p.get("top_genres", []) if g in genres][:3]
            if hits:
                bits.append("same %s sound" % " / ".join(hits))
            reason = "; ".join(bits)
            out.append({"playlist_id": p["id"], "confidence": round(score, 2), "reason": reason[:1].upper() + reason[1:]})
        return out

    def match(self, tracks: List[Dict], profiles: List[Dict]) -> Dict[str, Dict]:
        usable = [p for p in profiles if p["sample"]]
        results: Dict[str, Dict] = {}
        if not usable:
            return results
        if self.claude is not None:
            text = self.profiles_text(usable)
            valid = {p["id"] for p in usable}
            for i in range(0, len(tracks), MATCH_BATCH):
                lines = []
                for t in tracks[i : i + MATCH_BATCH]:
                    g = self.track_genres(t)
                    lines.append("track_id: %s | %s%s" % (t["id"], track_line(t), " | genres: " + ", ".join(g) if g else ""))
                res = self.claude.match_tracks(text, "\n".join(lines))
                for tm in res.tracks:
                    results[tm.track_id] = {
                        "vibe": tm.vibe,
                        "matches": [
                            {"playlist_id": m.playlist_id, "confidence": max(0.0, min(1.0, m.confidence)), "reason": m.reason}
                            for m in tm.matches
                            if m.playlist_id in valid
                        ],
                    }
        else:
            for t in tracks:
                results[t["id"]] = {"vibe": "", "matches": self.simple_matches(t, usable)}
        return results

    # ---------- scanning ----------
    def scan(self, backfill: Optional[int] = None) -> Dict[str, Any]:
        """Look at new Liked Songs only and queue suggestions. Never writes to Spotify."""
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
                tracks = self.spotify.liked_tracks(since=cutoff.strftime("%Y-%m-%dT%H:%M:%SZ"), limit=200)
            known = {p["track"]["id"] for p in self.store["proposals"].values()}
            new_tracks = [t for t in tracks if t["id"] not in known]
            created = matched_count = 0
            if new_tracks:
                profiles = self.playlist_profiles()
                by_id = {p["id"]: p for p in profiles}
                matched = self.match(new_tracks, profiles)
                new_props = {}
                for t in new_tracks:
                    m = matched.get(t["id"], {"vibe": "", "matches": []})
                    suggestions = []
                    for s in sorted(m["matches"], key=lambda x: -x["confidence"]):
                        p = by_id.get(s["playlist_id"])
                        if not p or s["confidence"] < self.settings.min_match_confidence:
                            continue
                        if t["id"] in p.get("track_ids", []):
                            continue
                        suggestions.append(dict(s, playlist_name=p["name"], playlist_image=p["image"], playlist_url=p["url"], status="pending"))
                        if len(suggestions) >= self.settings.max_playlists_per_song:
                            break
                    pid = self.store.new_id()
                    new_props[pid] = {
                        "id": pid,
                        "track": t,
                        "genres": self.track_genres(t),
                        "vibe": m["vibe"],
                        "suggestions": suggestions,
                        "status": "pending" if suggestions else "no_match",
                        "created_at": self.store.now(),
                    }
                    created += 1
                    matched_count += bool(suggestions)
                with self.store.lock:
                    props = self.store["proposals"]
                    props.update(new_props)
                    self.store["proposals"] = props
            dated = [t["added_at"] for t in tracks if t.get("added_at")]
            if dated and (not since or max(dated) > since):
                self.store["last_seen_added_at"] = max(dated)
            self.store["last_scan"] = self.store.now()
            self.store["last_scan_error"] = None
            return {"status": "ok", "new_songs": created, "with_suggestions": matched_count}
        except Exception as e:
            log.exception("scan failed")
            self.store["last_scan_error"] = str(e)
            raise
        finally:
            self.scan_lock.release()

    # ---------- approvals ----------
    def approve_proposal(self, proposal_id: str, playlist_ids: List[str]) -> Dict[str, Any]:
        with self.store.lock:
            props = self.store["proposals"]
            p = props[proposal_id]
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
            self.store["proposals"] = props
        return {"added_to": added}

    def dismiss_proposal(self, proposal_id: str) -> None:
        with self.store.lock:
            props = self.store["proposals"]
            p = props[proposal_id]
            p["status"] = "dismissed"
            for s in p["suggestions"]:
                if s["status"] == "pending":
                    s["status"] = "skipped"
            self.store["proposals"] = props

    # ---------- mood playlists ----------
    def _simple_mood(self, library: List[Dict], mood: str, length: int) -> Dict[str, Any]:
        words = set(re.findall(r"[a-z&\-]+", mood.lower()))
        hints = set()
        for key, genres in MOOD_HINTS.items():
            if any(w.startswith(key) for w in words):
                hints.update(genres)
        hints.update(w for w in words if len(w) > 3)
        pool = library[:400]
        self.genres_for([a["id"] for t in pool for a in t["artists"]], fetch_limit=150)
        scored = []
        tag_counts: Counter = Counter()
        for t in pool:
            g = self.track_genres(t)
            text = " ".join(g + [t["name"], t["album"]["name"]]).lower()
            hit = [h for h in hints if h in text]
            if hit:
                scored.append((len(hit), t, g))
                tag_counts.update(x for x in g if any(h in x for h in hints))
        scored.sort(key=lambda x: -x[0])
        picks = [
            {"track": t, "reason": "From your likes · " + (", ".join(g[:2]) if g else "matches your mood"), "source": "library"}
            for _, t, g in scored[:length]
        ]
        tags = [g for g, _ in tag_counts.most_common(5)]
        name = " ".join(w.capitalize() for w in mood.split()[:4]) or "Mood Mix"
        return {
            "name": name,
            "description": "%d songs from your Liked Songs for when you're feeling %s." % (len(picks), mood.strip()),
            "blurb": ("A mix of %s from your library." % ", ".join(tags[:3])) if tags else "Songs from your library that match your mood.",
            "vibe_tags": tags,
            "picks": picks,
        }

    def curate_mood(self, mood: str, length: int = 25) -> Dict[str, Any]:
        library = self.spotify.liked_tracks(limit=MAX_LIBRARY_FOR_MOOD)
        by_id = {t["id"]: t for t in library}
        if self.claude is not None:
            plan = self.claude.curate_mood("\n".join("%s | %s" % (t["id"], track_line(t)) for t in library), mood, length)
            picks, seen = [], set()
            for pick in plan.picks:
                t = by_id.get(pick.library_track_id or "")
                source = "library"
                if t is None:
                    t, source = self.spotify.search_track(pick.title, pick.artist), "new"
                if t is None or t["id"] in seen:
                    continue
                seen.add(t["id"])
                picks.append({"track": t, "reason": pick.reason, "source": source})
            draft = {"name": plan.name, "description": plan.description[:300], "blurb": plan.blurb, "vibe_tags": plan.vibe_tags, "picks": picks}
        else:
            draft = self._simple_mood(library, mood, length)
        mid = self.store.new_id()
        draft.update({"id": mid, "mood": mood, "status": "draft", "created_at": self.store.now()})
        with self.store.lock:
            moods = self.store["moods"]
            moods[mid] = draft
            self.store["moods"] = moods
        return draft

    def approve_mood(self, mood_id: str, name: str, description: str, track_ids: List[str], public: bool = False) -> Dict[str, Any]:
        d = self.store["moods"][mood_id]
        order = {tid: i for i, tid in enumerate(track_ids)}
        keep = sorted((p for p in d["picks"] if p["track"]["id"] in order), key=lambda p: order[p["track"]["id"]])
        if not keep:
            raise ValueError("Keep at least one song.")
        desc = " ".join(description.split())[:300]
        created = self.spotify.create_playlist(name.strip() or d["name"], desc, public=public)
        self.spotify.add_to_playlist(created["id"], [p["track"]["uri"] for p in keep])
        cover_ok = False
        try:
            jpeg = make_cover([p["track"]["album"]["image"] for p in keep], self.spotify.http)
            if jpeg:
                self.spotify.upload_cover(created["id"], jpeg)
                cover_ok = True
        except Exception:
            log.exception("cover upload failed; playlist was still created")
        d.update(
            {
                "status": "created",
                "name": name.strip() or d["name"],
                "description": desc,
                "picks": keep,
                "playlist_id": created["id"],
                "playlist_url": (created.get("external_urls") or {}).get("spotify"),
                "cover_uploaded": cover_ok,
            }
        )
        with self.store.lock:
            moods = self.store["moods"]
            moods[mood_id] = d
            self.store["moods"] = moods
        return d

    def discard_mood(self, mood_id: str) -> None:
        with self.store.lock:
            moods = self.store["moods"]
            moods[mood_id]["status"] = "discarded"
            self.store["moods"] = moods

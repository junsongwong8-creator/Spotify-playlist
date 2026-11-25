"""Claude helpers: structured vibe matching and mood curation."""

from __future__ import annotations

from typing import Optional, TypeVar

import anthropic
from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


# ---------- Schemas ----------
class PlaylistMatch(BaseModel):
    playlist_id: str
    confidence: float  # 0.0 - 1.0
    reason: str  # one short sentence, written for the listener


class TrackMatches(BaseModel):
    track_id: str
    vibe: str  # 3-8 word description of the song's feel
    matches: list[PlaylistMatch]


class MatchResult(BaseModel):
    tracks: list[TrackMatches]


class MoodPick(BaseModel):
    library_track_id: Optional[str]  # id from the library list, or null for a new find
    title: str
    artist: str
    reason: str


class MoodPlaylist(BaseModel):
    name: str
    description: str  # <= 280 chars, goes on Spotify
    blurb: str  # longer write-up shown in the approval page
    vibe_tags: list[str]
    picks: list[MoodPick]


MATCH_SYSTEM = """You sort a listener's newly liked songs into their existing Spotify playlists by vibe.

Each playlist comes with its name, description, most common artists and genres, and a sample of its songs. Infer what each playlist is *for* (mood, energy, setting, era, language, genre) from all of that together, not from the name alone.

For each new song, use what you know about the song and artist to judge its feel, then list only the playlists it would genuinely fit, with a confidence from 0 to 1:
- 0.85+ : clearly belongs; it would not feel out of place next to the sample songs
- 0.6-0.85 : good fit with a minor difference (e.g. slightly different energy)
- below 0.6 : leave it out
It is fine, and common, for a song to fit no playlist. Do not force matches. If you do not recognize a song, judge from the artist, album, genres and title, and lower your confidence.

Write each reason as one short, specific sentence for the listener (e.g. "Same hazy late-night R&B as the Frank Ocean and Daniel Caesar tracks here"). Use the exact playlist_id and track_id values given."""

MOOD_SYSTEM = """You are a music curator building a Spotify playlist for a specific mood the listener describes.

You get the listener's Liked Songs library (one per line: id | title - artist | album (year)). Build the playlist mostly from that library so it feels personal, then add a few new finds that fit and that the listener would likely enjoy given their taste.

Guidelines:
- Aim for the requested length. Roughly 70-85% library songs, the rest new finds, unless the listener asks otherwise.
- For a library song, set library_track_id to its exact id. For a new find, set library_track_id to null and give the exact official title and primary artist so it can be found on Spotify search. Only suggest real songs you are confident exist.
- Order the songs so the playlist flows (a gentle arc in energy, no jarring jumps).
- name: short and evocative, 2-5 words, no emoji.
- description: one or two sentences, at most 280 characters, no line breaks. It goes on Spotify.
- blurb: 2-4 sentences describing the mood, the sound, and the arc of the playlist.
- vibe_tags: 3-6 short tags (e.g. "late night", "warm synths", "slow build").
- reason: one short line on why that song belongs."""


class Claude:
    def __init__(self, api_key: str, model: str) -> None:
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model

    def _parse(self, schema: type[T], system: list[dict], user: str, effort: str) -> T:
        response = self.client.beta.messages.parse(
            model=self.model,
            max_tokens=16000,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_format=schema,
            output_config={"effort": effort},
            thinking={"type": "adaptive"},
            # If a safety classifier declines, retry on a fallback model automatically.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        if response.stop_reason == "refusal":
            raise RuntimeError("Claude declined this request. Try rewording the mood.")
        if response.stop_reason == "max_tokens" or response.parsed_output is None:
            raise RuntimeError("Claude's answer was cut off. Try a shorter request.")
        return response.parsed_output

    def match_tracks(self, playlists_text: str, tracks_text: str) -> MatchResult:
        system = [
            {"type": "text", "text": MATCH_SYSTEM},
            {
                "type": "text",
                "text": "The listener's playlists:\n\n" + playlists_text,
                "cache_control": {"type": "ephemeral"},
            },
        ]
        user = "Newly liked songs to sort:\n\n" + tracks_text
        return self._parse(MatchResult, system, user, effort="medium")

    def curate_mood(self, library_text: str, mood: str, length: int) -> MoodPlaylist:
        system = [
            {"type": "text", "text": MOOD_SYSTEM},
            {
                "type": "text",
                "text": "The listener's Liked Songs library:\n\n" + library_text,
                "cache_control": {"type": "ephemeral"},
            },
        ]
        user = f"Mood / request: {mood}\nTarget length: about {length} songs."
        return self._parse(MoodPlaylist, system, user, effort="medium")

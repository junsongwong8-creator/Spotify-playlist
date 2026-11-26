"""Claude-powered vibe matching and mood curation (Python 3.10+, only used when ANTHROPIC_API_KEY is set)."""

from __future__ import annotations

from typing import List, Optional, Type, TypeVar

import anthropic
from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class PlaylistMatch(BaseModel):
    playlist_id: str
    confidence: float  # 0.0 - 1.0
    reason: str


class TrackMatches(BaseModel):
    track_id: str
    vibe: str  # 3-8 word feel of the song
    matches: List[PlaylistMatch]


class MatchResult(BaseModel):
    tracks: List[TrackMatches]


class MoodPick(BaseModel):
    library_track_id: Optional[str]  # id from the library list, or null for a new find
    title: str
    artist: str
    reason: str


class MoodPlaylist(BaseModel):
    name: str
    description: str
    blurb: str
    vibe_tags: List[str]
    picks: List[MoodPick]


MATCH_SYSTEM = """You sort a listener's newly liked songs into their existing Spotify playlists by vibe. Be selective: the listener was annoyed that an earlier tool put every song into too many playlists.

Each playlist comes with its name, description, top artists, genres and a sample of its songs. Work out what each playlist is *for* (mood, energy, setting, era, language, genre) from the sample songs above all; names can be misleading.

For each new song, judge its feel from what you know about the song and artist, then return:
- the single best-fitting playlist, only if it genuinely belongs there (it would not feel out of place next to the sample songs);
- a second playlist only if it fits that one just as clearly;
- no playlists at all when nothing is a clear fit. That is a normal, good answer.

Confidence: 0.85+ clearly belongs; 0.7-0.85 good fit with a minor difference; below 0.7 do not include it. If you don't recognise a song, judge from artist, album, genres and title, and lower your confidence.

Write each reason as one short, specific sentence for the listener (e.g. "Same hazy late-night R&B as the Daniel Caesar and SZA songs here"). Write vibe as 3-8 vivid words. Use the exact playlist_id and track_id values given."""

MOOD_SYSTEM = """You are a music curator building a Spotify playlist for the mood the listener describes.

You get their Liked Songs library (one per line: id | title - artist | album (year)). Build the playlist mostly from that library so it feels personal, then add a few new finds they would likely enjoy given their taste.

- Aim for the requested length: roughly 70-85% library songs, the rest new finds, unless they ask otherwise.
- Library song: set library_track_id to its exact id. New find: library_track_id null, with the exact official title and primary artist so Spotify search finds it. Only suggest real songs you are sure exist.
- Order the songs so the playlist flows, with a gentle arc in energy and no jarring jumps.
- name: short and evocative, 2-5 words, no emoji.
- description: one or two sentences, at most 280 characters, no line breaks (it goes on Spotify).
- blurb: 2-4 vivid sentences on the mood, the sound and the arc.
- vibe_tags: 3-6 short tags (e.g. "late night", "warm synths", "slow build").
- reason: one short line on why the song belongs."""


class Claude:
    def __init__(self, api_key: str, model: str) -> None:
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model

    def _parse(self, schema: Type[T], system: list, user: str) -> T:
        response = self.client.beta.messages.parse(
            model=self.model,
            max_tokens=16000,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_format=schema,
            output_config={"effort": "medium"},
            thinking={"type": "adaptive"},
            # If a safety classifier declines, the API retries on a fallback model automatically.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        if response.stop_reason == "refusal":
            raise RuntimeError("Claude declined this request. Try rewording it.")
        if response.stop_reason == "max_tokens" or response.parsed_output is None:
            raise RuntimeError("Claude's answer was cut off. Try a shorter playlist.")
        return response.parsed_output

    def match_tracks(self, playlists_text: str, tracks_text: str) -> MatchResult:
        system = [
            {"type": "text", "text": MATCH_SYSTEM},
            {"type": "text", "text": "The listener's playlists:\n\n" + playlists_text, "cache_control": {"type": "ephemeral"}},
        ]
        return self._parse(MatchResult, system, "Newly liked songs to sort:\n\n" + tracks_text)

    def curate_mood(self, library_text: str, mood: str, length: int) -> MoodPlaylist:
        system = [
            {"type": "text", "text": MOOD_SYSTEM},
            {"type": "text", "text": "The listener's Liked Songs library:\n\n" + library_text, "cache_control": {"type": "ephemeral"}},
        ]
        return self._parse(MoodPlaylist, system, "Mood / request: %s\nTarget length: about %d songs." % (mood, length))

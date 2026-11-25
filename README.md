# Spotify Playlist Bot

A small app that runs on your computer and:

1. **Watches your Liked Songs.** When you like a song, it suggests which of your existing playlists it fits, with a confidence score and a one-line reason ("Same hazy late-night R&B as the Frank Ocean tracks here").
2. **Curates mood playlists on request.** Type a mood ("rainy Sunday, slow and warm"), and it drafts a playlist from your library plus a few new finds, with a name, description, write-up, tags, album art for every song, and a 2x2 album-art cover.

**Nothing changes in your Spotify until you click Approve** in the web page.

## How "vibe" is judged

Spotify removed mood data (energy, danceability, etc.) and recommendations for new apps in late 2024. So the bot builds a profile of each playlist (its name, description, top artists, genres, and a sample of songs) and:

- **With an Anthropic API key (recommended):** Claude compares each new song against those profiles and explains its picks. It also curates mood playlists. Typical cost is a few cents per scan or mood playlist.
- **Without a key:** it falls back to matching on shared genres and artists. Simpler, less accurate.

## Setup (one time)

You need Python 3.10+ and a Spotify **Premium** account (Spotify requires Premium for developer apps).

### 1. Create a Spotify developer app
1. Go to <https://developer.spotify.com/dashboard> and click **Create app**.
2. Any name and description. **Redirect URI:** `http://127.0.0.1:8888/callback`
3. Tick **Web API**, save, then open **Settings** and copy the **Client ID** and **Client Secret**.

### 2. Install
```bash
git clone https://github.com/ykwong2002/spotify-playlist-bot.git
cd spotify-playlist-bot
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # Windows: copy .env.example .env
```

Open `.env` and fill in `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`, and (optional) `ANTHROPIC_API_KEY` from <https://console.anthropic.com/settings/keys>.

### 3. Run
```bash
python -m bot
```
Open <http://127.0.0.1:8888>, click **Log in with Spotify**, and approve access. The bot checks your Liked Songs every 15 minutes while it runs (first check looks back 7 days; use **Check my last 50 likes** to go further back).

## Using it

- **New likes tab:** each new song shows its cover, artist, album, a short vibe description, and suggested playlists. Strong matches are pre-ticked. Untick any you don't want, then **Add to selected playlists**, or **Skip this song**.
- **Mood playlists tab:** describe a mood, pick a length, and click **Curate playlist**. Review the draft: edit the name and description, untick songs, then **Approve & create in Spotify**. It's created as a private playlist (tick *Make public* to change that) with the album-art cover uploaded.

## Optional: scan without the page open
`python -m bot scan` runs one check and saves suggestions for review next time you open the page. You can schedule it with cron (macOS/Linux) or Task Scheduler (Windows).

## Settings (`.env`)
| Setting | Default | Meaning |
|---|---|---|
| `WATCH_INTERVAL_MINUTES` | 15 | How often to check Liked Songs while running |
| `FIRST_SCAN_DAYS` | 7 | How far back the very first check looks |
| `MIN_MATCH_CONFIDENCE` | 0.6 | Hide suggestions below this fit score |
| `CLAUDE_MODEL` | `claude-opus-5-5` | Claude model used for matching and curation |

## Notes and limits
- The bot can only add songs to playlists you own or collaborate on (a Spotify rule).
- Your login token and the bot's state live in `data/` on your computer; `.env` and `data/` are git-ignored.
- Spotify's development-mode apps allow up to 5 users, so this is meant for personal use.

## Development
```bash
pip install pytest
python -m pytest -q
```
Tests use an in-memory fake of Spotify and Claude, so they make no network calls.

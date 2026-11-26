# Spotify Playlist Organiser

A local app that keeps your Spotify playlists tidy, and never changes anything until you approve it.

- **Review likes.** Each new liked song appears as a big card with its album art, a vibe line, a playable preview, and the *one* playlist it fits best, with a fit score and a reason. Swipe or drag the card right to add it, or left to skip. You can also use the arrow keys.
- **Mood studio.** Pick a mood chip (or type your own, e.g. "slow Sunday morning, warm acoustic") and get a draft playlist: a name, a description, a short write-up, vibe tags and album art for every song, plus a cover built from four album covers. Play songs, drag them to reorder, remove or restore them, and edit the name and description. Then click **Create in Spotify**.

## Setup

You need a Spotify **Premium** account, because Spotify requires it for developer apps. Python 3.9 works. Python 3.10 or newer enables the much better Claude matching.

### 1. Spotify developer app
1. Go to <https://developer.spotify.com/dashboard> and open your app, or click **Create app**.
2. Under **Redirect URIs**, add `http://127.0.0.1:8765/callback`, tick **Web API** and save.
3. Copy the **Client ID**. You don't need the client secret.

### 2. Install
```bash
git clone https://github.com/junsongwong8-creator/Spotify-playlist.git
cd Spotify-playlist
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install --upgrade pip
pip install -r requirements.txt
cp .env.example .env               # Windows: copy .env.example .env
```
Open `.env` and set `SPOTIFY_CLIENT_ID`. To get Claude vibe matching and smarter mood playlists, also set `ANTHROPIC_API_KEY` (from <https://console.anthropic.com/settings/keys>). This needs Python 3.10+. On macOS, `brew install python@3.12` and use `python3.12 -m venv .venv` above.

### 3. Run
```bash
python app.py
```
Open <http://127.0.0.1:8765> and click **Connect Spotify**. The first check runs straight away and looks back 14 days. After that, the app checks for new likes every hour while it's running. **Look at my last 50 likes** goes further back.

## How matching works

The app builds a profile of each playlist you own: its name, its description, its most common artists and genres, and a sample of its songs.

- **Claude matching** (API key + Python 3.10+): Claude judges each new song against those profiles and is told to be selective. It picks the single best playlist, or none when nothing clearly fits. Expect a few cents per check or mood playlist.
- **Simple matching** (otherwise): shared genres, weighted by how central they are to the playlist, plus shared artists. A playlist's name alone never triggers a suggestion, which was the cause of the old "every song goes everywhere" problem.

Songs that don't clearly fit anywhere are listed separately instead of being forced into a playlist.

## Settings (`.env`)
| Setting | Default | Meaning |
|---|---|---|
| `MAX_PLAYLISTS_PER_SONG` | 1 | Set to 2 to allow a second, equally strong suggestion |
| `MIN_MATCH_CONFIDENCE` | 0.7 | Hide suggestions below this fit score |
| `SCAN_INTERVAL_MINUTES` | 60 | How often to check for new likes while running |
| `FIRST_SCAN_DAYS` | 14 | How far back the very first check looks |
| `SPOTIFY_REDIRECT_URI` | `http://127.0.0.1:8765/callback` | Must match your Spotify app |

## Notes
- `python app.py scan` runs one check without opening the page, for example from a scheduler.
- Only playlists you own or collaborate on are considered, because Spotify only lets apps add to those.
- Your Spotify login and the app's state live in `organiser.db` next to `app.py`. Don't share or commit it (it's git-ignored).
- Song previews use Spotify's embed player, so you need to be logged in to Spotify in your browser for full playback.

## Development
```bash
pip install pytest && python -m pytest -q
```
The tests use an in-memory fake Spotify and a fake Claude, and run on Python 3.9+.

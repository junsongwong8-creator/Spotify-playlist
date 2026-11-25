"""Run with:  python -m bot            (web app at http://127.0.0.1:8888)
            python -m bot scan       (one-off scan, e.g. from cron; proposals wait in the web app)"""

import logging
import sys
import urllib.parse

import uvicorn

from .config import load_settings


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = load_settings()
    if len(sys.argv) > 1 and sys.argv[1] == "scan":
        from .app import build_engine

        engine = build_engine(settings)
        if not engine.spotify.logged_in:
            sys.exit("Not logged in. Run `python -m bot` and log in through the web page first.")
        print(engine.scan())
        return
    parsed = urllib.parse.urlparse(settings.spotify_redirect_uri)
    host, port = parsed.hostname or "127.0.0.1", parsed.port or 8888
    print(f"\n  Spotify Playlist Bot running at http://{host}:{port}\n")
    from .app import create_app

    uvicorn.run(create_app(), host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()

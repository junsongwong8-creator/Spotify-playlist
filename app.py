"""Spotify Playlist Organiser.

    python app.py         start the page at http://127.0.0.1:8765
    python app.py scan    run one check of your Liked Songs (e.g. from a scheduler)
"""

import logging
import sys
from urllib.parse import urlparse

if sys.version_info < (3, 9):
    sys.exit("Playlist Organiser needs Python 3.9 or newer.")

from organiser.config import load_settings  # noqa: E402


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    settings = load_settings()
    from organiser.web import build_engine, create_app, start_scanner

    if len(sys.argv) > 1 and sys.argv[1] == "scan":
        engine = build_engine(settings)
        if not engine.spotify.logged_in:
            sys.exit("Not connected yet. Run `python app.py`, open the page and click Connect Spotify first.")
        print(engine.scan())
        return

    engine = build_engine(settings)
    if settings.claude_note:
        print("Note: " + settings.claude_note)
    app = create_app(engine)
    start_scanner(engine)
    url = urlparse(settings.redirect_uri)
    host, port = url.hostname or "127.0.0.1", url.port or 8765
    print("\n  Playlist Organiser is running: open http://%s:%d\n" % (host, port))
    app.run(host=host, port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()

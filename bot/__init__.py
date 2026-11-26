import sys

if sys.version_info < (3, 11):
    sys.exit(
        f"Spotify Playlist Bot needs Python 3.11 or newer, but this is Python "
        f"{sys.version_info.major}.{sys.version_info.minor}.\n"
        "Install a newer Python (macOS: `brew install python@3.12` or https://www.python.org/downloads/),\n"
        "then recreate the virtual environment:  rm -rf .venv && python3.12 -m venv .venv"
    )

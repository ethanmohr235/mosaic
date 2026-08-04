# ui/app.py
from pathlib import Path

import webview

from ui.api import Api

FRONTEND_DIR = Path(__file__).resolve().parent / "frontend"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
WEBVIEW_STORAGE_DIR = PROJECT_ROOT / ".webview_data"


def launch(garden: str | None = None) -> None:
    api = Api(garden)
    window = webview.create_window(
        "MonOSS",
        url=str(FRONTEND_DIR / "index.html"),
        js_api=api,
        width=1280,
        height=800,
        min_size=(900, 600),
    )
    api._window = window
    window.events.closed += api.shutdown
    # http_server=True: serve the frontend over http://127.0.0.1 instead of
    # file://. WebKit disables localStorage for file:// pages (no origin to
    # partition storage by), which app.js relies on to remember the last-
    # focused mote per garden and each node's layout position.
    #
    # private_mode=False so storage survives across app restarts. Without
    # an explicit storage_path, pywebview falls back to WebKit's ambient
    # default context (shared across whatever else on the system uses it)
    # rather than a location dedicated to this app -- storage_path pins it
    # to our own project directory instead, same spirit as gardens/.
    webview.start(http_server=True, private_mode=False, storage_path=str(WEBVIEW_STORAGE_DIR))

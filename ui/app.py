# ui/app.py
from pathlib import Path

import webview

from ui.api import Api

FRONTEND_DIR = Path(__file__).resolve().parent / "frontend"


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
    window.events.closed += api.shutdown
    # http_server=True: serve the frontend over http://127.0.0.1 instead of
    # file://. WebKit disables localStorage for file:// pages (no origin to
    # partition storage by), which app.js relies on to remember the last-
    # focused mote per garden. private_mode=False so that storage actually
    # survives across app restarts instead of being wiped like incognito.
    webview.start(http_server=True, private_mode=False)

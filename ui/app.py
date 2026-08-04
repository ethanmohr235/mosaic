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
    webview.start()

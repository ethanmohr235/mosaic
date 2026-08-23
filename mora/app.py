# mora/app.py
"""The Mora window. ui/app.py's twin, and deliberately so -- same
pywebview settings, same cache workaround, different frontend and
different Api.

A separate window rather than a view inside the main app: Mora opens on
its own garden connection and closes independently, and a note editor
that can only exist while the graph window is open is a note editor you
cannot put on a second monitor.
"""
import logging
import shutil
from pathlib import Path

import webview

from mora.api import Api

FRONTEND_DIR = Path(__file__).resolve().parent / "frontend"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
WEBVIEW_STORAGE_DIR = PROJECT_ROOT / ".webview_data"

logger = logging.getLogger(__name__)


def _clear_http_cache() -> None:
    """Drop WebKit's HTTP cache before the window opens -- see
    ui/app.py::_clear_http_cache for the bug this prevents. pywebview
    serves from a fixed port, so an edited frontend file otherwise
    silently doesn't appear.

    Only `cache/` goes; the sibling `localstorage/` holds per-garden
    state both windows rely on."""
    shutil.rmtree(WEBVIEW_STORAGE_DIR / "cache", ignore_errors=True)


def launch(garden: str | None = None, note: str | None = None) -> None:
    _clear_http_cache()
    api = Api(garden, note)
    window = webview.create_window(
        "Mora",
        url=str(FRONTEND_DIR / "index.html"),
        js_api=api,
        width=1440,
        height=900,
        min_size=(1000, 620),
    )
    api._window = window
    window.events.closed += api.shutdown
    webview.start(http_server=True, private_mode=False, storage_path=str(WEBVIEW_STORAGE_DIR))

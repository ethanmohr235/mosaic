# mora/app.py
"""The Mora window. monarch/app.py's twin, and deliberately so -- same
pywebview settings, same cache workaround, different frontend and
different Api. window_launcher.py carries the settings and workaround
all six apps share.

A separate window rather than a view inside the main app: Mora opens on
its own garden connection and closes independently, and a note editor
that can only exist while Monarch is open is a note editor you
cannot put on a second monitor.
"""
from pathlib import Path

from mora.api import Api
from window_launcher import launch_window

FRONTEND_DIR = Path(__file__).resolve().parent / "frontend"
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Its own storage directory, next to but never inside the graph
# window's. Both apps run at once by design (monarch/api.py::open_mora), and
# WebKit's cache/localstorage under one path is not something two
# processes should be sharing -- least of all when launch_window()'s
# cache-clearing deletes it on startup, which would have been deleting
# the other app's cache out from under a running window.
WEBVIEW_STORAGE_DIR = PROJECT_ROOT / ".webview_data_mora"

# pywebview pins its built-in server to settings['DEFAULT_HTTP_PORT'] --
# 42001 -- whenever private_mode is off and no port is given. The graph
# window takes that one. Mora asking for it too is not a near miss: the
# second process fails to bind, and its window then loads *the first
# one's* page off 42001 and calls the wrong bridge. That is exactly what
# happened -- a window titled Mora showing the Monarch graph, failing on
# `api_reserved_names is not a function`. window_launcher.choose_port()
# is the fallback that prevents it from costing the window too.
DEFAULT_HTTP_PORT = 42002


def launch(garden: str | None = None, note: str | None = None) -> None:
    api = Api(garden, note)
    launch_window(
        name="Mora",
        frontend_dir=FRONTEND_DIR,
        storage_dir=WEBVIEW_STORAGE_DIR,
        api=api,
        default_port=DEFAULT_HTTP_PORT,
        width=1440,
        height=900,
        min_size=(1000, 620),
    )

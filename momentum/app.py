# momentum/app.py
"""The Momentum window. monastery/app.py's twin, for the same reasons it
is monarch/app.py's -- same pywebview settings, same cache workaround,
different frontend and different Api. window_launcher.py carries the
settings and workaround all four apps share.

A fourth window rather than a tab inside another: a board wants the
width of a monitor, and a timer you can only see while the graph window
is open is a timer you cannot leave running on a second screen. It opens
on its own garden connection and closes independently.
"""
from pathlib import Path

from momentum.api import Api
from window_launcher import launch_window

FRONTEND_DIR = Path(__file__).resolve().parent / "frontend"
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Its own storage directory, a sibling of the other three windows' and
# never inside any of them. All four run at once by design, and WebKit's
# cache/localstorage under one path is not something four processes
# should share -- least of all when launch_window()'s cache-clearing
# deletes it on startup, which would be deleting a running window's
# cache.
WEBVIEW_STORAGE_DIR = PROJECT_ROOT / ".webview_data_momentum"

# Monarch takes pywebview's default 42001, Mora 42002 and Monastery
# 42003. A fourth app asking for any of those is not a near miss: the
# process that loses the bind does not fail visibly -- it points its
# window at the winner's address and calls the wrong bridge. That
# shipped once already as a window titled Mora showing the Monarch graph
# (see mora/app.py). window_launcher.choose_port() is the fallback that
# prevents it from costing the window too.
DEFAULT_HTTP_PORT = 42004


def launch(garden: str | None = None, focus: str | None = None) -> None:
    api = Api(garden, focus)
    launch_window(
        name="Momentum",
        frontend_dir=FRONTEND_DIR,
        storage_dir=WEBVIEW_STORAGE_DIR,
        api=api,
        default_port=DEFAULT_HTTP_PORT,
        width=1440,
        height=900,
        min_size=(1040, 640),
    )

# monastery/app.py
"""The Monastery window. mora/app.py's twin, for the same reasons it is
monarch/app.py's -- same pywebview settings, same cache workaround,
different frontend and different Api. window_launcher.py carries the
settings and workaround all six apps share.

A third window rather than a view inside the main app: a calendar wants
the whole width of a monitor, and one that can only exist while the graph
window is open is a calendar you cannot leave up on a second screen. It
opens on its own garden connection and closes independently.
"""
from pathlib import Path

from monastery.api import Api
from window_launcher import launch_window

FRONTEND_DIR = Path(__file__).resolve().parent / "frontend"
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Its own storage directory, a sibling of the other two windows' and
# never inside either. All three run at once by design, and WebKit's
# cache/localstorage under one path is not something three processes
# should share -- least of all when launch_window()'s cache-clearing
# deletes it on startup, which would be deleting a running window's
# cache.
WEBVIEW_STORAGE_DIR = PROJECT_ROOT / ".webview_data_monastery"

# Monarch takes pywebview's default 42001 and Mora takes 42002.
# A third app asking for either is not a near miss: the process that
# loses the bind does not fail visibly -- it points its window at the
# winner's address and calls the wrong bridge. That shipped once already
# as a window titled Mora showing the Monarch graph (see mora/app.py).
# window_launcher.choose_port() is the fallback that prevents it from
# costing the window too.
DEFAULT_HTTP_PORT = 42003


def launch(garden: str | None = None, focus: str | None = None) -> None:
    api = Api(garden, focus)
    launch_window(
        name="Monastery",
        frontend_dir=FRONTEND_DIR,
        storage_dir=WEBVIEW_STORAGE_DIR,
        api=api,
        default_port=DEFAULT_HTTP_PORT,
        width=1440,
        height=900,
        min_size=(900, 640),
    )

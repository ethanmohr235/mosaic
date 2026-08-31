# monolith/app.py
"""The Monolith window. mora/app.py's twin, and deliberately so -- same
pywebview settings, same cache workaround, different frontend and
different Api. window_launcher.py carries the settings and workaround
all five apps share.

Wider than the others by default: this one draws a table, and a table
that has to be scrolled sideways on first open is a table that has
already lost the comparison it exists to make.
"""
from pathlib import Path

from monolith.api import Api
from window_launcher import launch_window

FRONTEND_DIR = Path(__file__).resolve().parent / "frontend"
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Its own storage directory, next to but never inside the other windows'
# -- launch_window()'s cache-clearing deletes it on startup, and all five
# apps are meant to run at once.
WEBVIEW_STORAGE_DIR = PROJECT_ROOT / ".webview_data_monolith"

# 42001-42004 belong to Monarch, Mora, Monastery and Momentum. Asking for
# a port another window already holds is not a near miss: the loser fails
# to bind and then loads *the winner's* page against its own bridge --
# see window_launcher.choose_port, and mora/app.py for the time that
# actually shipped.
DEFAULT_HTTP_PORT = 42005


def launch(garden: str | None = None, view: str | None = None) -> None:
    api = Api(garden, view)
    launch_window(
        name="Monolith",
        frontend_dir=FRONTEND_DIR,
        storage_dir=WEBVIEW_STORAGE_DIR,
        api=api,
        default_port=DEFAULT_HTTP_PORT,
        width=1560,
        height=940,
        min_size=(1080, 620),
    )

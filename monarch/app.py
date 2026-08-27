# monarch/app.py
"""The Monarch window: the graph itself, and the first of the three.

Mora (mora/app.py) and Monastery (monastery/app.py) are its twins --
same pywebview settings, same cache workaround, different frontend and
different Api. Each is its own window on the same gardens rather than a
pane inside this one, so any of them can be closed, moved to a second
monitor, or opened alone. window_launcher.py carries the settings and
workaround all four share.
"""
from pathlib import Path

from monarch.api import Api
from window_launcher import launch_window

FRONTEND_DIR = Path(__file__).resolve().parent / "frontend"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
# Its own storage directory, a sibling of Mora's and Monastery's and
# never a parent of either. All three run at once by design, and
# launch_window()'s cache-clearing deletes what is under this path on
# startup -- a shared directory would mean deleting a running window's
# cache.
WEBVIEW_STORAGE_DIR = PROJECT_ROOT / ".webview_data_monarch"

# pywebview's own DEFAULT_HTTP_PORT, used whenever private_mode is off
# and no port is given. Mora and Monastery ask for 42002 and 42003 and
# their own storage directories instead -- see window_launcher.py for
# what happens to whichever of these apps loses a collision.
DEFAULT_HTTP_PORT = 42001


def launch(garden: str | None = None) -> None:
    api = Api(garden)
    launch_window(
        name="Monarch",
        frontend_dir=FRONTEND_DIR,
        storage_dir=WEBVIEW_STORAGE_DIR,
        api=api,
        default_port=DEFAULT_HTTP_PORT,
        width=1280,
        height=800,
        min_size=(900, 600),
    )

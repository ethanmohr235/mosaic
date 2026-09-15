# gardens_app/app.py
"""The Gardens window: create, rename, delete, import, export and sync
the gardens the other six windows open.

A window of its own rather than a modal inside each app, and its own
process rather than a second window on one of them. Garden management
was Monarch's alone (a modal only that window had), which meant the
answer to "make a new garden from Motion" was "open Monarch". Six copies
of that modal would have been the other way to fix it; one window all
six can open is this way, and it is the same choice the six apps already
make about each other -- see api_common.open_app.

Standalone matters here specifically because of what this window does to
gardens: a manager that lived inside an app would be managing, among
others, the garden that app is holding open. This one holds none (see
api.py), so renaming or deleting the garden you were just looking at is
an ordinary operation rather than a special case to refuse.

Smaller than the app windows on purpose -- it is a list and a detail
pane, and it is meant to sit open beside the window that opened it.
"""
from pathlib import Path

from gardens_app.api import Api
from window_launcher import launch_window

FRONTEND_DIR = Path(__file__).resolve().parent / "frontend"
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Its own storage directory, never inside another window's --
# launch_window() clears the HTTP cache under this path on startup, and
# this window is by definition open alongside others.
WEBVIEW_STORAGE_DIR = PROJECT_ROOT / ".webview_data_gardens"

# 42001-42006 belong to Monarch, Mora, Monastery, Momentum, Monolith and
# Motion. Seventh window, seventh port -- and see window_launcher.choose_port
# for why asking for a port somebody else holds is worse than it sounds.
DEFAULT_HTTP_PORT = 42007


def launch(select: str | None = None, open_sync: bool = False) -> None:
    api = Api(select, open_sync)
    launch_window(
        name="Gardens",
        frontend_dir=FRONTEND_DIR,
        storage_dir=WEBVIEW_STORAGE_DIR,
        api=api,
        default_port=DEFAULT_HTTP_PORT,
        width=940,
        height=620,
        min_size=(760, 480),
    )

# moxie/app.py
"""The Moxie window. motion/app.py's twin, and deliberately so -- same
pywebview settings, same cache workaround, different frontend and
different Api. window_launcher.py carries the settings and workaround all
six apps share.

Sized like Motion: a grid of plots with a mote rail on one side and a
bindings rail on the other needs the width, and a plot squeezed below
about 300px stops being a reading and becomes a squiggle. The frontend
drops the grid to one column below that (see the layout notes in
frontend/app.js), but the default should not need it to.
"""
from pathlib import Path

from moxie.api import Api
from window_launcher import launch_window

FRONTEND_DIR = Path(__file__).resolve().parent / "frontend"
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Its own storage directory, next to but never inside the other windows'
# -- launch_window()'s cache-clearing deletes it on startup, and all
# six apps are meant to run at once.
WEBVIEW_STORAGE_DIR = PROJECT_ROOT / ".webview_data_moxie"

# 42001-42003 belong to Monarch, Mora and Monastery, 42005-42006 to
# Monolith and Motion, and **42007 to the Gardens window** -- which is not in
# api_common.APPS, being a window onto the list of gardens rather than
# onto a garden, and is therefore easy to miss when counting. Asking for
# a port another window already holds is not a near miss: the loser fails
# to bind and then loads *the winner's* page against its own bridge --
# see window_launcher.choose_port, and mora/app.py for the time that
# actually shipped.
DEFAULT_HTTP_PORT = 42008


def launch(garden: str | None = None, focus: str | None = None) -> None:
    api = Api(garden, focus)
    launch_window(
        name="Moxie",
        frontend_dir=FRONTEND_DIR,
        storage_dir=WEBVIEW_STORAGE_DIR,
        api=api,
        default_port=DEFAULT_HTTP_PORT,
        width=1560,
        height=960,
        min_size=(900, 660),
    )

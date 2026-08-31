# motion/app.py
"""The Motion window. monolith/app.py's twin, and deliberately so -- same
pywebview settings, same cache workaround, different frontend and
different Api. window_launcher.py carries the settings and workaround
all six apps share.

Sized like Monolith rather than like Mora: this window puts a canvas, a
timeline dock and two side rails on screen at once, and a narrow one
forces the dock to a height where a trace is a squiggle rather than a
reading. The frontend collapses gracefully below that (see the layout
notes in frontend/app.js), but the default should not need it to.
"""
from pathlib import Path

from motion.api import Api
from window_launcher import launch_window

FRONTEND_DIR = Path(__file__).resolve().parent / "frontend"
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Its own storage directory, next to but never inside the other windows'
# -- launch_window()'s cache-clearing deletes it on startup, and all six
# apps are meant to run at once.
WEBVIEW_STORAGE_DIR = PROJECT_ROOT / ".webview_data_motion"

# 42001-42005 belong to Monarch, Mora, Monastery, Momentum and Monolith.
# Asking for a port another window already holds is not a near miss: the
# loser fails to bind and then loads *the winner's* page against its own
# bridge -- see window_launcher.choose_port, and mora/app.py for the time
# that actually shipped.
DEFAULT_HTTP_PORT = 42006


def launch(garden: str | None = None, focus: str | None = None) -> None:
    api = Api(garden, focus)
    launch_window(
        name="Motion",
        frontend_dir=FRONTEND_DIR,
        storage_dir=WEBVIEW_STORAGE_DIR,
        api=api,
        default_port=DEFAULT_HTTP_PORT,
        width=1560,
        height=960,
        min_size=(1080, 660),
    )

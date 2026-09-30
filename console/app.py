# console/app.py
"""The Mosaic window: the console (docs/console.md). Same pywebview
settings and cache workaround as every other window -- window_launcher.py
carries them -- over the console's root Api and its own frontend.

User-facing name only: the window says Mosaic, while the package, the
entry point (run_console.py) and the storage directory keep the repo's
names until the rename phase (docs/console.md -> Rename)."""
from pathlib import Path

from console.api import Api
from window_launcher import launch_window

FRONTEND_DIR = Path(__file__).resolve().parent / "frontend"
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Its own storage directory, never shared with another window --
# launch_window()'s cache-clearing deletes what is under it on startup.
WEBVIEW_STORAGE_DIR = PROJECT_ROOT / ".webview_data_console"

# 42001-42003 and 42005-42008 belong to the old windows (Monarch, Mora,
# Monastery, Monolith, Motion, Gardens, Moxie), which keep running beside
# this one until each is ported -- see window_launcher.choose_port.
DEFAULT_HTTP_PORT = 42009


def launch(garden: str | None = None) -> None:
    api = Api(garden)
    launch_window(
        name="Mosaic",
        frontend_dir=FRONTEND_DIR,
        storage_dir=WEBVIEW_STORAGE_DIR,
        api=api,
        default_port=DEFAULT_HTTP_PORT,
        width=1560,
        height=960,
        min_size=(900, 600),
    )

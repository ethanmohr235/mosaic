# console/app.py
"""The Mosaic window: the console (docs/console.md), the one window
(window_launcher.py carries its pywebview settings and cache workaround),
over the console's root Api and its own frontend.

User-facing name only: the window says Mosaic, while the package, the
entry point (run_console.py) and the storage directory keep the repo's
names until the rename phase (docs/console.md -> Rename)."""
from pathlib import Path

from console.api import Api
from core.sync_worker import CONSOLE, Worker
from window_launcher import launch_window

FRONTEND_DIR = Path(__file__).resolve().parent / "frontend"
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Its own storage directory, never shared with another window --
# launch_window()'s cache-clearing deletes what is under it on startup.
WEBVIEW_STORAGE_DIR = PROJECT_ROOT / ".webview_data_console"

# 42001-42008 were the old windows'; they are all deleted. See
# window_launcher.choose_port for a second console.
DEFAULT_HTTP_PORT = 42009


def launch(garden: str | None = None) -> None:
    # The machine's sync worker, unless a service already is
    # (core/sync_worker.py). Started here, not in Api, so a test's Api
    # never starts one.
    worker = Worker.acquire(CONSOLE)
    api = Api(garden, worker=worker.start() if worker else None)
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

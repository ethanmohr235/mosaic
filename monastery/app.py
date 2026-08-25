# monastery/app.py
"""The Monastery window. mora/app.py's twin, for the same reasons it is
monarch/app.py's -- same pywebview settings, same cache workaround, different
frontend and different Api.

A third window rather than a view inside the main app: a calendar wants
the whole width of a monitor, and one that can only exist while the graph
window is open is a calendar you cannot leave up on a second screen. It
opens on its own garden connection and closes independently.
"""
import logging
import shutil
import socket
from pathlib import Path

import webview

from monastery.api import Api

FRONTEND_DIR = Path(__file__).resolve().parent / "frontend"
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Its own storage directory, a sibling of the other two windows' and
# never inside either. All three run at once by design, and WebKit's
# cache/localstorage under one path is not something three processes
# should share -- least of all when _clear_http_cache() deletes it on
# startup, which would be deleting a running window's cache.
WEBVIEW_STORAGE_DIR = PROJECT_ROOT / ".webview_data_monastery"

# Monarch takes pywebview's default 42001 and Mora takes 42002.
# A third app asking for either is not a near miss: the process that
# loses the bind does not fail visibly -- it points its window at the
# winner's address and calls the wrong bridge. That shipped once already
# as a window titled Mora showing the Monarch graph (see mora/app.py).
DEFAULT_HTTP_PORT = 42003

logger = logging.getLogger(__name__)


def choose_port(preferred: int = DEFAULT_HTTP_PORT) -> int:
    """`preferred` if it is free, else whatever the OS hands out.

    A fixed port by preference because the page origin is what scopes
    WebKit's localStorage: a port that moved every launch would mean a
    calendar that forgets which bindings you turned off between
    sessions. A fallback because a second Monastery window is a thing a
    person may reasonably open, and losing that argument should cost the
    port, not the window."""
    if _is_free(preferred):
        return preferred
    port = _ephemeral_port()
    logger.info("port %s is taken; serving Monastery on %s instead", preferred, port)
    return port


def _is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
        return True


def _ephemeral_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _clear_http_cache() -> None:
    """Drop WebKit's HTTP cache before the window opens -- see
    monarch/app.py::_clear_http_cache for the bug this prevents: a fixed port
    means an identical origin every run, so an edited frontend file
    silently doesn't appear.

    Only `cache/` goes; the sibling `localstorage/` is state meant to
    survive. Scoped to Monastery's own storage directory, so this can
    never reach into another window's."""
    shutil.rmtree(WEBVIEW_STORAGE_DIR / "cache", ignore_errors=True)


def launch(garden: str | None = None, focus: str | None = None) -> None:
    _clear_http_cache()
    api = Api(garden, focus)
    window = webview.create_window(
        "Monastery",
        url=str(FRONTEND_DIR / "index.html"),
        js_api=api,
        width=1440,
        height=900,
        min_size=(1040, 640),
    )
    api._window = window
    window.events.closed += api.shutdown
    webview.start(
        http_server=True,
        http_port=choose_port(),
        private_mode=False,
        storage_path=str(WEBVIEW_STORAGE_DIR),
    )

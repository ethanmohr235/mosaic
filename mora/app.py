# mora/app.py
"""The Mora window. ui/app.py's twin, and deliberately so -- same
pywebview settings, same cache workaround, different frontend and
different Api.

A separate window rather than a view inside the main app: Mora opens on
its own garden connection and closes independently, and a note editor
that can only exist while the graph window is open is a note editor you
cannot put on a second monitor.
"""
import logging
import shutil
import socket
from pathlib import Path

import webview

from mora.api import Api

FRONTEND_DIR = Path(__file__).resolve().parent / "frontend"
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Its own storage directory, next to but never inside the graph
# window's. Both apps run at once by design (ui/api.py::open_mora), and
# WebKit's cache/localstorage under one path is not something two
# processes should be sharing -- least of all when _clear_http_cache()
# below deletes it on startup, which would have been deleting the other
# app's cache out from under a running window.
WEBVIEW_STORAGE_DIR = PROJECT_ROOT / ".webview_data_mora"

# pywebview pins its built-in server to settings['DEFAULT_HTTP_PORT'] --
# 42001 -- whenever private_mode is off and no port is given. The graph
# window takes that one. Mora asking for it too is not a near miss: the
# second process fails to bind, and its window then loads *the first
# one's* page off 42001 and calls the wrong bridge. That is exactly what
# happened -- a window titled Mora showing the MonOSS graph, failing on
# `api_reserved_names is not a function`.
DEFAULT_HTTP_PORT = 42002

logger = logging.getLogger(__name__)


def choose_port(preferred: int = DEFAULT_HTTP_PORT) -> int:
    """`preferred` if it is free, else whatever the OS hands out.

    A fixed port by preference because the page origin is what scopes
    WebKit's localStorage: a port that moved every launch would mean an
    editor that forgets everything between sessions. A fallback because
    a second Mora window is a thing a person may reasonably open, and
    losing that argument should cost the port, not the window."""
    if _is_free(preferred):
        return preferred
    port = _ephemeral_port()
    logger.info("port %s is taken; serving Mora on %s instead", preferred, port)
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
    ui/app.py::_clear_http_cache for the bug this prevents: a fixed port
    means an identical origin every run, so an edited frontend file
    silently doesn't appear.

    Only `cache/` goes; the sibling `localstorage/` is state meant to
    survive. Scoped to Mora's own storage directory, so this can never
    reach into the graph window's."""
    shutil.rmtree(WEBVIEW_STORAGE_DIR / "cache", ignore_errors=True)


def launch(garden: str | None = None, note: str | None = None) -> None:
    _clear_http_cache()
    api = Api(garden, note)
    window = webview.create_window(
        "Mora",
        url=str(FRONTEND_DIR / "index.html"),
        js_api=api,
        width=1440,
        height=900,
        min_size=(1000, 620),
    )
    api._window = window
    window.events.closed += api.shutdown
    webview.start(
        http_server=True,
        http_port=choose_port(),
        private_mode=False,
        storage_path=str(WEBVIEW_STORAGE_DIR),
    )

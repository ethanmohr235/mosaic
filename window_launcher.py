# window_launcher.py
"""Shared pywebview window-launching boilerplate for the five desktop
apps (monarch, mora, monastery, momentum, monolith). Before this, each app's
app.py carried its own byte-for-byte copy of port picking and the
WebKit HTTP-cache-clearing workaround -- see api_common.py for the
other half of the boilerplate that used to get re-copied per app.
"""
import logging
import shutil
from pathlib import Path
from socket import AF_INET, SOCK_STREAM, socket

import webview

logger = logging.getLogger(__name__)


def choose_port(name: str, preferred: int) -> int:
    """`preferred` if it is free, else whatever the OS hands out.

    A fixed port by preference because the page origin is what scopes
    WebKit's localStorage: a port that moved every launch would mean a
    window that forgets what it saved between sessions. A fallback
    because a second window of the same app is a thing a person may
    reasonably open, and losing that argument should cost the port, not
    the window -- the alternative is what shipped once already, a
    window titled Mora showing the Monarch graph because the loser of
    the bind pointed itself at the winner's address instead."""
    if _is_free(preferred):
        return preferred
    port = _ephemeral_port()
    logger.info("port %s is taken; serving %s on %s instead", preferred, name, port)
    return port


def _is_free(port: int) -> bool:
    with socket(AF_INET, SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
        return True


def _ephemeral_port() -> int:
    with socket(AF_INET, SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def clear_http_cache(storage_dir: Path) -> None:
    """Drop WebKit's HTTP cache before the window opens.

    A fixed port means an identical origin every run, so WebKit happily
    reuses cached copies of index.html/app.js/style.css from a previous
    launch -- meaning an edit to the frontend silently doesn't appear.
    Only `cache/` is removed; sibling `localstorage/` holds state (saved
    node positions, switched-off bindings, the last-focused mote, ...)
    that must survive."""
    shutil.rmtree(storage_dir / "cache", ignore_errors=True)


def launch_window(
    *,
    name: str,
    frontend_dir: Path,
    storage_dir: Path,
    api,
    default_port: int,
    width: int,
    height: int,
    min_size: tuple[int, int],
) -> None:
    """Opens one app's window and blocks until it closes.

    `api` becomes the window's js_api and gets `_window` set on it once
    the window exists (needed for native file dialogs), the same as
    every app.py did by hand before this. `storage_dir` must already be
    this app's own directory, never shared with another window: all seven
    apps run at once by design, and clear_http_cache() deletes what is
    under it on startup."""
    clear_http_cache(storage_dir)
    window = webview.create_window(
        name,
        url=str(frontend_dir / "index.html"),
        js_api=api,
        width=width,
        height=height,
        min_size=min_size,
    )
    api._window = window
    window.events.closed += api.shutdown
    # http_server=True: serve the frontend over http://127.0.0.1 instead
    # of file://. WebKit disables localStorage for file:// pages (no
    # origin to partition storage by), which every frontend here relies
    # on to remember per-garden state between launches.
    #
    # private_mode=False so storage survives across app restarts.
    # Without an explicit storage_path, pywebview falls back to WebKit's
    # ambient default context (shared across whatever else on the system
    # uses it) rather than a location dedicated to this app.
    webview.start(
        http_server=True,
        http_port=choose_port(name, default_port),
        private_mode=False,
        storage_path=str(storage_dir),
    )

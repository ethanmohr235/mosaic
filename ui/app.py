# ui/app.py
import shutil
from pathlib import Path

import webview

from ui.api import Api

FRONTEND_DIR = Path(__file__).resolve().parent / "frontend"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
WEBVIEW_STORAGE_DIR = PROJECT_ROOT / ".webview_data"


def _clear_http_cache() -> None:
    """Drop WebKit's HTTP cache before the window opens.

    pywebview serves the frontend from a fixed port (42001 -- its
    DEFAULT_HTTP_PORT, used whenever private_mode is off and no port is
    given), so the origin is identical on every run and WebKit happily
    reuses cached copies of
    index.html/app.js/style.css from a previous launch -- meaning an edit
    to the frontend silently doesn't appear. This was caught for real:
    after adding the Run simulation button, the running app still rendered
    the old disabled Automations button while the file on disk had the new
    one. The automated smoke test never saw it because it uses a throwaway
    storage_path each run.

    Only `cache/` is removed. Sibling `localstorage/` holds each garden's
    saved node positions and last-focused mote, which must survive. There
    is nothing to gain from caching files served off local disk anyway.

    Mora is the other app on these gardens and it must not want this
    port: it runs at the same time as this one (see Api.open_mora), and
    the loser of that race does not merely fail to bind -- it points its
    window at the winner's address and loads the wrong app's page. See
    mora/app.py, which asks for 42002 and its own storage directory.
    """
    shutil.rmtree(WEBVIEW_STORAGE_DIR / "cache", ignore_errors=True)


def launch(garden: str | None = None) -> None:
    _clear_http_cache()
    api = Api(garden)
    window = webview.create_window(
        "MonOSS",
        url=str(FRONTEND_DIR / "index.html"),
        js_api=api,
        width=1280,
        height=800,
        min_size=(900, 600),
    )
    api._window = window
    window.events.closed += api.shutdown
    # http_server=True: serve the frontend over http://127.0.0.1 instead of
    # file://. WebKit disables localStorage for file:// pages (no origin to
    # partition storage by), which app.js relies on to remember the last-
    # focused mote per garden and each node's layout position.
    #
    # private_mode=False so storage survives across app restarts. Without
    # an explicit storage_path, pywebview falls back to WebKit's ambient
    # default context (shared across whatever else on the system uses it)
    # rather than a location dedicated to this app -- storage_path pins it
    # to our own project directory instead, same spirit as gardens/.
    webview.start(http_server=True, private_mode=False, storage_path=str(WEBVIEW_STORAGE_DIR))

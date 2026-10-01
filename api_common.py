# api_common.py
"""Shared pieces of the pywebview Api bridges: the console's (console/)
and the Gardens window's (gardens_app/) -- `synchronized`, launching a
sibling process, and the opportunistic background sync. The per-app
mixins the old windows shared (the inspector, the command bar, undo and
annotations) went with Monarch, the last of them (docs/console.md, U8).
See window_launcher.py for the other half of the launch boilerplate.
"""
import functools
import logging
import subprocess
import sys
import threading
from pathlib import Path

from core import gardens as gardens_repo
from core import sync as sync_repo
from core.db import get_connection

logger = logging.getLogger(__name__)


def synchronized(method):
    """pywebview invokes exposed methods from a worker thread, and not
    the same one each time. Each Api holds one long-lived sqlite3
    connection (opened with check_same_thread=False), so calls onto it
    must be serialized here rather than left to sqlite3 to police.
    Assumes `self._lock`, a threading.Lock every Api.__init__ sets up."""
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapper


# ---- launching a sibling app ----

REPO_ROOT = Path(__file__).resolve().parent

# app id -> (display name, its run_*.py, the flag that opens straight to
# one mote -- None where that script takes no such flag).
APPS = {
    # The console (docs/console.md): the one window the others were
    # ported into. Listed so the Gardens window can open it; it takes no
    # focus flag -- selection lives in the console's own store.
    "console": ("Mosaic", REPO_ROOT / "run_console.py", None),
}


# The Gardens window (gardens_app/), deliberately not an entry in APPS
# above: those are windows *onto* a garden, launched with --garden. This
# one is a window onto the list of gardens, so "open it on garden X"
# means "start with X selected". `mo saic` opens it until console U9.
GARDENS_SCRIPT = REPO_ROOT / "run_gardens.py"


def open_gardens(garden: str | None = None, *, sync: bool = False) -> dict:
    """Open the shared garden manager, as its own detached process.

    Creating, renaming, importing and deleting gardens used to exist only
    inside Monarch's own modal; one window, one implementation, opened
    from the console's `mo saic`.

    Not a singleton: a second Gardens window is a duplicate list rather
    than anything dangerous, and pywebview can't raise and focus a
    window belonging to another process -- so a click that silently did
    nothing (because the first one is buried behind the app that opened
    it) would be the worse failure. Same trade window_launcher.choose_port
    already makes for second copies of the apps."""
    command = [sys.executable, str(GARDENS_SCRIPT)]
    if garden:
        command += ["--garden", garden]
    if sync:
        command.append("--sync")
    popen_detached(command, quiet=False)
    return {"gardens": True, "select": garden, "sync": sync}


def popen_detached(args: list[str], *, quiet: bool = True) -> None:
    """Launches args without waiting on it, but still reaps the child on a
    background thread so it doesn't sit as a zombie for as long as MonOSS
    keeps running -- this app is long-lived and every launch spawns one
    of these.

    `quiet` discards the child's output, which is right for an OS opener
    (`gio open` chatters about nothing we can act on) and wrong for our
    own apps: a discarded traceback is how a crashed sibling app came to
    look like a mysteriously wrong window instead of an error."""
    streams = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL} if quiet else {}
    proc = subprocess.Popen(args, **streams)
    threading.Thread(target=proc.wait, daemon=True).start()


def best_effort_sync(garden: str) -> None:
    """Every app's `_best_effort_sync()` calls this on open, garden
    switch, and close: it pushes/pulls `garden` on a background thread,
    using its own short-lived connection rather than the caller's, so a
    sync server that's slow or offline (compass turned off, Tailscale
    peer unreachable) never blocks opening, switching gardens, or
    closing a window -- whichever one of those called this has often
    already replaced or closed its own connection by the time the
    thread gets to run. Errors are logged, not raised: this path is
    opportunistic, unlike the visible refresh()/sync_now() button,
    which waits and surfaces a real failure to the user."""
    if gardens_repo.get_sync_dir(garden) is None:
        return

    def _run() -> None:
        conn = get_connection(garden)
        try:
            sync_repo.run(conn, garden)
        except Exception:
            logger.exception("garden sync failed for %r", garden)
        finally:
            conn.close()

    threading.Thread(target=_run, daemon=True).start()


def open_app(app_id: str, garden: str, mote_id: str | None = None) -> dict:
    """Launches another MonOSS app on this same garden, as its own
    detached process rather than a second window here: each app holds
    its own garden connection and outlives or predates this one freely.
    The two processes then hold separate connections to the same garden
    file -- neither observes the other's writes until it refetches, but
    nothing is lost either way, since both write one property at a time.

    `sys.executable` rather than a bare `python`: the app is running in
    a specific virtualenv (docs/ui-setup.md), and the sibling has to come
    up in that same one. `quiet=False` so a sibling that fails to start
    says so on the terminal this app was launched from, rather than
    failing silently."""
    _name, script, focus_flag = APPS[app_id]
    command = [sys.executable, str(script), "--garden", garden]
    if mote_id and focus_flag:
        command += [focus_flag, mote_id]
    popen_detached(command, quiet=False)
    return {"app": app_id, "garden": garden, "focus": mote_id}

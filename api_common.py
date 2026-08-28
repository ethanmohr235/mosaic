# api_common.py
"""Shared pieces of the pywebview Api bridge every desktop app (monarch,
mora, monastery, momentum) exposes as its js_api. Before this, each
app's api.py carried its own byte-for-byte copy of `synchronized` and,
where properties are edited as text, `coerce`/`coerce_properties` --
see window_launcher.py for the other half of the boilerplate that used
to get re-copied per app.
"""
import functools
import json
import subprocess
import sys
import threading
from pathlib import Path


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


def coerce(raw):
    """A property value as the garden should store it: text-field input
    interpreted as JSON where possible, so a duration typed as "90" is
    stored as a number rather than a string core/calendar.py would
    ignore. Only strings need coercing -- copy/paste sends a mote's
    already-typed properties straight back through create_mote, already
    the right type, and json.loads() raises TypeError on anything that
    isn't str/bytes/bytearray."""
    if not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def coerce_properties(raw_properties: dict) -> dict:
    return {k: coerce(v) for k, v in raw_properties.items()}


# ---- launching a sibling app ----

REPO_ROOT = Path(__file__).resolve().parent

# app id -> (display name, its run_*.py, the flag that opens straight to
# one mote -- None where that script takes no such flag). The single
# source both the ⌘J switcher (below) and each app's own frontend read,
# so a fifth app is one entry here rather than a fifth copy-pasted
# open_<sibling>() per existing app.
APPS = {
    "monarch": ("Monarch", REPO_ROOT / "run_monarch.py", None),
    "mora": ("Mora", REPO_ROOT / "run_mora.py", "--note"),
    "monastery": ("Monastery", REPO_ROOT / "run_monastery.py", "--focus"),
    "momentum": ("Momentum", REPO_ROOT / "run_momentum.py", "--focus"),
}


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

# monastery/api.py
"""JS-facing bridge for Monastery, the calendar window.

A fifth adapter over core/, alongside cli/, ui/, mora/ and mcp_server/ --
not part of ui/ for the same reason Mora isn't: Monastery is its own
window with its own concerns, and sharing ui/api.py would mean sharing a
garden connection between windows that open and close independently.

It follows mora/api.py's shape deliberately: one long-lived connection
for the open garden, held as instance state, with every exposed method
serialized behind a lock because pywebview calls in from a pool of
worker threads.

One line this bridge does not cross. It holds no knowledge of what makes
a property a date -- discovering bindings and expanding recurrence is
core/calendar.py's, and shaping the answer is monastery/payload.py's.
What it adds is the two things only a window knows: which garden is
open, and which bindings the person has turned off.
"""
import functools
import json
import logging
import threading
from datetime import datetime

import webview

from core import calendar as calendar_repo
from core import gardens as gardens_repo
from core import graph as graph_repo
from core import motes as motes_repo
from core import relationships as rel_repo
from core import scenarios as scenarios_repo
from core import species as species_repo
from core import sync as sync_repo
from core.db import get_connection, initialize_database
from monastery import payload as payload_repo

logger = logging.getLogger(__name__)

# How long a write waits out a lock held by another process before giving
# up. Monastery is opened *from* the graph window (ui/api.py::
# open_monastery) and may sit alongside Mora too, so several processes
# holding the same garden file open at once is the normal case here, not
# the exception -- the same reason mora/api.py and mcp_server/server.py
# set this. core/db.py stays untouched, so the CLI and the main UI keep
# their current behavior exactly.
_BUSY_TIMEOUT_MS = 5000


def _synchronized(method):
    """pywebview invokes exposed methods from a worker thread, and not
    the same one each time. The connection is opened check_same_thread=
    False, so serializing here is this class's job -- the same reason
    ui/api.py and mora/api.py have this decorator."""
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapper


def _coerce(raw):
    """A property value as the garden should store it: text-field input
    interpreted as JSON where possible, so a duration typed as "90" is
    stored as a number rather than a string core/calendar.py would
    ignore. A date is not valid JSON and stays a string, which is what
    we want.

    Byte-for-byte ui/api.py::_coerce, copied rather than imported -- no
    adapter imports another -- because "what does typing this into a
    property field mean" must not have two answers in two windows."""
    if not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def _coerce_properties(raw_properties: dict) -> dict:
    return {k: _coerce(v) for k, v in raw_properties.items()}


class Api:
    """Every read takes the visible window as arguments rather than
    holding a cursor of its own. The frontend owns where the calendar is
    scrolled to; asking for a range keeps that the one source of truth
    and means a poll can refresh what is on screen without the two sides
    having to agree about a stored position."""

    def __init__(self, garden: str | None = None, focus: str | None = None):
        self._lock = threading.Lock()
        self._garden = gardens_repo.resolve_garden(garden)
        # `run_monastery.py --focus <id>`: the frontend opens on today,
        # so the command line has to be able to say otherwise.
        self._initial_focus = focus
        self._conn = self._open(self._garden)
        self._window: webview.Window | None = None
        self._best_effort_sync()

    def _open(self, garden: str):
        conn = get_connection(garden)
        conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        initialize_database(conn)
        return conn

    def _best_effort_sync(self) -> None:
        """Same call, same reasoning, as mora/api.py::Api._best_effort_sync:
        run_monastery.py is its own launch path, not only ever opened from
        the graph window, so a mote dated here must converge on its own
        rather than depending on that other window's session having also
        been open to carry it. Never allowed to block opening, switching
        or closing this window."""
        if gardens_repo.get_sync_dir(self._garden) is None:
            return
        try:
            sync_repo.run(self._conn, self._garden)
        except Exception:
            logger.exception("garden sync failed for %r", self._garden)

    @_synchronized
    def shutdown(self) -> None:
        self._best_effort_sync()
        self._conn.close()

    # ---- gardens ----

    @_synchronized
    def current_garden(self) -> str:
        return self._garden

    @_synchronized
    def list_gardens(self) -> list[str]:
        return gardens_repo.list_gardens()

    @_synchronized
    def switch_garden(self, name: str) -> dict:
        resolved = gardens_repo.resolve_garden(name)
        self._conn.close()
        self._conn = self._open(resolved)
        self._garden = resolved
        self._best_effort_sync()
        return {"garden": resolved}

    @_synchronized
    def initial_focus(self) -> str | None:
        return self._initial_focus

    # ---- the calendar ----

    def _excluded_ids(self) -> set[str]:
        """Templates and scenario overrides, which hold replacement or
        starting values rather than real data. The same exclusion
        core/google_calendar.py::exportable_motes applies, and for the
        same reason: a dated template would draw as an event for
        something that never happened."""
        return species_repo.template_mote_ids(self._conn) | scenarios_repo.override_mote_ids(
            rel_repo.list_relationships(self._conn, type_=scenarios_repo.OVERRIDES)
        )

    @_synchronized
    def calendar(self, window_start: str, window_end: str, bindings: list[str] | None = None) -> dict:
        """Everything on screen for one visible range.

        `bindings` is None on the first call of a session -- the frontend
        has no saved preference to send yet -- and core answers with all
        of them. After that the frontend always says, so a binding
        toggled off stays off across a poll."""
        motes, relationships = graph_repo.whole_garden(self._conn)
        excluded = self._excluded_ids()
        counts = calendar_repo.binding_counts(motes)
        active = calendar_repo.default_bindings(motes) if bindings is None else list(bindings)
        found = calendar_repo.occurrences(
            motes,
            bindings=active,
            window_start=datetime.fromisoformat(window_start),
            window_end=datetime.fromisoformat(window_end),
            exclude_ids=excluded,
        )
        return payload_repo.calendar_payload(
            found,
            payload_repo.Directory.of(motes, relationships),
            binding_counts=counts,
            active_bindings=active,
            undated=calendar_repo.undated_count(motes, exclude_ids=excluded),
        )

    # ---- the detail panel ----

    def _panel(self, mote_id: str) -> dict | None:
        """The detail panel's answer, without taking the lock -- the
        editing methods below already hold it, and threading.Lock is not
        reentrant."""
        mote = motes_repo.get_mote(self._conn, mote_id)
        if mote is None:
            return None
        motes, relationships = graph_repo.whole_garden(self._conn)
        return payload_repo.panel_payload(mote, payload_repo.Directory.of(motes, relationships))

    @_synchronized
    def focus(self, mote_id: str) -> dict | None:
        return self._panel(mote_id)

    # ---- editing ----

    @_synchronized
    def create_mote(self, raw_properties: dict, species_id: str | None = None) -> dict:
        """A mote created from an empty slot on the grid.

        Always writes the reserved `date` (see docs/architecture.md's
        reserved property names), never the binding the calendar happened
        to be filtered by: reading is deliberately general, but there is
        one right answer to where a *new* date belongs, and Calendar
        export already knows that key."""
        mote_id = motes_repo.create_mote(self._conn, _coerce_properties(raw_properties))
        if species_id:
            species_repo.set_species(self._conn, mote_id, species_id)
        return self._panel(mote_id)

    @_synchronized
    def update_mote(self, mote_id: str, raw_properties: dict) -> dict | None:
        motes_repo.update_mote(self._conn, mote_id, _coerce_properties(raw_properties))
        return self._panel(mote_id)

    @_synchronized
    def delete_property(self, mote_id: str, key: str) -> dict | None:
        motes_repo.delete_property(self._conn, mote_id, key)
        return self._panel(mote_id)

    @_synchronized
    def delete_mote(self, mote_id: str) -> bool:
        return motes_repo.delete_mote(self._conn, mote_id)

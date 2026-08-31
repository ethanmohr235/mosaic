# monastery/api.py
"""JS-facing bridge for Monastery, the calendar window.

A fifth adapter over core/, alongside cli/, monarch/, mora/ and mcp_server/ --
not part of monarch/ for the same reason Mora isn't: Monastery is its own
window with its own concerns, and sharing monarch/api.py would mean sharing a
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
import logging
import sqlite3
import threading
from datetime import datetime

import webview

from api_common import coerce_properties as _coerce_properties
from api_common import open_app as _open_app
from api_common import species_ids as _species_ids
from api_common import synchronized as _synchronized
from core import calendar as calendar_repo
from core import gardens as gardens_repo
from core import graph as graph_repo
from core import motes as motes_repo
from core import relationships as rel_repo
from core import scenarios as scenarios_repo
from core import species as species_repo
from core import sync as sync_repo
from core.db import get_connection, initialize_database
from core.entities import Mote
from monastery import payload as payload_repo

logger = logging.getLogger(__name__)

# How long a write waits out a lock held by another process before giving
# up. Monastery is opened *from* Monarch (monarch/api.py::
# open_monastery) and may sit alongside Mora too, so several processes
# holding the same garden file open at once is the normal case here, not
# the exception -- the same reason mora/api.py and mcp_server/server.py
# set this. core/db.py stays untouched, so the CLI and the main UI keep
# their current behavior exactly.
_BUSY_TIMEOUT_MS = 5000

# What a dragged or deleted rectangle was meant to change when the mote
# behind it recurs. The window asks before it writes (the chooser in
# monastery/frontend/app.js) and sends one of these back; nothing is
# guessed on this side.
SERIES = "series"
OCCURRENCE = "occurrence"


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
        Monarch, so a mote dated here must converge on its own
        rather than depending on that other window's session having also
        been open to carry it. Never allowed to block opening, switching
        or closing this window."""
        if gardens_repo.get_sync_dir(self._garden) is None:
            return
        try:
            sync_repo.run(self._conn, self._garden)
        except Exception:
            logger.exception("garden sync failed for %r", self._garden)

    # ---- sync ----

    @_synchronized
    def refresh(self) -> dict:
        """Manual refresh button: sync (if configured) then let the
        caller re-fetch the current view fresh from self._conn. Unlike
        _best_effort_sync(), a real sync error is not swallowed --
        surfaced to the user as a toast via call()'s existing rejection
        handling. Unconfigured sync is not an error -- refresh is also
        just "reload from disk" for a garden with no sync set up."""
        if gardens_repo.get_sync_dir(self._garden) is None:
            return {"synced": False}
        result = sync_repo.run(self._conn, self._garden)
        return {"synced": True, **result}

    @_synchronized
    def shutdown(self) -> None:
        self._best_effort_sync()
        self._conn.close()

    # ---- app switcher ----

    @_synchronized
    def open_app(self, app_id: str, mote_id: str | None = None) -> dict:
        """Launch another MonOSS app on this same garden from the ⌘J
        switcher -- see api_common.open_app for why this is a detached
        process rather than a second window here."""
        return _open_app(app_id, self._garden, mote_id)

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
        starting values rather than real data: a dated template would
        draw as an event for something that never happened."""
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
        return {
            **payload_repo.calendar_payload(
                found,
                payload_repo.Directory.of(motes, relationships),
                binding_counts=counts,
                active_bindings=active,
                undated=calendar_repo.undated_count(motes, exclude_ids=excluded),
            ),
            # The palette universe rides along with the answer rather
            # than being a call of its own -- see api_common.species_ids.
            "speciesIds": _species_ids(self._conn),
        }

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

    # ---- editing an occurrence ----
    #
    # A rectangle on the grid is a *placement*, not a mote: one mote can
    # draw fifty of them. So each of these takes the binding that placed
    # it and the start that identifies it within its series, and asks
    # core/calendar.py what the drag means in properties. Nothing here
    # decides which key to write -- that answer lives in one place, with
    # the code that read it.

    def _occurrence(self, mote_id: str, binding: str, occurrence_start: str):
        """The mote, the placement's start, and whether it is all-day --
        what every edit below needs. None when the mote is gone or no
        longer carries that binding, which a poll racing another window's
        write can legitimately produce."""
        mote = motes_repo.get_mote(self._conn, mote_id)
        if mote is None:
            return None
        parsed = calendar_repo.parse_date_value(mote.properties.get(binding))
        if parsed is None:
            return None
        return mote, datetime.fromisoformat(occurrence_start), parsed[1]

    def _detach(self, mote, binding: str, occurrence_start: datetime, all_day: bool,
                new_start: datetime, new_end: datetime) -> str:
        """One occurrence lifted out of its series into a mote of its
        own: an exception where it used to be, a copy where it now is.

        This is the only honest reading of "just this one" in a garden
        where one mote *is* the whole series. RFC 5545 would call the
        copy a RECURRENCE-ID override; MonOSS has no such thing on
        purpose, and an ordinary mote says the same thing without
        inventing a second kind of event.
        """
        excepted = calendar_repo.with_exception(mote, binding, occurrence_start)
        if excepted:
            motes_repo.update_mote(self._conn, mote.id, excepted)
        # detached_properties() drops the rule itself -- a copy that kept
        # it would go on recurring, and the rule now lives inside the
        # binding's own value rather than in a property of its own.
        detached_id = motes_repo.copy_mote(
            self._conn, self._conn, mote.id,
            calendar_repo.detached_properties(mote, binding, new_start, new_end),
        )
        # copy_mote() is properties only -- there is no general answer for
        # relationships -- but the species is the one a split-off
        # occurrence has to keep, because it is what colours it.
        for species in species_repo.species_of(self._conn, mote.id):
            species_repo.set_species(self._conn, detached_id, species.id)
        return detached_id

    @_synchronized
    def place_occurrence(self, mote_id: str, binding: str, occurrence_start: str,
                         new_start: str, new_end: str, scope: str = SERIES) -> dict | None:
        """Where a dragged rectangle came to rest: this placement now
        occupies [new_start, new_end).

        One method for moving and resizing both, because the top edge of
        an event is both at once -- dragging it moves the start and keeps
        the end, and two separate writes would mean two oplog rows for
        one gesture and, on a series, two detachments for one drop.

        What is written follows from what actually changed: a start that
        moved rewrites the binding that placed it (never `date` -- see
        core/calendar.py's writing section), and a span that changed
        rewrites whichever of `ends`/`duration` the mote already declares.
        A move that kept its length therefore leaves the length alone
        rather than pinning a default of 60 onto a mote that never said
        one.
        """
        found = self._occurrence(mote_id, binding, occurrence_start)
        if found is None:
            return None
        mote, start, all_day = found
        target, target_end = datetime.fromisoformat(new_start), datetime.fromisoformat(new_end)
        if target_end <= target:
            return self._panel(mote.id)

        if scope == OCCURRENCE and calendar_repo.is_recurring(mote, binding):
            return self._panel(self._detach(mote, binding, start, all_day, target, target_end))

        moved = (
            calendar_repo.moved_properties(mote, binding, start, target)
            if target != start else {}
        )
        # Measured against the *moved* mote: an `ends` written from the
        # old anchor would land the span a drag's worth of days away from
        # where the rectangle now is.
        after = Mote(properties={**mote.properties, **moved}, id=mote.id)
        resized = (
            calendar_repo.resized_properties(after, binding, target, target_end)
            if target_end - target != calendar_repo.span_of(mote, binding) else {}
        )
        motes_repo.update_mote(self._conn, mote.id, {**moved, **resized})
        return self._panel(mote.id)

    @_synchronized
    def delete_occurrence(self, mote_id: str, binding: str, occurrence_start: str,
                          scope: str = SERIES) -> dict | None:
        """Removing what you can see, which is a placement rather than
        necessarily a mote.

        `scope="occurrence"` on a series writes an exception: the mote
        stays, one repetition stops happening. Anything else deletes the
        mote, because a mote that does not recur *is* its placement.
        Both hand back what undo_delete() needs to put it back, which is
        why this returns a token rather than a bare bool.
        """
        found = self._occurrence(mote_id, binding, occurrence_start)
        if found is None:
            return None
        mote, start, all_day = found
        name = payload_repo.name_of(mote)

        if scope == OCCURRENCE:
            excepted = calendar_repo.with_exception(mote, binding, start)
            if excepted:
                motes_repo.update_mote(self._conn, mote.id, excepted)
                return {"name": name, "scope": OCCURRENCE, "undo": {
                    "kind": "interval",
                    "moteId": mote.id,
                    "binding": binding,
                    "value": mote.properties.get(binding),
                }}

        # Recorded before the delete, not after: relationships cascade
        # with the mote (core/db.py's ON DELETE CASCADE), so this is the
        # last moment they can be read.
        relationships = [
            {"id": r.id, "fromId": r.from_id, "toId": r.to_id, "type": r.type}
            for r in rel_repo.list_relationships(self._conn)
            if mote.id in (r.from_id, r.to_id)
        ]
        motes_repo.delete_mote(self._conn, mote.id)
        return {"name": name, "scope": SERIES, "undo": {
            "kind": "mote",
            "moteId": mote.id,
            "properties": mote.properties,
            "relationships": relationships,
        }}

    @_synchronized
    def undo_delete(self, token: dict) -> dict | None:
        """Put back exactly what delete_occurrence() took.

        A deleted mote returns under its own id, with the relationships
        that touched it under theirs -- create_mote() and
        create_relationship() both accept one -- so undo restores the
        graph rather than a lookalike, and every arrow that pointed at it
        still points at it.
        """
        if not isinstance(token, dict):
            return None

        if token.get("kind") == "interval":
            mote_id, binding = token.get("moteId"), token.get("binding")
            previous = token.get("value")
            if previous is None:
                motes_repo.delete_property(self._conn, mote_id, binding)
            else:
                motes_repo.update_mote(self._conn, mote_id, {binding: previous})
            return self._panel(mote_id)

        if token.get("kind") != "mote":
            return None
        restored = motes_repo.create_mote(
            self._conn, token.get("properties") or {}, id=token.get("moteId")
        )
        for relationship in token.get("relationships") or []:
            try:
                rel_repo.create_relationship(
                    self._conn, relationship["fromId"], relationship["toId"],
                    relationship["type"], id=relationship["id"],
                )
            except sqlite3.IntegrityError:
                # The mote at the other end went away while the undo toast
                # was still up. Restoring the rest is better than refusing
                # the whole undo over an arrow with nothing to point at.
                logger.warning("could not restore relationship %r", relationship.get("id"))
        return self._panel(restored)

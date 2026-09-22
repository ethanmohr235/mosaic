# momentum/api.py
"""JS-facing bridge for Momentum, the task window.

A sixth adapter over core/, alongside cli/, monarch/, monastery/, mora/
and mcp_server/. It follows monastery/api.py's shape deliberately: one
long-lived connection for the open garden, held as instance state, with
every exposed method serialized behind a lock because pywebview calls in
from a pool of worker threads.

Two lines this bridge does not cross.

It holds no knowledge of **what makes a property a status, a priority or
an estimate** -- that is core/task_candidates.py's, discovered per
garden rather than reserved, the same way core/calendar.py discovers
which key is a date. What this adds is the one thing only a window
knows: which of those candidates the person picked.

It holds no knowledge of **when a day is free**, either. Dropping a task
on a day asks core/calendar.py for the earliest gap, reading the same
occurrences Monastery draws, so "busy" means one thing in this app
rather than two.
"""
import logging
import threading
from datetime import date, datetime, time, timedelta

import webview

from api_common import CommandBarApi, InspectorApi
from api_common import coerce_properties as _coerce_properties
from api_common import open_app as _open_app
from api_common import open_gardens as _open_gardens
from api_common import species_ids as _species_ids
from api_common import synchronized as _synchronized
from core import calendar as calendar_repo
from core import gardens as gardens_repo
from core import interval as interval_repo
from core import graph as graph_repo
from core import modoc as modoc_repo
from core import motes as motes_repo
from core import relationships as rel_repo
from core import scenarios as scenarios_repo
from core import species as species_repo
from core import sync as sync_repo
from core import task_candidates as candidates_repo
from core.db import get_connection, initialize_database
from core.entities import Mote
from momentum import payload as payload_repo

logger = logging.getLogger(__name__)

# How long a write waits out a lock held by another process. Momentum is
# opened *from* Monarch and expects Monastery and Mora alongside it, so
# several processes holding one garden file open is the normal case --
# the same reason monastery/api.py and mora/api.py set this.
_BUSY_TIMEOUT_MS = 5000

# Where a scheduled time goes in a garden that has never dated anything.
# An ordinary property name, not a reserved one: it is only ever the
# *fallback*, used when neither the mote nor the garden already answers
# "which key means when".
FALLBACK_BINDING = "scheduled"

# What a task with no estimate is assumed to need when looking for a
# gap. Matches core/calendar.py::DEFAULT_TIMED_MINUTES, so a scheduled
# task and the rectangle Monastery draws for it are the same length.
DEFAULT_SLOT_MINUTES = 60

# How low a logged session is allowed to push a task's remaining
# estimate while the task is still open. An estimate hitting exactly
# zero without the task itself being marked done reads as "nothing
# left to do" everywhere that figure is shown, which is rarely true --
# the last quarter hour is still owed until something says the task is
# over. Completing the task (the timer's own complete button, the days
# view's check, or dragging to a done column) lifts this floor.
ESTIMATE_FLOOR_MINUTES = 15

# The values the focus timer writes to a status-bound property on its
# own -- two of the three presets the person asked for; a task with no
# status at all already reads as "not started" everywhere this window
# looks (board_payload's "no status" column, is_done()), so nothing
# ever has to write that one down. Ordinary strings, exactly like
# core/task_candidates.py::DEFAULT_STATUSES -- a garden using its own
# vocabulary elsewhere still gets these from the timer, because
# payload.is_done_value() already recognizes "complete" as finished
# regardless of what else a garden calls its done column.
STATUS_WORKING = "working"
STATUS_COMPLETE = "complete"

# The mote a garden keeps Momentum's settings on, found by name. No
# reserved property and no new table: a garden that never edits these
# never grows one, and one that does can edit it like any other mote.
SETTINGS_NAME = "Momentum"
DEFAULT_CAPACITY_HOURS = 6.0

# The species logged work sessions are instances of, found or created by
# name. A session is an ordinary mote carrying an ordinary interval,
# which is precisely why it turns up in Monastery for free.
SESSION_SPECIES_NAME = "Work Session"

# How far back the focus tab's heatmap and streak look.
HEATMAP_DAYS = 14
STREAK_LIMIT_DAYS = 90


def _parse_time(raw, fallback: time) -> time:
    if not isinstance(raw, str):
        return fallback
    try:
        return time.fromisoformat(raw.strip())
    except ValueError:
        return fallback


class Api(InspectorApi, CommandBarApi):
    """Every read takes the person's key choices as arguments rather
    than holding them as state. The window owns which key it is reading
    as status, the same way Monastery owns which bindings are switched
    on -- so a poll refreshes what is on screen without the two sides
    having to agree about a stored preference."""

    def __init__(self, garden: str | None = None, focus: str | None = None):
        self._lock = threading.Lock()
        self._garden = gardens_repo.resolve_garden(garden)
        # `run_momentum.py --focus <id>`: the window opens on the board,
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
        """Same call, same reasoning, as monastery/api.py's:
        run_momentum.py is its own launch path, so a task scheduled here
        must converge on its own rather than depending on another
        window's session having been open to carry it. Never allowed to
        block opening, switching or closing this window."""
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
        caller re-fetch the current view. Unlike _best_effort_sync(), a
        real sync error is surfaced to the user as a toast."""
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
    def open_gardens(self, sync: bool = False) -> dict:
        """Open the shared Gardens window (gardens_app/): create, rename,
        delete, import, export and sync, for every garden rather than
        just this one.

        All six windows expose this same method, which is the point --
        garden management used to exist once, privately, inside
        Monarch's own modal, so doing any of it from here meant opening
        Monarch first. Switching which garden *this* window shows stays
        here (switch_garden above); everything about the gardens
        themselves is one window all six can open."""
        return _open_gardens(self._garden, sync=sync)

    @_synchronized
    def initial_focus(self) -> str | None:
        return self._initial_focus

    # ---- what the garden means ----

    def _excluded_ids(self) -> set[str]:
        """Templates and scenario overrides, which hold replacement or
        starting values rather than real data. The same exclusion
        monastery/api.py applies: a template carrying a status describes
        what instances start as, not a task anyone has to do."""
        return species_repo.template_mote_ids(self._conn) | scenarios_repo.override_mote_ids(
            rel_repo.list_relationships(self._conn, type_=scenarios_repo.OVERRIDES)
        )

    def _resolve_keys(
        self,
        motes: list[Mote],
        status_key=None,
        priority_key=None,
        estimate_key=None,
        bindings=None,
    ) -> payload_repo.Keys:
        """Which key means what for this request.

        Every argument is None on the first call of a session -- the
        window has no saved preference to send yet -- and core answers
        with its best guess. After that the window always says, so a key
        the person corrected stays corrected across a poll. `False` is
        how the window says "explicitly nothing" for a key it wants
        switched off, which None cannot express.
        """
        status = (
            candidates_repo.default_enum_key(motes, candidates_repo.PREFERRED_STATUS_NAMES)
            if status_key is None else _explicit(status_key)
        )
        priority = (
            candidates_repo.default_enum_key(
                motes, candidates_repo.PREFERRED_PRIORITY_NAMES, exclude=status
            )
            if priority_key is None else _explicit(priority_key)
        )
        estimate = (
            candidates_repo.default_estimate_key(motes)
            if estimate_key is None else _explicit(estimate_key)
        )
        active = (
            calendar_repo.default_bindings(motes) if bindings is None else list(bindings)
        )
        return payload_repo.Keys(
            status=status, priority=priority, estimate=estimate, bindings=tuple(active)
        )

    def _candidates(self, motes: list[Mote], keys: payload_repo.Keys) -> dict:
        return payload_repo.candidates_payload(
            candidates_repo.enum_candidates(motes),
            candidates_repo.estimate_candidates(motes),
            calendar_repo.binding_counts(motes),
            keys,
        )

    def _settings(self, motes: list[Mote]) -> dict:
        """Momentum's per-garden settings, read off a mote named
        "Momentum" if the garden has one. Ordinary properties on an
        ordinary mote, so they are editable in every window rather than
        living in a config file this app alone can reach."""
        found = next(
            (m for m in motes if payload_repo.name_of(m).casefold() == SETTINGS_NAME.casefold()),
            None,
        )
        props = found.properties if found else {}
        hours = props.get("capacity_hours")
        capacity = float(hours) if isinstance(hours, (int, float)) and not isinstance(hours, bool) and hours > 0 else DEFAULT_CAPACITY_HOURS
        return {
            "capacityMinutes": capacity * 60,
            "workStart": _parse_time(props.get("work_start"), calendar_repo.DEFAULT_WORK_START),
            "workEnd": _parse_time(props.get("work_end"), calendar_repo.DEFAULT_WORK_END),
            "settingsId": found.id if found else None,
        }

    def _garden_state(self, **key_args):
        """One read of the garden plus everything derived from it that
        all three tabs need. Every view method starts here, so a tab
        switch cannot see a different garden than the tab before it.

        `visible` is everything that still counts as *occupying time* --
        logged sessions included. `tasks` is what draws as a card, which
        excludes two things:

        - **sessions**, because a session is a record of work already
          done, and a board listing one as something to do would be
          asking you to do it twice. They stay in `visible` because an
          hour already spent is an hour genuinely not free.
        - **motes acting as a species**, because a species declares what
          a kind of thing *is*. `Work Session` is a type, not an
          afternoon's work -- and since this window creates that mote
          itself, leaving it in would mean Momentum putting its own
          plumbing on your board.

        There is deliberately no *positive* gate beyond those: a garden's
        tasks are whatever its motes say they are, the same answer
        Monastery gives about which motes are dated.
        """
        motes, relationships = graph_repo.whole_garden(self._conn)
        excluded = self._excluded_ids()
        visible = [m for m in motes if m.id not in excluded]
        not_work = self._session_ids() | species_repo.species_ids_in(relationships)
        tasks = [m for m in visible if m.id not in not_work]
        keys = self._resolve_keys(tasks, **key_args)
        directory = payload_repo.Directory.of(motes, relationships)
        return visible, tasks, excluded, keys, directory

    def _session_ids(self) -> set[str]:
        """Every mote that is a logged work session, by species."""
        species_id = self._session_species_id()
        if species_id is None:
            return set()
        return {m.id for m in species_repo.instances_of(self._conn, species_id)}

    def _stats(self, cards: list[dict], visible: list[Mote], keys: payload_repo.Keys) -> dict:
        return {
            "open": sum(1 for c in cards if not payload_repo.is_done(c)),
            "undated": sum(1 for c in cards if c["scheduled"] is None),
            "total": len(cards),
        }

    def _with_palette(self, answer: dict) -> dict:
        """Every view answer carries the palette universe -- see
        api_common.species_ids. One place rather than three, because a
        tab that forgot it would draw a species in a colour no other
        window uses."""
        return {**answer, "speciesIds": _species_ids(self._conn)}

    # ---- the board ----

    @_synchronized
    def board(self, status_key=None, priority_key=None, estimate_key=None, bindings=None) -> dict:
        """Every mote in the garden, in columns by status.

        No folder or species gate: a garden's tasks are whatever its
        motes say they are, which is the same answer Monastery gives
        about which motes are dated. A mote the status key says nothing
        about still gets a column (payload.board_payload) rather than
        disappearing.
        """
        _, tasks, _, keys, directory = self._garden_state(
            status_key=status_key, priority_key=priority_key,
            estimate_key=estimate_key, bindings=bindings,
        )
        cards = [payload_repo.card_payload(m, directory, keys) for m in tasks]
        return self._with_palette(payload_repo.board_payload(
            cards,
            statuses=candidates_repo.statuses_for(tasks, keys.status),
            keys=keys,
            candidates=self._candidates(tasks, keys),
            stats=self._stats(cards, tasks, keys),
        ))

    # ---- the week ----

    @_synchronized
    def days(
        self, window_start: str, window_end: str,
        status_key=None, priority_key=None, estimate_key=None, bindings=None,
    ) -> dict:
        """Seven days of scheduled work, and the undated pool beside it.

        A card lands on a day because a date-shaped property says so --
        the same reading Monastery does -- so anything scheduled here is
        already an event there, and anything dated there already appears
        here. That is the whole of the integration: there is no second
        store of "when I plan to do this".

        A mote that only ever had its date set -- through the MCP tools,
        say, rather than a drag in this window -- carries a bare day with
        no time on it. _auto_place() gives that day a real start and end
        the same way a drop onto a day would, so "set a date" and "drag
        onto a day" converge on the same placement without the person
        having to do the drag themselves. It is the one read in this
        class that writes, and it is handed the same window the days are
        drawn from so it can only ever touch what is on screen.
        """
        visible, tasks, excluded, keys, directory = self._garden_state(
            status_key=status_key, priority_key=priority_key,
            estimate_key=estimate_key, bindings=bindings,
        )
        settings = self._settings(tasks)

        start = datetime.fromisoformat(window_start).date()
        end = datetime.fromisoformat(window_end).date()
        self._auto_place(tasks, visible, excluded, keys, settings, start, end)
        cards = [payload_repo.card_payload(m, directory, keys) for m in tasks]

        by_day: dict[str, list[dict]] = {}
        pool: list[dict] = []
        for card in cards:
            placement = card["scheduled"]
            if placement is None:
                pool.append(card)
            else:
                by_day.setdefault(placement["day"], []).append(card)

        days = []
        cursor = start
        while cursor < end:
            key = cursor.strftime("%Y-%m-%d")
            days.append(payload_repo.day_payload(
                key,
                sorted(by_day.get(key, []), key=lambda c: c["scheduled"]["start"]),
                capacity_minutes=settings["capacityMinutes"],
            ))
            cursor += timedelta(days=1)

        return self._with_palette(payload_repo.days_payload(
            days, pool,
            keys=keys,
            candidates=self._candidates(tasks, keys),
            stats=self._stats(cards, tasks, keys),
            capacity_minutes=settings["capacityMinutes"],
            work_start=settings["workStart"].strftime("%H:%M"),
            work_end=settings["workEnd"].strftime("%H:%M"),
        ))

    # ---- the focus tab ----

    def _session_species_id(self) -> str | None:
        for mote in species_repo.list_species(self._conn):
            if payload_repo.name_of(mote).casefold() == SESSION_SPECIES_NAME.casefold():
                return mote.id
        return None

    def _sessions(self, directory: payload_repo.Directory) -> list[dict]:
        """Every logged session, newest first, with the task it was
        about. Read from the graph rather than a counter: a session is a
        mote, so deleting one in any window is simply a session that no
        longer happened."""
        species_id = self._session_species_id()
        if species_id is None:
            return []
        found = []
        for mote in species_repo.instances_of(self._conn, species_id):
            subject = next(iter(directory.targets(mote.id, modoc_repo.ABOUT)), None)
            found.append(payload_repo.session_payload(mote, directory, subject))
        return sorted(found, key=lambda s: s["start"] or "", reverse=True)

    @_synchronized
    def focus_tab(self, status_key=None, priority_key=None, estimate_key=None, bindings=None) -> dict:
        """The timer's side of the window: what is queued for today,
        what has been logged, and the streak that comes out of it."""
        _, tasks, _, keys, directory = self._garden_state(
            status_key=status_key, priority_key=priority_key,
            estimate_key=estimate_key, bindings=bindings,
        )
        cards = [payload_repo.card_payload(m, directory, keys) for m in tasks]
        today = date.today().strftime("%Y-%m-%d")
        queue = [
            c for c in cards
            if not payload_repo.is_done(c)
            and (c["scheduled"] or {}).get("day") == today
        ]
        sessions = self._sessions(directory)
        return self._with_palette(payload_repo.focus_payload(
            sorted(queue, key=lambda c: c["scheduled"]["start"]),
            sessions,
            keys=keys,
            candidates=self._candidates(tasks, keys),
            stats=self._stats(cards, tasks, keys),
            streak=_streak(sessions, date.today()),
            heatmap=_heatmap(sessions, date.today()),
        ))

    # ---- the detail panel ----

    def _panel(self, mote_id: str, keys: payload_repo.Keys | None = None) -> dict | None:
        """The detail panel's answer, without taking the lock -- the
        editing methods below already hold it, and threading.Lock is not
        reentrant."""
        mote = motes_repo.get_mote(self._conn, mote_id)
        if mote is None:
            return None
        motes, relationships = graph_repo.whole_garden(self._conn)
        resolved = keys or self._resolve_keys(motes)
        return payload_repo.panel_payload(
            mote, payload_repo.Directory.of(motes, relationships), resolved
        )

    @_synchronized
    def focus(self, mote_id: str) -> dict | None:
        return self._panel(mote_id)

    # ---- editing ----

    @_synchronized
    def set_status(
        self, mote_id: str, status_key: str, status: str | None, estimate_key=None,
    ) -> dict | None:
        """What dragging a card between columns means: one write, to
        whichever key the person is reading as status.

        The key is sent by the window rather than re-derived here. The
        card was drawn under one answer to "which key is status", and an
        edit of what you can see has to be an edit of the key you can
        see it by -- the same rule core/calendar.py states about writing
        back to the binding that placed a rectangle.

        Landing on a done value that the mote was not already on closes
        out its estimate first (see _log_closing_session) -- dragging a
        card to a done column with no timer running means the same
        thing as the days view's check button: whatever was left is
        logged as one last session rather than silently disappearing.
        """
        mote = motes_repo.get_mote(self._conn, mote_id)
        if mote is not None and isinstance(status, str) and payload_repo.is_done_value(status) \
                and not payload_repo.is_done_value(mote.properties.get(status_key)):
            _, _, _, keys, _ = self._garden_state(estimate_key=estimate_key, status_key=status_key)
            self._log_closing_session(mote, keys.estimate)
        if status is None:
            motes_repo.delete_property(self._conn, mote_id, status_key)
        else:
            motes_repo.update_mote(self._conn, mote_id, {status_key: status})
        return self._panel(mote_id)

    @_synchronized
    def complete_task(self, mote_id: str, status_key=None, estimate_key=None) -> dict | None:
        """The days view's check button: mark a task done with no timer
        running to say how long its last stretch took.

        Whatever estimate remains is logged as one closing session --
        the same accounting a timer's own complete button does with the
        time it actually watched go by -- and the status-bound property
        (if the garden has one) is set to the literal "complete", one of
        the three preset values this window's timer moves a task
        through on its own (see toggleTimer() and _log_closing_session).
        """
        _, _, _, keys, _ = self._garden_state(estimate_key=estimate_key, status_key=status_key)
        mote = motes_repo.get_mote(self._conn, mote_id)
        if mote is None:
            return None
        self._log_closing_session(mote, keys.estimate)
        if keys.status:
            motes_repo.update_mote(self._conn, mote_id, {keys.status: STATUS_COMPLETE})
        return self._panel(mote_id, keys)

    @_synchronized
    def create_task(self, name: str, status_key=None, status: str | None = None,
                    species_id: str | None = None) -> dict | None:
        """A task created from a board column. It starts in the column
        it was created in, and carries nothing else -- a new mote is a
        name, and everything after that is the person's."""
        properties: dict = {"name": name}
        if status_key and status:
            properties[status_key] = status
        mote_id = motes_repo.create_mote(self._conn, properties)
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

    @_synchronized
    def set_subtask(self, mote_id: str, index: int, done: bool) -> dict | None:
        """One checkbox. The whole list is rewritten because it is one
        property value -- a checklist is not a relationship, and v1
        deliberately keeps it that way (see the plan): a subtask is a
        line of text on its parent, not a mote of its own."""
        mote = motes_repo.get_mote(self._conn, mote_id)
        if mote is None:
            return None
        subtasks = payload_repo.subtasks_of(mote)
        if not 0 <= index < len(subtasks):
            return self._panel(mote_id)
        subtasks[index]["done"] = bool(done)
        motes_repo.update_mote(self._conn, mote_id, {payload_repo.SUBTASKS_KEY: subtasks})
        return self._panel(mote_id)

    @_synchronized
    def add_subtask(self, mote_id: str, text: str) -> dict | None:
        mote = motes_repo.get_mote(self._conn, mote_id)
        if mote is None:
            return None
        subtasks = payload_repo.subtasks_of(mote)
        subtasks.append({"text": text, "done": False})
        motes_repo.update_mote(self._conn, mote_id, {payload_repo.SUBTASKS_KEY: subtasks})
        return self._panel(mote_id)

    # ---- scheduling ----

    @_synchronized
    def set_working_hours(self, work_start: str, work_end: str) -> dict:
        """The earliest and latest time of day `schedule()` is allowed to
        land a task -- the same `work_start`/`work_end` _settings() reads
        off the garden's "Momentum" mote, written back onto it (creating
        it the first time a garden sets this). An invalid time for
        either argument keeps that one as it was rather than raising: the
        rail's inputs are free text, and a garden that types something
        `time.fromisoformat` cannot read should not lose its other value
        over it.
        """
        _, tasks, _, _, _ = self._garden_state()
        settings = self._settings(tasks)
        updates = {
            "work_start": _parse_time(work_start, settings["workStart"]).strftime("%H:%M"),
            "work_end": _parse_time(work_end, settings["workEnd"]).strftime("%H:%M"),
        }
        if settings["settingsId"]:
            motes_repo.update_mote(self._conn, settings["settingsId"], updates)
        else:
            motes_repo.create_mote(self._conn, {"name": SETTINGS_NAME, **updates})
        return {"workStart": updates["work_start"], "workEnd": updates["work_end"]}

    def _binding_to_write(self, mote: Mote, keys: payload_repo.Keys, motes: list[Mote]) -> str:
        """Which key a drop onto a day writes.

        The mote's own, if it already carries one of the active bindings
        -- rescheduling should move the date you can see, not leave it
        standing and add a second one beside it. Otherwise the most-used
        of the bindings the board is currently showing -- a drop should
        fill in a property the board is already bound to, never invent
        one the board isn't even displaying. Only a board with no active
        bindings at all falls back to the global fallback key.
        """
        for binding in keys.bindings:
            if calendar_repo.parse_date_value(mote.properties.get(binding)) is not None:
                return binding
        if keys.bindings:
            counts = calendar_repo.binding_counts(motes)
            return sorted(keys.bindings, key=lambda k: (-counts.get(k, 0), k))[0]
        return FALLBACK_BINDING

    def _place_in_earliest_slot(
        self, mote: Mote, binding: str, day: date, minutes: float,
        visible: list[Mote], excluded: set[str],
        keys: payload_repo.Keys, settings: dict,
    ) -> tuple[datetime, bool]:
        """The earliest free start on `day` for `minutes` of work under
        `binding`, written onto the mote -- what "the earliest unblocked
        position" means everywhere this window places a task, whether
        that is a drag onto a day (schedule()) or a bare date catching up
        to a real time on its own (_auto_place()). One write to one key:
        the span rides inside the value, so an estimate the mote made
        becomes the length of the block rather than scheduling having to
        decide whether it may also touch a separate `duration`. That the
        span is *rewritten* is why both callers have to be sure the
        placement was asked for -- a drag is a request to re-place, and
        _auto_place() checks that nothing was declared before it writes.

        The gap is found against everything the calendar considers busy
        that day, through core/calendar.py, so a task never lands on top
        of a meeting Monastery would draw. A day with no room left is
        not an error -- the task is placed at the start of the working
        day and the caller says so, because refusing to schedule
        something is not this window's decision to make.
        """
        slot = calendar_repo.earliest_free_slot(
            [m for m in visible if m.id != mote.id],
            day=day,
            duration=timedelta(minutes=minutes),
            bindings=list(keys.bindings) or [FALLBACK_BINDING],
            work_start=settings["workStart"],
            work_end=settings["workEnd"],
            exclude_ids=excluded,
        )
        full = slot is None
        start = slot or datetime.combine(day, settings["workStart"])

        existing = interval_repo.parse(mote.properties.get(binding))
        placed = interval_repo.Interval(
            start=start,
            end=start + timedelta(minutes=minutes),
            all_day=False,
            rule_lines=existing.rule_lines if existing else (),
        )
        written = interval_repo.format(placed) if existing is None else \
            interval_repo.format_like(mote.properties[binding], placed, force_span=True)
        motes_repo.update_mote(self._conn, mote.id, {binding: written})
        # Kept in step so a second mote placed in the same call -- or the
        # card this same request renders -- sees the slot as taken and
        # already timed, rather than a moment-old snapshot.
        mote.properties[binding] = written
        return start, full

    def _auto_place(
        self, tasks: list[Mote], visible: list[Mote], excluded: set[str],
        keys: payload_repo.Keys, settings: dict,
        window_start: date, window_end: date,
    ) -> None:
        """Give every task that carries only a bare date -- no time -- a
        real start and end, in the earliest gap its day still has.

        A mote can arrive with just a date and nothing else, set through
        the MCP tools or any other window rather than dragged onto a day
        here. Leaving it as an all-day block until someone happens to
        drag it would mean "the estimate decides the block's length" is
        only true for tasks scheduled one particular way. Doing this on
        every days() read means it converges here, the same view that
        already reads and writes the placement a drag makes.

        Three things this deliberately will not touch, because giving a
        day a time is a *convenience* and none of them asked for it:

        - **A span someone declared.** An all-day value lasting more than
          the default day is a booking, not an undated intention -- a
          conference held across three days means those three days, and
          folding it into an hour at 09:00 would delete what the value
          said. Only a plain single day is a placement waiting to happen.
        - **Anything outside the drawn window.** This runs on a read, so
          it may only write where that read is looking. A task next March
          is not this week's business.
        - **A task already done.** A finished task has no work left to
          find a gap for, and placing one would spend a morning's room on
          it -- pushing the tasks that *are* open later down the day.
        """
        for mote in tasks:
            if keys.status and payload_repo.is_done_value(mote.properties.get(keys.status)):
                continue
            for binding in keys.bindings:
                existing = interval_repo.parse(mote.properties.get(binding))
                if existing is None:
                    continue
                day = existing.start.date()
                if existing.all_day \
                        and existing.span == timedelta(days=interval_repo.DEFAULT_ALL_DAY_DAYS) \
                        and window_start <= day < window_end:
                    minutes = candidates_repo.parse_estimate_value(
                        mote.properties.get(keys.estimate) if keys.estimate else None
                    ) or DEFAULT_SLOT_MINUTES
                    self._place_in_earliest_slot(
                        mote, binding, day, minutes, visible, excluded, keys, settings
                    )
                break

    @_synchronized
    def schedule(
        self, mote_id: str, day: str,
        status_key=None, priority_key=None, estimate_key=None, bindings=None,
    ) -> dict | None:
        """Drop a task on a day: the person picks the day, the earliest
        free gap picks the time. See _place_in_earliest_slot() for what
        that means.
        """
        visible, tasks, excluded, keys, _ = self._garden_state(
            status_key=status_key, priority_key=priority_key,
            estimate_key=estimate_key, bindings=bindings,
        )
        mote = motes_repo.get_mote(self._conn, mote_id)
        if mote is None:
            return None
        settings = self._settings(tasks)
        target_day = date.fromisoformat(day)

        estimated = candidates_repo.parse_estimate_value(
            mote.properties.get(keys.estimate) if keys.estimate else None
        )
        minutes = estimated or DEFAULT_SLOT_MINUTES
        binding = self._binding_to_write(mote, keys, tasks)
        start, full = self._place_in_earliest_slot(
            mote, binding, target_day, minutes, visible, excluded, keys, settings
        )

        panel = self._panel(mote_id, keys)
        return {**(panel or {}), "slotFull": full, "binding": binding,
                "start": start.isoformat()}

    @_synchronized
    def unschedule(self, mote_id: str, bindings=None) -> dict | None:
        """Back to the pool: every active binding the mote carries is
        removed, because the pool means "not scheduled" and leaving one
        of two dates behind would put the card straight back on the
        week."""
        _, _, _, keys, _ = self._garden_state(bindings=bindings)
        mote = motes_repo.get_mote(self._conn, mote_id)
        if mote is None:
            return None
        for binding in keys.bindings:
            if calendar_repo.parse_date_value(mote.properties.get(binding)) is not None:
                motes_repo.delete_property(self._conn, mote_id, binding)
        return self._panel(mote_id, keys)

    # ---- the timer ----

    def _record_session(self, subject: Mote | None, minutes: float, start: datetime) -> str:
        """The mote a finished session becomes: an ordinary mote carrying
        `date` and `duration`, an instance of a `Work Session` species,
        pointing at the task with `about` (the same relationship a Mora
        note uses for its subject). Being ordinary is what makes it show
        up in Monastery and Monarch without either window knowing this
        one exists. Shared by log_session() and _log_closing_session(),
        which differ only in where `start` and `minutes` come from -- a
        watched timer for one, whatever estimate was left for the other.
        """
        name = payload_repo.name_of(subject) if subject else "Work session"
        species_id = self._session_species_id()
        if species_id is None:
            species_id = motes_repo.create_mote(self._conn, {"name": SESSION_SPECIES_NAME})
        session_id = motes_repo.create_mote(self._conn, {
            "name": name,
            "date": interval_repo.format(interval_repo.Interval(
                start=start, end=start + timedelta(minutes=minutes), all_day=False,
            )),
        })
        species_repo.set_species(self._conn, session_id, species_id)
        if subject is not None:
            rel_repo.create_relationship(self._conn, session_id, subject.id, modoc_repo.ABOUT)
        return session_id

    def _deduct_estimate(self, mote: Mote, estimate_key: str, minutes: float, *, floor: bool) -> None:
        """A logged session's minutes taken back off the task's own
        estimate -- the number is what's left to do, not what was
        planned, once work against it has actually happened.

        Stays at or above ESTIMATE_FLOOR_MINUTES while `floor` is set,
        which callers pass as "the task is not being completed by this
        same write" -- the last quarter hour of a task nobody has said
        is finished yet is still owed, even if the session that just
        finished ran longer than what was left on the clock.

        Written back in the unit it was read in, the same courtesy
        core/interval.py::format_like does for a date: a task estimated
        at `5h` comes back as `4h`, not as `275`.
        """
        raw = mote.properties.get(estimate_key)
        current = candidates_repo.parse_estimate_value(raw)
        if current is None:
            return
        floor_minutes = ESTIMATE_FLOOR_MINUTES if floor else 0
        remaining = max(current - minutes, floor_minutes)
        motes_repo.update_mote(
            self._conn, mote.id,
            {estimate_key: candidates_repo.format_estimate_like(raw, remaining)},
        )

    def _log_closing_session(self, mote: Mote, estimate_key: str | None) -> None:
        """What marking a task done means for whatever estimate is still
        on it, with no timer around to say how long that last stretch
        actually took: log it as one session ending now, so the hour a
        task took to finish is not silently lost the moment it is marked
        complete, then zero the estimate -- ESTIMATE_FLOOR_MINUTES stops
        applying once the task is the thing declaring itself finished.

        The session is anchored to end now, but never to start before
        this morning. A session is filed under the day it starts, so a
        task carrying a fortnight's estimate would otherwise land its
        closing entry on some day last week -- crediting the streak and
        the heatmap for work that was recorded today.
        """
        if not estimate_key:
            return
        remaining = candidates_repo.parse_estimate_value(mote.properties.get(estimate_key))
        if not remaining or remaining <= 0:
            return
        now = datetime.now()
        start = max(now - timedelta(minutes=remaining), datetime.combine(now.date(), time.min))
        self._record_session(mote, remaining, start)
        motes_repo.update_mote(
            self._conn, mote.id,
            {estimate_key: candidates_repo.format_estimate_like(mote.properties[estimate_key], 0)},
        )

    @_synchronized
    def log_session(
        self, mote_id: str | None, minutes: float, started_at: str | None = None,
        complete: bool = False, estimate_key=None, status_key=None,
    ) -> dict:
        """Write down a finished session.

        Only a *finished* one. A running countdown is the window's own
        state and nothing is written until it stops -- the same
        preview/commit split core/simulation.py keeps, and for the same
        reason: a session that was never completed describes work that
        never happened.

        `complete` is the timer's own complete button: the same session
        that would otherwise just be logged also closes the task out --
        its estimate is deducted with no floor, and if the garden has a
        status-bound property it is set to the literal "complete".
        """
        if minutes <= 0:
            return {"logged": False}
        start = datetime.fromisoformat(started_at) if started_at else datetime.now()
        subject = motes_repo.get_mote(self._conn, mote_id) if mote_id else None
        session_id = self._record_session(subject, minutes, start)

        if subject is not None:
            _, _, _, keys, _ = self._garden_state(estimate_key=estimate_key, status_key=status_key)
            if keys.estimate:
                self._deduct_estimate(subject, keys.estimate, minutes, floor=not complete)
            if complete and keys.status:
                motes_repo.update_mote(self._conn, subject.id, {keys.status: STATUS_COMPLETE})

        return {"logged": True, "id": session_id, "minutes": round(minutes),
                "subject": subject.id if subject else None}

    @_synchronized
    def delete_session(self, session_id: str) -> bool:
        return motes_repo.delete_mote(self._conn, session_id)

    # ---- relationships ----

    @_synchronized
    def mote_relationships(self, mote_id: str) -> list[dict]:
        return [
            {"id": r.id, "type": r.type, "from_id": r.from_id, "to_id": r.to_id}
            for r in graph_repo.relationships_touching(self._conn, mote_id)
        ]

    @_synchronized
    def link(self, from_id: str, to_id: str, type_: str) -> dict | None:
        """An owner, a dependency, a blocker -- all one operation,
        because all three are the same thing: a typed edge between two
        motes. Nothing here validates the type against a list; a garden
        may say `reviewed_by` and Momentum will draw it in the panel
        beside the rest."""
        rel_repo.create_relationship(self._conn, from_id, to_id, type_)
        return self._panel(from_id)

    @_synchronized
    def unlink(self, rel_id: str, mote_id: str) -> dict | None:
        rel_repo.delete_relationship(self._conn, rel_id)
        return self._panel(mote_id)

    @_synchronized
    def search_motes(self, query: str, limit: int = 20) -> list[dict]:
        """For the panel's link picker. The same search the other
        windows use, so finding a person to assign a task to works the
        way finding anything else does."""
        motes, relationships = graph_repo.whole_garden(self._conn)
        directory = payload_repo.Directory.of(motes, relationships)
        return [
            directory.describe(m.id)
            for m in motes_repo.search_motes(self._conn, query, limit=limit)
        ]

    @_synchronized
    def list_species(self) -> list[dict]:
        motes, relationships = graph_repo.whole_garden(self._conn)
        directory = payload_repo.Directory.of(motes, relationships)
        return sorted(
            (directory.describe(m.id) for m in species_repo.list_species(self._conn)),
            key=lambda e: e["name"].casefold(),
        )


def _explicit(value):
    """What the window sent for a key it is choosing itself. `False` and
    `""` both mean "nothing" -- the window has to be able to say that a
    key is switched off, which None already means "you decide"."""
    return value or None


def _streak(sessions: list[dict], today: date) -> int:
    """Consecutive days ending today with at least one session on them.

    Today not having one yet does not break the streak -- it is still
    early. Yesterday not having one does. Any other reading would make
    the number drop every morning and climb back every afternoon.
    """
    days = {s["day"] for s in sessions if s["day"]}
    if not days:
        return 0
    cursor = today
    if cursor.strftime("%Y-%m-%d") not in days:
        cursor = today - timedelta(days=1)
    streak = 0
    while streak < STREAK_LIMIT_DAYS and cursor.strftime("%Y-%m-%d") in days:
        streak += 1
        cursor -= timedelta(days=1)
    return streak


def _heatmap(sessions: list[dict], today: date) -> list[dict]:
    """The last fortnight, oldest first: how much was logged each day."""
    minutes: dict[str, float] = {}
    counts: dict[str, int] = {}
    for session in sessions:
        if not session["day"]:
            continue
        minutes[session["day"]] = minutes.get(session["day"], 0) + session["minutes"]
        counts[session["day"]] = counts.get(session["day"], 0) + 1
    found = []
    for offset in range(HEATMAP_DAYS - 1, -1, -1):
        day = (today - timedelta(days=offset)).strftime("%Y-%m-%d")
        found.append({
            "day": day,
            "minutes": minutes.get(day, 0),
            "sessions": counts.get(day, 0),
            "today": offset == 0,
        })
    return found

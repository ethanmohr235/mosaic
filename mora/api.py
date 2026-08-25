# mora/api.py
"""JS-facing bridge for Mora, the .modoc editor.

A fourth adapter over core/, alongside cli/, monarch/ and mcp_server/ -- not
part of monarch/, because Mora is its own window with its own concerns and
sharing monarch/api.py would mean sharing a garden connection between two
windows that open and close independently.

It follows monarch/api.py's shape deliberately: one long-lived connection for
the open garden, held as instance state, with every exposed method
serialized behind a lock because pywebview calls in from a pool of
worker threads.

Two lines this bridge does not cross. It holds no knowledge of the
.modoc language -- parsing is core/modoc.py's, resolving is
core/modoc_bind.py's, and shaping the answer is mora/payload.py's. And
it writes exactly two things: a note's source, and a simulation step the
user explicitly applied. Everything else here reads.
"""
import functools
import logging
import threading

import webview

from core import gardens as gardens_repo
from core import graph as graph_repo
from core import modoc
from core import modoc_bind
from core import motes as motes_repo
from core import simulation as simulation_repo
from core import sync as sync_repo
from core.db import get_connection, initialize_database
from mora import payload as payload_repo

logger = logging.getLogger(__name__)

MAX_STEP = 60
_SEARCH_LIMIT = 12
# How long a write waits out a lock held by another process before
# giving up. Mora is opened *from* Monarch (monarch/api.py::
# open_mora), so two processes holding the same garden file open at
# once is the normal case here, not the exception -- the same reason
# mcp_server/server.py sets this. core/db.py stays untouched, so the
# CLI and the main UI keep their current behavior exactly.
_BUSY_TIMEOUT_MS = 5000


def _synchronized(method):
    """pywebview invokes exposed methods from a worker thread, and not
    the same one each time. The connection is opened check_same_thread=
    False, so serializing here is this class's job -- the same reason
    monarch/api.py has this decorator."""
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapper


class Api:
    """Every method takes the note's *live* source rather than reading it
    back from the database. The source pane is the truth while you type;
    the property only catches up when you save. Resolving what is on
    screen is the entire point of the rendered pane, so the unsaved text
    has to be what gets resolved."""

    def __init__(self, garden: str | None = None, note: str | None = None):
        self._lock = threading.Lock()
        self._garden = gardens_repo.resolve_garden(garden)
        # `run_mora.py --note <id>`: the frontend opens whatever it was
        # last on, so the command line has to be able to say otherwise.
        self._initial_note = note
        self._conn = self._open(self._garden)
        self._window: webview.Window | None = None
        self._best_effort_sync()

    def _open(self, garden: str):
        conn = get_connection(garden)
        conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        initialize_database(conn)
        return conn

    def _best_effort_sync(self) -> None:
        """Same call, same reasoning, as monarch/api.py::Api._best_effort_sync:
        run_mora.py is its own launch path, not only ever opened from the
        graph window, so a note edited here must converge on its own
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

    # ---- notes ----

    @_synchronized
    def list_notes(self) -> list[dict]:
        """Every mote carrying a note.

        Asked of the property, not of core/kinds.py, because carrying a
        note is deliberately not a kind: every kind in that table is a
        job a mote does in the graph's machinery, and `modoc` changes
        nothing about how a mote is treated -- it is data, like `url` or
        `date`, neither of which is a kind either. Making it one meant a
        project that gained a note stopped reading as a project, since a
        kind displaces the species in the main app's type label."""
        motes, relationships = graph_repo.whole_garden(self._conn)
        directory = payload_repo.Directory.of(motes, relationships)
        notes = [directory.describe(m.id) for m in motes if modoc.has_modoc(m.properties)]
        return sorted(notes, key=lambda n: n["name"].casefold())

    @_synchronized
    def open_note(self, mote_id: str | None = None) -> dict:
        """The note to start on: the one asked for, else the first note in
        the garden, else the most recent mote -- which opens as an empty
        page rather than as an error, because a mote with no note yet is
        exactly where you'd want to start writing one."""
        if mote_id is None:
            mote_id = self._initial_note
        if mote_id is None or motes_repo.get_mote(self._conn, mote_id) is None:
            mote_id = self._default_note()
        if mote_id is None:
            return {"noteId": None, "source": "", "name": ""}
        mote = motes_repo.get_mote(self._conn, mote_id)
        return {
            "noteId": mote_id,
            "source": modoc.read_source(self._conn, mote_id),
            "name": payload_repo.name_of(mote),
        }

    def _default_note(self) -> str | None:
        motes, _ = graph_repo.whole_garden(self._conn)
        existing = [m.id for m in motes if modoc.has_modoc(m.properties)]
        return existing[0] if existing else graph_repo.most_recent_mote_id(self._conn)

    @_synchronized
    def save_note(self, mote_id: str, source: str) -> dict:
        """Writes the `modoc` property and nothing else -- saving a note
        must never be a way to write simulation results (that is
        apply_step, below, and it asks first)."""
        modoc.write_source(self._conn, mote_id, source)
        return {"noteId": mote_id, "saved": True}

    @_synchronized
    def render(self, mote_id: str, source: str, step: int = 0) -> dict:
        """The rendered pane, the data panel's link list and the status
        bar's counts, for the source as it stands right now."""
        step = max(0, min(int(step), MAX_STEP))
        document = modoc.parse(source)
        resolution = modoc_bind.resolve(self._conn, mote_id, document, step=step)
        motes, relationships = graph_repo.whole_garden(self._conn)
        directory = payload_repo.Directory.of(motes, relationships)
        return payload_repo.note_payload(document, resolution, directory, source)

    # ---- the data panel ----

    @_synchronized
    def focus(self, mote_id: str) -> dict | None:
        mote = motes_repo.get_mote(self._conn, mote_id)
        if mote is None:
            return None
        motes, relationships = graph_repo.whole_garden(self._conn)
        return payload_repo.panel_payload(mote, payload_repo.Directory.of(motes, relationships))

    @_synchronized
    def search(self, query: str, limit: int = _SEARCH_LIMIT) -> list[dict]:
        """Backs the `@` link picker. core/motes.py::search_motes is the
        same search the main app's palette and the MCP tool use, so a
        query finds the same motes wherever it's typed."""
        motes, relationships = graph_repo.whole_garden(self._conn)
        directory = payload_repo.Directory.of(motes, relationships)
        found = motes_repo.search_motes(self._conn, query, limit)
        return [directory.describe(m.id) for m in found]

    # ---- simulation ----

    @_synchronized
    def apply_step(self, step: int) -> dict:
        """The one destructive thing in this window, and the only reason
        it is separate from Save: stepping the clock is a projection until
        somebody decides it isn't. Commits the same run the reader has
        been looking at -- run_time_series is deterministic given the same
        garden, so re-solving it here reproduces exactly those numbers."""
        step = int(step)
        if step < 1:
            raise ValueError("Nothing to apply at t+0 -- that is the garden as it stands.")
        if step > MAX_STEP:
            # Same bound render() clamps a preview to. The frontend never
            # asks past MAX_STEP either (see mora/frontend/app.js), but
            # this method commits to the graph, so it checks for itself
            # rather than trusting the caller stayed in range.
            raise ValueError(f"Can't apply past t+{MAX_STEP} -- that's the simulation preview's own limit.")
        result = simulation_repo.run_time_series(self._conn, step, commit=True)
        return {
            "steps": simulation_repo.step_payloads(result.steps),
            "failed_at": result.failed_at,
            "error": result.error,
            "run_id": result.run_id,
            "run_number": result.run_number,
        }

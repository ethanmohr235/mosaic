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
it writes exactly one thing: a note's source. Everything else here
reads.

It used to write a second: a simulation step the reader had projected to
and then applied. That is gone with the `t+N` stepper it belonged to.
Mora shows the garden as it stands; moving a garden through time is
Motion's window, and a note that could commit a run was a note editor
holding a lever that belonged somewhere else.
"""
import logging
import threading

import webview

from api_common import CommandBarApi, InspectorApi, UndoableApi
from api_common import open_app as _open_app
from api_common import open_gardens as _open_gardens
from api_common import species_ids as _species_ids
from api_common import synchronized as _synchronized
from core import gardens as gardens_repo
from core import graph as graph_repo
from core import modoc
from core import modoc_bind
from core import motes as motes_repo
from core import plotview
from core import sync as sync_repo
from core import undo
from core.db import get_connection, initialize_database
from mora import payload as payload_repo

logger = logging.getLogger(__name__)

_SEARCH_LIMIT = 12
# How long a write waits out a lock held by another process before
# giving up. Mora is opened *from* Monarch (monarch/api.py::
# open_mora), so two processes holding the same garden file open at
# once is the normal case here, not the exception -- the same reason
# mcp_server/server.py sets this. core/db.py stays untouched, so the
# CLI and the main UI keep their current behavior exactly.
_BUSY_TIMEOUT_MS = 5000


# Whether anything at all has been written to the garden. Counts as well
# as timestamps, because a delete lowers the count without moving the
# most recent write.
_FINGERPRINT_SQL = """
SELECT (SELECT COUNT(*) FROM mote_properties),
       (SELECT MAX(updated_at) FROM mote_properties),
       (SELECT COUNT(*) FROM relationships),
       (SELECT MAX(updated_at) FROM relationships)
"""


class _Figures:
    """Every `\\graph{...}` in the open note, drawn once per state of the
    garden.

    A figure is a saved Moxie view re-solved (core/plotview.py), which is
    the most expensive thing a note can ask for by a wide margin -- a
    sweep is dozens of solves and a surface is hundreds. Mora re-resolves
    the whole note on every pause in typing, and typing does not move the
    garden, so without this every keystroke would re-run every sweep in
    the document for an answer that could not have changed.

    Invalidation is the garden's own last write rather than a list of
    hooks hung off each method that writes. A property edited in the
    floating inspector, a run applied from the footer, a value that
    arrived over sync, and an edit made in Monarch while this window sat
    open all move that fingerprint equally -- and none of them has to
    remember to say so, which is what a list of hooks would require and
    would eventually get wrong."""

    def __init__(self) -> None:
        self._drawn: dict[str, plotview.Figure] = {}
        self._at: tuple | None = None

    def __call__(self, conn, ref: str, *, motes=None) -> plotview.Figure:
        fingerprint = tuple(conn.execute(_FINGERPRINT_SQL).fetchone())
        if fingerprint != self._at:
            self._drawn.clear()
            self._at = fingerprint
        if ref not in self._drawn:
            self._drawn[ref] = plotview.figure(conn, ref, motes=motes)
        return self._drawn[ref]


class Api(InspectorApi, CommandBarApi, UndoableApi):
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
        self._figures = _Figures()
        self._init_undo()
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
        # A view is named per garden and a mote id means nothing in
        # another one, so the memo does not travel with the window.
        self._figures = _Figures()
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
        notes = []
        for m in motes:
            if not modoc.has_modoc(m.properties):
                continue
            entry = directory.describe(m.id)
            source = m.properties.get(modoc.MODOC_KEY) or ""
            entry["text"] = modoc.plain_text(modoc.parse(source))
            notes.append(entry)
        return sorted(notes, key=lambda n: n["name"].casefold())

    @_synchronized
    def create_note(self, name: str | None = None) -> dict:
        """A fresh mote to start typing on, returned in open_note()'s
        shape so the caller can drop straight into loadNote().

        Writes an empty `modoc` property rather than leaving it unset --
        that is what list_notes() and has_modoc() key on, so a note
        created here shows up in the note switcher immediately rather
        than only after the first save."""
        properties = {modoc.MODOC_KEY: ""}
        if name:
            properties["name"] = name
        mote_id = motes_repo.create_mote(self._conn, properties)
        self.undo_manager.push(undo.for_create_mote(self._conn, mote_id, properties, label="create a note"))
        mote = motes_repo.get_mote(self._conn, mote_id)
        return {"noteId": mote_id, "source": "", "name": payload_repo.name_of(mote)}

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
        """Writes the `modoc` property and nothing else. It is the only
        write this bridge makes."""
        modoc.write_source(self._conn, mote_id, source)
        return {"noteId": mote_id, "saved": True}

    @_synchronized
    def render(self, mote_id: str, source: str) -> dict:
        """The rendered pane, the data panel's link list and the status
        bar's counts, for the source as it stands right now -- against
        the garden as *it* stands right now, which is the only reading a
        note has."""
        document = modoc.parse(source)
        resolution = modoc_bind.resolve(
            self._conn, mote_id, document, draw=self._figures,
        )
        motes, relationships = graph_repo.whole_garden(self._conn)
        directory = payload_repo.Directory.of(motes, relationships)
        return {
            **payload_repo.note_payload(document, resolution, directory, source),
            # The palette universe rides along with the answer rather
            # than being a call of its own -- see api_common.species_ids.
            "speciesIds": _species_ids(self._conn),
        }

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

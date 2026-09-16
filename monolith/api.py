# monolith/api.py
"""JS-facing bridge for Monolith, the table view.

A fifth adapter over core/, following mora/api.py's shape exactly: one
long-lived connection for the open garden held as instance state, every
exposed method serialized behind a lock because pywebview calls in from
a pool of worker threads, and a best-effort sync on the way in and out.

Two lines this bridge does not cross. It holds no knowledge of the query
language -- parsing, matching, ordering and grouping are core/query.py's,
and shaping the answer is monolith/payload.py's. And every write it makes
goes through core/motes.py or core/relationships.py one primitive at a
time, so a bulk edit across 200 motes is 200 ordinary writes that the
oplog replicates like any other -- there is no bulk path in the store for
this window to have invented.

Unlike the other four windows, this one writes a *lot*: the whole point
is editing many motes at once. The mitigation is not fewer writes but
narrower ones -- set_property touches exactly the key it was given on
exactly the ids it was given, and delete_motes hands back an undo token
per mote rather than gating the click on a confirmation dialog, so a
click that landed on a 200-row selection is one toast away from undone
instead of one dialog away from prevented.
"""
import logging
import sqlite3
import threading

import webview

from api_common import CommandBarApi, InspectorApi
from api_common import coerce as _coerce
from api_common import open_app as _open_app
from api_common import open_gardens as _open_gardens
from api_common import species_ids as _species_ids
from api_common import synchronized as _synchronized
from core import gardens as gardens_repo
from core import graph as graph_repo
from core import motes as motes_repo
from core import query as query_repo
from core import relationships as relationships_repo
from core import species as species_repo
from core import sync as sync_repo
from core.db import get_connection, initialize_database
from core.entities import Mote, Relationship
from core.query import Index, Query
from monolith import payload as payload_repo

logger = logging.getLogger(__name__)

_SEARCH_LIMIT = 20

# The species a saved view is an instance of, found or created by name --
# same trick momentum/api.py plays for SESSION_SPECIES_NAME: an ordinary
# mote, discovered rather than reserved, so a garden that has never saved
# a view never grows one.
VIEW_SPECIES_NAME = "Saved View"

# Same reasoning as mora/api.py: Monolith is opened *from* another window
# as often as on its own, so two processes holding the same garden file
# open at once is the normal case here, not the exception.
_BUSY_TIMEOUT_MS = 5000


class Api(InspectorApi, CommandBarApi):
    """Every read re-reads the whole garden. That is deliberate and it is
    what the other windows do too (monarch/api.py::focus,
    mora/api.py::render): a table showing a stale row after somebody
    else's edit is worse than a read that costs a few milliseconds, and
    the answer has to be consistent across columns that are discovered
    from the same pass that fills them."""

    def __init__(self, garden: str | None = None, view: str | None = None):
        self._lock = threading.Lock()
        self._garden = gardens_repo.resolve_garden(garden)
        # `run_monolith.py --view <id>`: the frontend otherwise opens on
        # whatever query it was last showing.
        self._initial_view = view
        self._conn = self._open(self._garden)
        self._window: webview.Window | None = None
        self._best_effort_sync()

    def _open(self, garden: str):
        conn = get_connection(garden)
        conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        initialize_database(conn)
        return conn

    def _best_effort_sync(self) -> None:
        """run_monolith.py is its own launch path, so an edit made here
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
        """Manual refresh: sync (if configured), then let the caller
        re-fetch the table. An unconfigured sync is not an error --
        refresh is also just "reload from disk"."""
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

    # ---- reading the garden ----

    def _read(self) -> tuple[list[Mote], list[Relationship], Index]:
        motes, relationships = graph_repo.whole_garden(self._conn)
        return motes, relationships, Index.of(motes, relationships, self._updated())

    def _updated(self) -> dict[str, str]:
        """When each mote last changed, from the timestamp core/motes.py
        already writes on every property. One grouped query rather than
        one per row -- this feeds a column, and a column is every mote."""
        rows = self._conn.execute(
            "SELECT mote_id, MAX(updated_at) AS updated FROM mote_properties GROUP BY mote_id"
        ).fetchall()
        return {row["mote_id"]: row["updated"] for row in rows if row["updated"]}

    @_synchronized
    def table(self, raw_query: dict | None = None) -> dict:
        """The window's whole answer in one call: the discovered columns,
        the rows the query selects, the saved views, and the garden's
        name. One call rather than four because they must agree -- a
        column list from before an edit and rows from after it would
        draw a table with a missing column."""
        motes, relationships, index = self._read()
        schema = payload_repo.Schema.of(index, motes, relationships)
        the_query = self._resolve_query(raw_query, schema, motes)
        return {
            "garden": self._garden,
            "schema": schema.to_json(),
            "query": the_query.to_json(),
            "summary": payload_repo.summarize(index, the_query),
            "views": self._views(motes, index),
            # The palette universe rides along with the answer rather
            # than being a call of its own -- see api_common.species_ids.
            "speciesIds": _species_ids(self._conn),
            **payload_repo.table_payload(index, schema, the_query, motes, relationships),
        }

    def _resolve_query(self, raw_query, schema, motes: list[Mote]) -> Query:
        """What to show: what the caller asked for, else the view named
        on the command line, else a default read off the garden itself."""
        if raw_query:
            return Query.from_json(raw_query)
        if self._initial_view:
            for mote in motes:
                if mote.id == self._initial_view and query_repo.has_query(mote.properties):
                    return Query.from_json(mote.properties[query_repo.QUERY_KEY])
        return payload_repo.default_query(schema)

    @_synchronized
    def focus(self, mote_id: str) -> dict | None:
        mote = motes_repo.get_mote(self._conn, mote_id)
        if mote is None:
            return None
        _motes, relationships, index = self._read()
        return payload_repo.panel_payload(mote, index, relationships)

    @_synchronized
    def search(self, text: str, limit: int = _SEARCH_LIMIT) -> list[dict]:
        """Backs the "link these to…" picker. core/motes.py::search_motes
        is the same search Monarch's palette and the MCP tool use, so a
        query finds the same motes wherever it is typed."""
        _motes, _relationships, index = self._read()
        found = motes_repo.search_motes(self._conn, text, limit)
        return [payload_repo.describe(index, m.id) for m in found]

    # ---- editing properties ----

    @_synchronized
    def set_property(self, mote_ids: list[str], key: str, raw_value) -> dict:
        """The bulk edit. One key, on exactly the ids given, with the
        value read as JSON where it parses (api_common.coerce) so that
        typing 90 into a duration column stores a number rather than a
        string core/calendar.py would ignore."""
        if not key:
            raise ValueError("A property needs a key.")
        value = _coerce(raw_value)
        written = self._existing(mote_ids)
        for mote_id in written:
            motes_repo.update_mote(self._conn, mote_id, {key: value})
        return {"written": len(written), "key": key, "value": value}

    @_synchronized
    def clear_property(self, mote_ids: list[str], key: str) -> dict:
        """Deleting the key, not writing an empty string -- "is empty"
        has to mean the same thing after a clear as it does before one
        was ever set."""
        cleared = [m for m in self._existing(mote_ids)
                   if motes_repo.delete_property(self._conn, m, key)]
        return {"cleared": len(cleared), "key": key}

    def _existing(self, mote_ids) -> list[str]:
        """Ids that are still there. A selection outlives the rows it was
        made on -- another window may have deleted one since -- and a
        bulk edit should write the other 199 rather than raise."""
        ids = [i for i in (mote_ids or []) if isinstance(i, str)]
        return [i for i in ids if motes_repo.get_mote(self._conn, i) is not None]

    @_synchronized
    def create_mote(self, raw_properties: dict | None = None) -> dict:
        """A blank row. Returned as an id rather than as a row, because
        where it lands depends on the query -- the caller re-fetches."""
        properties = {k: _coerce(v) for k, v in (raw_properties or {}).items()}
        return {"id": motes_repo.create_mote(self._conn, properties)}

    @_synchronized
    def delete_motes(self, mote_ids: list[str]) -> dict:
        """The one destructive thing in this window, and the reason it
        hands back an undo token per mote instead of asking first: every
        other action here is reversible by editing the cell back, and a
        confirmation dialog was the wrong shape for a bulk selection that
        might be one row or two hundred. undo_delete() below is what
        makes skipping that dialog safe.

        Relationships are recorded before the delete, not after -- they
        cascade with the mote (core/db.py's ON DELETE CASCADE), so this
        is the last moment they can be read."""
        undo = []
        for mote_id in self._existing(mote_ids):
            mote = motes_repo.get_mote(self._conn, mote_id)
            relationships = [
                {"id": r.id, "fromId": r.from_id, "toId": r.to_id, "type": r.type}
                for r in graph_repo.relationships_touching(self._conn, mote_id)
            ]
            if motes_repo.delete_mote(self._conn, mote_id):
                undo.append({"moteId": mote_id, "properties": mote.properties, "relationships": relationships})
        return {"deleted": len(undo), "undo": undo}

    @_synchronized
    def undo_delete(self, tokens: list[dict]) -> dict:
        """Put back exactly what delete_motes() took, under the same ids
        -- create_mote() and create_relationship() both accept one -- so
        every arrow that pointed at a restored mote still points at it."""
        restored = 0
        for token in tokens or []:
            if not isinstance(token, dict) or not token.get("moteId"):
                continue
            motes_repo.create_mote(self._conn, token.get("properties") or {}, id=token["moteId"])
            for relationship in token.get("relationships") or []:
                try:
                    relationships_repo.create_relationship(
                        self._conn, relationship["fromId"], relationship["toId"],
                        relationship["type"], id=relationship["id"],
                    )
                except sqlite3.IntegrityError:
                    # The mote at the other end went away while the undo
                    # toast was still up. Restoring the rest is better
                    # than refusing the whole undo over one dangling arrow.
                    logger.warning("could not restore relationship %r", relationship.get("id"))
            restored += 1
        return {"restored": restored}

    # ---- editing relationships ----

    @_synchronized
    def relate(self, pairs: list[list[str]], type_: str) -> dict:
        """Write one relationship per pair, skipping the ones that are
        already there. Backs three gestures that are the same write:
        setting a `→ depends_on` cell, painting a matrix cell, and the
        bulk bar's chain/fan-in."""
        if not type_:
            raise ValueError("A relationship needs a type.")
        existing = self._pairs_of_type(type_)
        written = 0
        for from_id, to_id in self._valid(pairs):
            if (from_id, to_id) in existing:
                continue
            relationships_repo.create_relationship(self._conn, from_id, to_id, type_)
            existing.add((from_id, to_id))
            written += 1
        return {"written": written, "type": type_}

    @_synchronized
    def unrelate(self, pairs: list[list[str]], type_: str) -> dict:
        """Erase the relationships between these pairs of this type --
        the matrix's second click, and clearing a rel cell. By pair
        rather than by relationship id so the caller can act on what a
        cell shows it (two motes and a type) without also tracking the
        id of an edge it never named."""
        wanted = set(self._valid(pairs))
        removed = [
            r for r in relationships_repo.list_relationships(self._conn)
            if r.type == type_ and (r.from_id, r.to_id) in wanted
            and relationships_repo.delete_relationship(self._conn, r.id)
        ]
        return {"removed": len(removed), "type": type_}

    def _pairs_of_type(self, type_: str) -> set[tuple[str, str]]:
        return {
            (r.from_id, r.to_id)
            for r in relationships_repo.list_relationships(self._conn, type_=type_)
        }

    def _valid(self, pairs) -> list[tuple[str, str]]:
        """Pairs of two different, still-existing motes. A mote related
        to itself is not a link anyone drew on purpose -- the matrix's
        diagonal is not clickable -- and a pair naming a deleted mote is
        a stale selection, not an error worth losing the batch over."""
        out = []
        for pair in pairs or []:
            if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                continue
            from_id, to_id = pair
            if from_id == to_id or not isinstance(from_id, str) or not isinstance(to_id, str):
                continue
            if motes_repo.get_mote(self._conn, from_id) and motes_repo.get_mote(self._conn, to_id):
                out.append((from_id, to_id))
        return out

    # ---- saved views ----

    def _views(self, motes: list[Mote], index: Index) -> list[dict]:
        """Every mote carrying a `query`. Asked of the property, not of
        core/kinds.py, for the same reason mora/api.py::list_notes asks
        of `modoc`: carrying a saved view is data, like a url or a date,
        and making it a kind would displace a view mote's real species in
        every other window's type label."""
        views = [payload_repo.view_payload(m, index) for m in motes
                 if query_repo.has_query(m.properties)]
        return sorted(views, key=lambda v: v["name"].casefold())

    def _view_species_id(self) -> str:
        """Finds or creates the `Saved View` species, same lazy pattern as
        momentum/api.py::_session_species_id -- but creating rather than
        answering None, since save_view always needs one to point at."""
        for mote in species_repo.list_species(self._conn):
            name = mote.properties.get("name")
            if isinstance(name, str) and name.casefold() == VIEW_SPECIES_NAME.casefold():
                return mote.id
        return motes_repo.create_mote(self._conn, {"name": VIEW_SPECIES_NAME})

    @_synchronized
    def save_view(self, name: str, raw_query: dict, view_id: str | None = None) -> dict:
        """A view is an ordinary mote with a `query` property, so it
        syncs, shows up in Monarch's graph, and can be linked from a Mora
        note like anything else. Re-parsed through Query rather than
        stored as handed in: what is written is what this window can read
        back.

        Every saved view is declared an instance of `Saved View`, the same
        way momentum/api.py::log_session declares a logged session an
        instance of `Work Session` -- so a garden's saved views show up as
        one kind of thing in Monarch and Monastery without either window
        knowing this one exists."""
        parsed = Query.from_json(raw_query).to_json()
        properties = {"name": name or "Untitled view", query_repo.QUERY_KEY: parsed}
        if view_id and motes_repo.get_mote(self._conn, view_id) is not None:
            motes_repo.update_mote(self._conn, view_id, properties)
            return {"id": view_id, "created": False}
        new_id = motes_repo.create_mote(self._conn, properties)
        species_repo.set_species(self._conn, new_id, self._view_species_id())
        return {"id": new_id, "created": True}

    @_synchronized
    def delete_view(self, view_id: str) -> dict:
        """Deletes the view mote outright. Unlike deleting rows this
        needs no confirmation flag: the frontend asks, and what is lost
        is a saved query rather than any of the garden's own data."""
        return {"deleted": bool(motes_repo.delete_mote(self._conn, view_id))}

# core/views.py
"""Saved views: a query kept under a name.

A view is **an ordinary mote carrying a `query` property**, the way a note
is an ordinary mote carrying `modoc`. That is the whole storage design, and
it is what nobody had to build: a view syncs between devices, shows up in
the graph window, can be linked from a note, and is editable from the CLI
and from MCP, because it is a mote and those all already work on motes.

Carrying a `query` is deliberately **not** a `core/kinds.py` kind, for the
same reason carrying a `modoc` isn't: every entry in that table is a job a
mote does in the graph's machinery, and a view mote that read as a kind
would displace its real species in every surface's type label.

Every saved view *is* declared an instance of a `Saved View` species,
though -- an ordinary mote, found or created by name rather than reserved,
so a garden that has never saved a view never grows one. That is what makes
a garden's views show up as one kind of thing in the graph and the calendar
without either of them knowing this module exists.

This lived in `monolith/api.py`. It is in `core/` because a saved view is
not one window's state: `docs/architecture.md`'s charter says every
capability lives in core and an adapter holding logic of its own is a bug,
and this one was already being read by a second surface
(Monarch's graph filter, since gone) through the property directly.
"""
import sqlite3

from core import graph as graph_repo
from core import motes as motes_repo
from core import query as query_repo
from core import species as species_repo
from core import undo
from core.entities import Mote
from core.query import Query

# The species every saved view is an instance of, found or created by name.
VIEW_SPECIES_NAME = "Saved View"

UNTITLED = "Untitled view"


def list_views(conn: sqlite3.Connection) -> list[Mote]:
    """Every mote carrying a `query`, by name.

    Asked of the property rather than of `core/kinds.py` -- see the module
    docstring. Sorted here rather than by each caller, so a rail and a
    completion list offer the same order."""
    views = [m for m in motes_repo.list_motes(conn) if query_repo.has_query(m.properties)]
    return sorted(views, key=lambda m: name_of(m).casefold())


def name_of(mote: Mote) -> str:
    name = mote.properties.get(query_repo.NAME_KEY)
    return name if isinstance(name, str) and name else mote.id[:8]


def query_of(mote: Mote) -> Query:
    """A view's query, read through `Query.from_json` -- forgiving on
    purpose, since a view's property is editable from every surface and a
    hand-edited one should come back narrower rather than raise."""
    raw = mote.properties.get(query_repo.QUERY_KEY)
    return Query.from_json(raw if isinstance(raw, dict) else None)


def find_view_by_name(conn: sqlite3.Connection, name: str) -> Mote | None:
    """The view a person named, matched case-insensitively on the whole
    name -- the same "pick one thing you can already see" rule
    core/command_bar.py's name lookup uses. The first match wins where two
    views share a name, because a `--view` that refused to open would be
    worse than one that opened the wrong one of two things you named
    identically."""
    needle = (name or "").strip().casefold()
    if not needle:
        return None
    return next((v for v in list_views(conn) if name_of(v).casefold() == needle), None)


def save_view(
    conn: sqlite3.Connection, name: str, the_query: Query, view_id: str | None = None
) -> tuple[dict, undo.Command]:
    """Create a view, or update the one `view_id` names.

    The query is re-parsed through `Query` rather than stored as handed
    in, so what is written is exactly what can be read back -- a view
    saved from a surface that sent a stray key does not come back with
    one."""
    properties = {
        query_repo.NAME_KEY: name or UNTITLED,
        query_repo.QUERY_KEY: Query.from_json(the_query.to_json()).to_json(),
    }
    existing = motes_repo.get_mote(conn, view_id) if view_id else None
    if existing is not None:
        before = {k: existing.properties[k] for k in properties if k in existing.properties}
        motes_repo.update_mote(conn, view_id, properties)
        return (
            {"id": view_id, "created": False},
            undo.for_update_mote(conn, view_id, before, properties, label="save view"),
        )

    new_id = motes_repo.create_mote(conn, properties)
    species_repo.set_species(conn, new_id, _view_species_id(conn))
    # Deleting the mote (this command's undo) already cascades away the
    # instance_of relationship set_species just made, so one
    # for_create_mote is the whole undo step.
    return (
        {"id": new_id, "created": True},
        undo.for_create_mote(conn, new_id, properties, label="save view"),
    )


def delete_view(conn: sqlite3.Connection, view_id: str) -> tuple[bool, undo.Command | None]:
    """Deletes the view mote outright. Needs no confirmation flag, unlike
    deleting rows: what is lost is a saved query rather than any of the
    garden's own data, and it undoes like anything else."""
    mote = motes_repo.get_mote(conn, view_id)
    if mote is None:
        return False, None
    relationships = [
        {"id": r.id, "fromId": r.from_id, "toId": r.to_id, "type": r.type}
        for r in graph_repo.relationships_touching(conn, view_id)
    ]
    if not motes_repo.delete_mote(conn, view_id):
        return False, None
    return True, undo.for_delete_mote(
        conn, view_id, mote.properties, relationships, label="delete view",
    )


def _view_species_id(conn: sqlite3.Connection) -> str:
    """Finds or creates the `Saved View` species, lazily -- creating rather
    than answering None, since `save_view` always needs one to point at."""
    for mote in species_repo.list_species(conn):
        name = mote.properties.get(query_repo.NAME_KEY)
        if isinstance(name, str) and name.casefold() == VIEW_SPECIES_NAME.casefold():
            return mote.id
    return motes_repo.create_mote(conn, {query_repo.NAME_KEY: VIEW_SPECIES_NAME})

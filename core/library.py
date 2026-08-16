# core/library.py
"""A reusable-motes "library" -- a reserved garden, `_library`, that
holds standalone copies of motes worth reusing across gardens (a person,
a recurring meeting shape, a standard task). Like core/portals.py, this
is a deliberate, explained exception to "core functions take one
already-open connection": saving/reusing a library entry always means
touching a second garden's connection.

Unlike a portal, a library entry is never a live reference -- saving
copies a mote's properties into `_library`, and reusing copies them back
out into a new, fully independent mote. Nothing ties the two together
afterward, the same way core/species.py's create_instance() seeds a new
instance from a template's properties without keeping them linked.

Both directions are core/motes.py::copy_mote(), which is the whole of
what this module does that motes.py couldn't: save is a copy into the
library garden, reuse is a copy back out. What is left here is the two
things that genuinely belong to the library rather than to copying --
which garden it is, and lazily bringing it into being.

Relationships are never saved or restored -- a library entry is
properties only. Saving whole subgraphs (a mote plus its relationships
to other library entries) is a deferred, larger feature -- see
docs/architecture.md's "Reusable motes" section for what it would have
to answer first.

`_library` is not validated to exist before use, unlike create_portal()'s
arbitrary user-named target garden: it's a fixed reserved name that
should come into being lazily on first save, the same way
core/gardens.py::resolve_garden(None) lazily creates DEFAULT_GARDEN.
That lazy path (get_connection + initialize_database) is also the only
way it can be created at all -- core/db.py::is_reserved_garden() marks
any underscore-prefixed name as MonOSS's own, which
core/gardens.py::create_garden() refuses to make and list_gardens()
leaves out of the user's workspaces.
"""
import sqlite3

from core.db import get_connection, initialize_database
from core.motes import copy_mote

LIBRARY_GARDEN = "_library"


def _library_connection() -> sqlite3.Connection:
    conn = get_connection(LIBRARY_GARDEN)
    initialize_database(conn)
    return conn


def save_to_library(conn: sqlite3.Connection, mote_id: str) -> str:
    """Snapshot mote_id's properties (from conn's garden) into a new,
    independent entry in the library. Returns the new entry's id."""
    library_conn = _library_connection()
    try:
        return copy_mote(conn, library_conn, mote_id)
    finally:
        library_conn.close()


def reuse_from_library(
    conn: sqlite3.Connection, library_mote_id: str, overrides: dict | None = None
) -> str:
    """Create a new mote in conn's garden, seeded from a library entry's
    properties -- exactly like core/species.py::create_instance() seeds
    a new instance from its species' template. Returns the new mote's
    id; the library entry itself is untouched and nothing links back to
    it afterward."""
    library_conn = _library_connection()
    try:
        return copy_mote(library_conn, conn, library_mote_id, overrides)
    except LookupError:
        raise LookupError(f"No such library entry: {library_mote_id!r}") from None
    finally:
        library_conn.close()

# core/portals.py
"""Portal motes -- an ordinary mote that references a mote living in a
*different* garden, so the graph can be navigated from garden A into
garden B at a specific mote while the two gardens otherwise stay
completely separate (docs/architecture.md). Like core/model_resolver.py,
this is a deliberate, explained exception to "core functions take one
already-open connection" -- everywhere else in core/ assumes a single
connection.

A portal is stored as two flat properties, not a relationship: the
`relationships` table's from_id/to_id both carry a `FOREIGN KEY ...
REFERENCES motes(id)` (core/db.py), so a relationship's target must exist
in the *same* connection's motes table. A portal's target lives in a
different garden file entirely, so it has to be plain data instead --
which also means it round-trips through core/garden_io.py's export/
import untouched, with no ID remapping to worry about.

Portals are navigation-only: nothing in core/simulation.py or
core/formula.py resolves one. There is no existing cross-garden mechanism
anywhere in the solver, and extending it to reach through a portal would
be a separate, substantially larger feature.
"""
import sqlite3

from core import gardens as gardens_repo
from core import motes as motes_repo
from core.db import get_connection, initialize_database
from core.entities import Mote

PORTAL_GARDEN_KEY = "portal_garden"
PORTAL_MOTE_KEY = "portal_mote"


class PortalError(LookupError):
    """Base for portal target-resolution failures -- distinct from the
    plain LookupError/ValueError create_portal() and resolve_portal()
    raise for a bad call, so a caller that wants to tell "target garden
    is gone" from "target mote is gone" apart can catch these
    specifically."""


class PortalGardenMissing(PortalError):
    """The portal's target garden no longer exists."""


class PortalMoteMissing(PortalError):
    """The portal's target garden exists, but the target mote is gone
    from it."""


def portal_target(properties: dict) -> tuple[str, str] | None:
    """(garden, mote_id) if `properties` describes a portal, else None.
    Pure and DB-free, like core/formula.py's Context -- testable with
    plain dicts, and safe to call from the frontend's detection logic
    without a round trip."""
    garden = properties.get(PORTAL_GARDEN_KEY)
    mote_id = properties.get(PORTAL_MOTE_KEY)
    if not isinstance(garden, str) or not garden.strip():
        return None
    if not isinstance(mote_id, str) or not mote_id.strip():
        return None
    return (garden, mote_id)


def create_portal(
    conn: sqlite3.Connection,
    target_garden: str,
    target_mote_id: str,
    extra_properties: dict | None = None,
) -> str:
    """Create a portal mote in `conn`'s garden pointing at
    target_mote_id in target_garden. Validates both eagerly, before
    creating anything -- the target garden must exist, and the target
    mote must exist in it, checked via a short-lived second connection
    (the pattern ui/api.py::export_garden already uses for reading a
    named garden without disturbing the caller's own connection).
    Analogous to core/species.py's create_instance() being a validating
    convenience wrapper over create_mote()."""
    if not gardens_repo.garden_exists(target_garden):
        raise LookupError(f"No such garden: {target_garden!r}")

    target_conn = get_connection(target_garden)
    try:
        initialize_database(target_conn)
        if motes_repo.get_mote(target_conn, target_mote_id) is None:
            raise LookupError(
                f"No such mote {target_mote_id[:8]} in garden {target_garden!r}"
            )
    finally:
        target_conn.close()

    properties = dict(extra_properties or {})
    properties[PORTAL_GARDEN_KEY] = target_garden
    properties[PORTAL_MOTE_KEY] = target_mote_id
    return motes_repo.create_mote(conn, properties)


def resolve_portal(properties: dict) -> tuple[str, Mote]:
    """Given a portal mote's properties, open its target garden and
    fetch the live target mote. Raises PortalGardenMissing or
    PortalMoteMissing rather than a generic error -- both are realistic
    failure modes since gardens and motes can be deleted independently
    of any portal pointing at them, and the two need different messages
    (delete the garden vs. delete the mote inside it)."""
    target = portal_target(properties)
    if target is None:
        raise ValueError("Not a portal mote (missing portal_garden/portal_mote)")
    target_garden, target_mote_id = target

    if not gardens_repo.garden_exists(target_garden):
        raise PortalGardenMissing(
            f"Portal's target garden no longer exists: {target_garden!r}"
        )

    conn = get_connection(target_garden)
    try:
        initialize_database(conn)
        mote = motes_repo.get_mote(conn, target_mote_id)
    finally:
        conn.close()

    if mote is None:
        raise PortalMoteMissing(
            f"Portal's target mote {target_mote_id[:8]} no longer exists "
            f"in garden {target_garden!r}"
        )
    return target_garden, mote

# core/relationships.py
import sqlite3

from core import oplog, stamps
from core.db import generate_uuid
from core.entities import Relationship
from core.stamps import Stamp

# Relationships programmatically modifying mote properties is the
# simulation capability, and it is built: core/rules.py discovers rules
# from `applies_to` relationships, and core/simulation.py solves them.
# Nothing about that lives here -- this module stays plain storage.


def _stamp(*floors: Stamp | None) -> Stamp:
    """The replayed op's stamp, or a fresh local one later than `floors`
    (core/stamps.py)."""
    return oplog.replay_stamp() or stamps.next_local(*floors)


def create_relationship(
    conn: sqlite3.Connection, from_id: str, to_id: str, type_: str, id: str | None = None
) -> str:
    cur = conn.cursor()
    rel_id = id or generate_uuid()
    # An id given back (undo of a delete) must beat its own delete, and
    # one between motes brought back must beat theirs.
    stamp = _stamp(
        stamps.death(conn, "relationship", rel_id),
        stamps.death(conn, "mote", from_id),
        stamps.death(conn, "mote", to_id),
    )
    cur.execute(
        "INSERT INTO relationships (id, from_id, to_id, type, updated_at, updated_by) VALUES (?, ?, ?, ?, ?, ?)",
        (rel_id, from_id, to_id, type_, stamp.ts, stamp.device)
    )
    oplog.append(
        conn, "relationship", rel_id, "create_relationship",
        {"id": rel_id, "from_id": from_id, "to_id": to_id, "type": type_}, stamp,
    )
    conn.commit()
    return rel_id


def _row_to_relationship(row: sqlite3.Row) -> Relationship:
    return Relationship(
        id=row["id"], from_id=row["from_id"], to_id=row["to_id"], type=row["type"]
    )


def get_relationship(conn: sqlite3.Connection, rel_id: str) -> Relationship | None:
    cur = conn.cursor()
    cur.execute("SELECT * FROM relationships WHERE id=?", (rel_id,))
    row = cur.fetchone()
    return _row_to_relationship(row) if row is not None else None


def list_relationships(
    conn: sqlite3.Connection, from_id: str | None = None, type_: str | None = None
) -> list[Relationship]:
    cur = conn.cursor()
    query = "SELECT * FROM relationships WHERE 1=1"
    params = []
    if from_id is not None:
        query += " AND from_id=?"
        params.append(from_id)
    if type_ is not None:
        query += " AND type=?"
        params.append(type_)
    cur.execute(query, params)
    return [_row_to_relationship(row) for row in cur.fetchall()]


def update_relationship(
    conn: sqlite3.Connection,
    rel_id: str,
    from_id: str | None = None,
    to_id: str | None = None,
    type_: str | None = None,
) -> bool:
    fields = {"from_id": from_id, "to_id": to_id, "type": type_}
    fields = {k: v for k, v in fields.items() if v is not None}
    if not fields:
        raise ValueError("update_relationship requires at least one field to update")

    current = stamps.relationship_stamp(conn, rel_id)
    if current is None:
        return False
    stamp = _stamp(current, *(stamps.death(conn, "mote", fields[end]) for end in ("from_id", "to_id") if end in fields))
    set_clause = ", ".join(f"{col} = ?" for col in fields) + ", updated_at = ?, updated_by = ?"
    conn.execute(
        f"UPDATE relationships SET {set_clause} WHERE id=?",
        (*fields.values(), stamp.ts, stamp.device, rel_id)
    )
    # The whole relationship, not just what changed: replay treats a
    # relationship as one value (core/merge.py), and two partial updates
    # racing would otherwise merge differently on each device.
    row = conn.execute("SELECT from_id, to_id, type FROM relationships WHERE id=?", (rel_id,)).fetchone()
    oplog.append(conn, "relationship", rel_id, "update_relationship", {"id": rel_id, **dict(row)}, stamp)
    conn.commit()
    return True


def delete_relationship(conn: sqlite3.Connection, rel_id: str) -> bool:
    current = stamps.relationship_stamp(conn, rel_id)
    if current is None:
        return False
    stamp = _stamp(current)
    conn.execute("DELETE FROM relationships WHERE id=?", (rel_id,))
    stamps.bury(conn, "relationship", rel_id, stamp)
    oplog.append(conn, "relationship", rel_id, "delete_relationship", {"id": rel_id}, stamp)
    conn.commit()
    return True

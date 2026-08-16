# core/relationships.py
import sqlite3

from core.db import generate_uuid
from core.entities import Relationship

# Relationships programmatically modifying mote properties is the
# simulation capability, and it is built: core/rules.py discovers rules
# from `applies_to` relationships, and core/simulation.py solves them.
# Nothing about that lives here -- this module stays plain storage.


def create_relationship(
    conn: sqlite3.Connection, from_id: str, to_id: str, type_: str, id: str | None = None
) -> str:
    cur = conn.cursor()
    rel_id = id or generate_uuid()
    cur.execute(
        "INSERT INTO relationships (id, from_id, to_id, type) VALUES (?, ?, ?, ?)",
        (rel_id, from_id, to_id, type_)
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

    set_clause = ", ".join(f"{col} = ?" for col in fields)
    cur = conn.cursor()
    cur.execute(
        f"UPDATE relationships SET {set_clause} WHERE id=?",
        (*fields.values(), rel_id)
    )
    conn.commit()
    return cur.rowcount > 0


def delete_relationship(conn: sqlite3.Connection, rel_id: str) -> bool:
    cur = conn.cursor()
    cur.execute("DELETE FROM relationships WHERE id=?", (rel_id,))
    conn.commit()
    return cur.rowcount > 0

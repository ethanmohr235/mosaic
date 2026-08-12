# core/motes.py
import json
import sqlite3

from core.db import generate_uuid
from core.models import Mote


def _encode_value(value):
    try:
        return json.dumps(value)
    except TypeError as e:
        raise ValueError(f"Property value is not JSON-serializable: {value!r}") from e


def _decode_value(row: sqlite3.Row):
    try:
        return json.loads(row["value"])
    except json.JSONDecodeError as e:
        raise ValueError(
            f"Corrupt property value for mote {row['mote_id']!r} key {row['key']!r}"
        ) from e


def _properties_by_mote(conn: sqlite3.Connection, mote_ids: list[str] | None = None) -> dict[str, dict]:
    """Every relevant mote's properties in one query, grouped by mote_id --
    mote_ids=None means the whole table (used by list_motes(), where an
    IN-list of every id risks SQLite's bound-parameter limit for a large
    garden)."""
    cur = conn.cursor()
    if mote_ids is None:
        cur.execute("SELECT mote_id, key, value FROM mote_properties")
    else:
        placeholders = ",".join("?" * len(mote_ids))
        cur.execute(
            f"SELECT mote_id, key, value FROM mote_properties WHERE mote_id IN ({placeholders})",
            mote_ids,
        )
    grouped: dict[str, dict] = {}
    for row in cur.fetchall():
        grouped.setdefault(row["mote_id"], {})[row["key"]] = _decode_value(row)
    return grouped


def _load_properties(conn: sqlite3.Connection, mote_id: str) -> dict:
    return _properties_by_mote(conn, [mote_id]).get(mote_id, {})


def create_mote(conn: sqlite3.Connection, properties: dict | None = None, id: str | None = None) -> str:
    properties = properties or {}
    cur = conn.cursor()

    mote_id = id or generate_uuid()
    cur.execute("INSERT INTO motes (id) VALUES (?)", (mote_id,))

    for key, value in properties.items():
        cur.execute(
            "INSERT INTO mote_properties (mote_id, key, value) VALUES (?, ?, ?)",
            (mote_id, key, _encode_value(value))
        )

    conn.commit()
    return mote_id


def get_mote(conn: sqlite3.Connection, mote_id: str) -> Mote | None:
    cur = conn.cursor()
    cur.execute("SELECT id FROM motes WHERE id=?", (mote_id,))
    row = cur.fetchone()
    if row is None:
        return None
    return Mote(id=row["id"], properties=_load_properties(conn, mote_id))


def list_motes(conn: sqlite3.Connection) -> list[Mote]:
    cur = conn.cursor()
    cur.execute("SELECT id FROM motes")
    ids = [row["id"] for row in cur.fetchall()]
    props = _properties_by_mote(conn)
    return [Mote(id=i, properties=props.get(i, {})) for i in ids]


def get_motes(conn: sqlite3.Connection, ids: list[str]) -> list[Mote]:
    """Batch equivalent of get_mote() -- two queries total instead of N.
    Preserves the order of `ids`; ids with no matching mote are skipped."""
    if not ids:
        return []
    placeholders = ",".join("?" * len(ids))
    cur = conn.cursor()
    cur.execute(f"SELECT id FROM motes WHERE id IN ({placeholders})", ids)
    existing = {row["id"] for row in cur.fetchall()}
    ordered = [i for i in ids if i in existing]
    props = _properties_by_mote(conn, ordered)
    return [Mote(id=i, properties=props.get(i, {})) for i in ordered]


def delete_mote(conn: sqlite3.Connection, id: str) -> bool:
    cur = conn.cursor()
    cur.execute("DELETE FROM motes WHERE id=?", (id,))
    conn.commit()
    return cur.rowcount > 0


def update_mote(conn: sqlite3.Connection, mote_id: str, new_props: dict) -> None:
    cur = conn.cursor()

    cur.executemany("""
        INSERT INTO mote_properties (mote_id, key, value)
        VALUES (?, ?, ?)
        ON CONFLICT(mote_id, key)
        DO UPDATE SET value = excluded.value
    """, [(mote_id, k, _encode_value(v)) for k, v in new_props.items()])

    conn.commit()


def delete_property(conn: sqlite3.Connection, mote_id: str, key: str) -> bool:
    cur = conn.cursor()
    cur.execute(
        "DELETE FROM mote_properties WHERE mote_id=? AND key=?", (mote_id, key)
    )
    conn.commit()
    return cur.rowcount > 0

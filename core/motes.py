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


def _load_properties(conn: sqlite3.Connection, mote_id: str) -> dict:
    cur = conn.cursor()
    cur.execute("SELECT key, value FROM mote_properties WHERE mote_id=?", (mote_id,))
    props = {}
    for row in cur.fetchall():
        try:
            props[row["key"]] = json.loads(row["value"])
        except json.JSONDecodeError as e:
            raise ValueError(
                f"Corrupt property value for mote {mote_id!r} key {row['key']!r}"
            ) from e
    return props


def create_mote(conn: sqlite3.Connection, properties: dict | None = None) -> str:
    properties = properties or {}
    cur = conn.cursor()

    mote_id = generate_uuid()
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
    return [
        Mote(id=row["id"], properties=_load_properties(conn, row["id"]))
        for row in cur.fetchall()
    ]


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

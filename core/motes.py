# core/motes.py
import json
import re
import sqlite3

from core.db import generate_uuid
from core.models import Mote

# Matches "key = value" (or "key=value"), e.g. "priority = high" --
# structured equality on a specific property, shared with
# ui/frontend/app.js::matchesQuery so a query behaves the same whether
# typed into the graph's search bar or an LLM's MCP call.
_KEY_VALUE_QUERY_RE = re.compile(r"^\s*([^=\s][^=]*?)\s*=\s*(.*)$")


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


def search_motes(conn: sqlite3.Connection, query: str, limit: int = 60) -> list[Mote]:
    """Case-insensitive text search over every mote's id and properties.
    "key = value" matches a specific property's value exactly; anything
    else is a substring match against the mote id or any property key or
    value. An empty/blank query matches everything, same as the UI's own
    search palette does with nothing typed. Mirrors
    ui/frontend/app.js::matchesQuery exactly so search results don't
    depend on which adapter (UI, CLI, MCP) asked."""
    motes = list_motes(conn)
    query = (query or "").strip()
    if not query:
        return motes[:limit]

    kv = _KEY_VALUE_QUERY_RE.match(query)
    if kv:
        key = kv.group(1).strip().lower()
        value = kv.group(2).strip().lower()

        def matches(mote: Mote) -> bool:
            for k, v in mote.properties.items():
                if k.lower() != key:
                    continue
                if not value:
                    return True
                vs = v if isinstance(v, str) else json.dumps(v)
                if vs.lower() == value:
                    return True
            return False
    else:
        q = query.lower()

        def matches(mote: Mote) -> bool:
            if q in mote.id.lower():
                return True
            for k, v in mote.properties.items():
                if q in k.lower():
                    return True
                vs = v if isinstance(v, str) else json.dumps(v)
                if q in vs.lower():
                    return True
            return False

    return [m for m in motes if matches(m)][:limit]


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

# core/motes.py
import json
import re
import sqlite3

from core import oplog, stamps
from core.db import generate_uuid
from core.entities import Mote
from core.stamps import Stamp

# Matches "key = value" (or "key=value"), e.g. "priority = high" --
# structured equality on a specific property, for an LLM's MCP call
# (`search_motes`) and the CLI.
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


def _stamp(*floors: Stamp | None) -> Stamp:
    """The replayed op's stamp, or a fresh local one later than `floors`
    (core/stamps.py)."""
    return oplog.replay_stamp() or stamps.next_local(*floors)


def create_mote(conn: sqlite3.Connection, properties: dict | None = None, id: str | None = None) -> str:
    properties = properties or {}
    cur = conn.cursor()

    mote_id = id or generate_uuid()
    # An id given back (undo of a delete) must beat its own delete.
    stamp = _stamp(stamps.death(conn, "mote", mote_id), stamps.newest_property_death(conn, mote_id))
    cur.execute(
        "INSERT INTO motes (id, created_at, created_by) VALUES (?, ?, ?)",
        (mote_id, stamp.ts, stamp.device),
    )

    for key, value in properties.items():
        cur.execute(
            "INSERT INTO mote_properties (mote_id, key, value, updated_at, updated_by) VALUES (?, ?, ?, ?, ?)",
            (mote_id, key, _encode_value(value), stamp.ts, stamp.device)
        )

    oplog.append(conn, "mote", mote_id, "create_mote", {"id": mote_id, "properties": properties}, stamp)
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
    value. An empty/blank query matches everything. (The console searches
    with `find`, core/findtext.py.)"""
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


def copy_mote(
    source: sqlite3.Connection,
    target: sqlite3.Connection,
    mote_id: str,
    overrides: dict | None = None,
) -> str:
    """Copy a mote's properties into a new, independent mote -- possibly
    in a different garden, since the two connections need not be the
    same one. Returns the new mote's id. Raises LookupError if there is
    no such mote in `source`.

    Properties only: relationships are not copied, because there is no
    general answer to what a relationship pointing outside the copied set
    should become. This is the same seeding core/species.py's
    create_instance() does from a template.

    Nothing links the copy to the original afterward."""
    mote = get_mote(source, mote_id)
    if mote is None:
        raise LookupError(f"No such mote: {mote_id!r}")

    properties = dict(mote.properties)
    properties.update(overrides or {})
    return create_mote(target, properties)


def delete_mote(conn: sqlite3.Connection, id: str) -> bool:
    """The delete cascades to the mote's properties and relationships. Its
    tombstone, stamped after all of them, keeps them from coming back
    from a device that hadn't seen this yet; each relationship it takes
    also keeps the stamp of its own last write (core/merge.py::_retire),
    so an older write to it, moving it elsewhere, still loses."""
    cur = conn.cursor()
    if cur.execute("SELECT 1 FROM motes WHERE id=?", (id,)).fetchone() is None:
        return False
    stamp = _stamp(stamps.newest_child(conn, id))
    for row in cur.execute(
        "SELECT id, updated_at, updated_by FROM relationships WHERE from_id=? OR to_id=?", (id, id)
    ).fetchall():
        stamps.bury(conn, "relationship", row["id"], Stamp.of(row["updated_at"], row["updated_by"]))
    stamps.bury(conn, "mote", id, stamp)
    cur.execute("DELETE FROM motes WHERE id=?", (id,))
    oplog.append(conn, "mote", id, "delete_mote", {"id": id}, stamp)
    conn.commit()
    return True


def update_mote(conn: sqlite3.Connection, mote_id: str, new_props: dict) -> None:
    """One oplog row per changed key, not one for the whole call --
    concurrent edits to *different* properties on the same mote from two
    devices then merge with no conflict at all (core/merge.py)."""
    cur = conn.cursor()

    for key, value in new_props.items():
        stamp = _stamp(
            stamps.property_stamp(conn, mote_id, key),
            stamps.death(conn, "property", mote_id, key),
            stamps.death(conn, "mote", mote_id),
        )
        cur.execute("""
            INSERT INTO mote_properties (mote_id, key, value, updated_at, updated_by)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(mote_id, key)
            DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at,
                          updated_by = excluded.updated_by
        """, (mote_id, key, _encode_value(value), stamp.ts, stamp.device))
        oplog.append(
            conn, "mote", mote_id, "set_property",
            {"mote_id": mote_id, "key": key, "value": value}, stamp,
        )

    conn.commit()


def delete_property(conn: sqlite3.Connection, mote_id: str, key: str) -> bool:
    cur = conn.cursor()
    current = stamps.property_stamp(conn, mote_id, key)
    if current is None:
        return False
    stamp = _stamp(current)
    cur.execute("DELETE FROM mote_properties WHERE mote_id=? AND key=?", (mote_id, key))
    stamps.bury(conn, "property", mote_id, stamp, key)
    oplog.append(conn, "mote", mote_id, "delete_property", {"mote_id": mote_id, "key": key}, stamp)
    conn.commit()
    return True
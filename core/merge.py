# core/merge.py
"""Replaying another device's ops so that every device which has seen the
same ops holds the same garden, in whatever order they arrived.

Each op carries its stamp (core/stamps.py), and the garden is a function
of the stamps alone:

- **A property** holds whichever set or delete of it has the latest stamp.
- **A mote or relationship is alive** while its latest create is later
  than its latest delete. A create after a delete -- undo bringing it
  back -- revives it.
- **A relationship is one value** -- its ends and its type. Every create
  and update writes all of it (core/relationships.py), the latest wins,
  and a relationship is alive while that latest write is later than its
  latest delete.
- **A property or relationship needs its motes alive,** and counts only
  if it is later than the latest delete of its mote (a relationship: of
  both motes it joins). Older ones went with the delete, for good. A
  newer one that arrives while its mote is deleted waits in `held_ops`,
  invisible, so a revive brings it back on every device alike; so does
  one whose mote simply hasn't arrived yet.

The replay itself goes through the ordinary primitives in core/motes.py
and core/relationships.py, inside oplog.replaying(stamp), so it writes
the op's stamp and logs nothing. Only the bookkeeping a local write never
needs -- pruning what a delete outdates, holding what it hides -- is SQL
here.

apply() returns None when an op landed, or a note: "shadowed" (something
newer already holds), "delete_won" (it is older than a delete), "held"
(waiting, see above).
"""
import json
import sqlite3
from typing import Any, Callable

from core import motes as motes_repo
from core import oplog, stamps
from core import relationships as rel_repo
from core.stamps import Stamp

Op = dict[str, Any]
Note = dict[str, Any] | None


def apply(conn: sqlite3.Connection, op: Op) -> Note:
    """Replay one remote op and commit."""
    handler = _HANDLERS.get(op["action"])
    if handler is None:
        raise ValueError(f"Unknown oplog action: {op['action']!r}")
    stamp = Stamp.of(op["ts"], op["device_id"])
    try:
        note = handler(conn, op, op["payload"], stamp)
    except sqlite3.IntegrityError as e:
        # Not expected: every rule above is checked first. Holding the op
        # keeps it, and keeps it out of the way of every op behind it.
        conn.rollback()
        note = _hold(conn, op["entity_id"], op) | {"reason": "error", "detail": str(e)}
    conn.commit()
    return note


def retry_held(conn: sqlite3.Connection) -> int:
    """Re-apply every held op whose mote or relationship is here now.
    Returns how many are still held."""
    waiting = [r["waiting_on"] for r in conn.execute("SELECT DISTINCT waiting_on FROM held_ops")]
    for entity_id in waiting:
        if stamps.mote_life(conn, entity_id) or stamps.relationship_stamp(conn, entity_id):
            _release(conn, entity_id)
    conn.commit()
    return held_count(conn)


def held_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) AS n FROM held_ops").fetchone()["n"]


def _note(reason: str, entity: str) -> Note:
    return {"reason": reason, "entity": entity}


def _hold(conn: sqlite3.Connection, waiting_on: str, op: Op) -> dict[str, Any]:
    kept = {k: op[k] for k in ("device_id", "ts", "entity_type", "entity_id", "action", "payload")}
    conn.execute("INSERT INTO held_ops (waiting_on, op) VALUES (?, ?)", (waiting_on, json.dumps(kept)))
    return {"reason": "held", "entity": waiting_on}


def _release(conn: sqlite3.Connection, entity_id: str) -> None:
    rows = conn.execute("SELECT id, op FROM held_ops WHERE waiting_on = ?", (entity_id,)).fetchall()
    conn.execute("DELETE FROM held_ops WHERE waiting_on = ?", (entity_id,))
    for row in rows:
        apply(conn, json.loads(row["op"]))


def _synthetic(action: str, entity_type: str, entity_id: str, payload: dict, stamp: Stamp) -> Op:
    return {"device_id": stamp.device, "ts": stamp.ts, "entity_type": entity_type,
            "entity_id": entity_id, "action": action, "payload": payload}


def _older_than(stamp: Stamp, *deaths: Stamp | None) -> bool:
    return any(d is not None and stamp < d for d in deaths)


# ---- motes and properties ----

def _create_mote(conn: sqlite3.Connection, op: Op, payload: dict, stamp: Stamp) -> Note:
    mote_id = payload["id"]
    if _older_than(stamp, stamps.death(conn, "mote", mote_id)):
        return _note("delete_won", mote_id)
    life = stamps.mote_life(conn, mote_id)
    if life is None:
        props = {k: v for k, v in payload["properties"].items()
                 if not _older_than(stamp, stamps.death(conn, "property", mote_id, k))}
        with oplog.replaying(stamp):
            motes_repo.create_mote(conn, props, id=mote_id)
        _release(conn, mote_id)
        return None
    if stamp > life:
        conn.execute("UPDATE motes SET created_at = ?, created_by = ? WHERE id = ?",
                     (stamp.ts, stamp.device, mote_id))
    for key, value in payload["properties"].items():
        _set(conn, mote_id, key, value, stamp)
    return None


def _child_gate(conn: sqlite3.Connection, op: Op, mote_id: str, stamp: Stamp) -> Note:
    """None when `mote_id` is here and `stamp` is after its latest delete."""
    if _older_than(stamp, stamps.death(conn, "mote", mote_id)):
        return _note("delete_won", mote_id)
    if stamps.mote_life(conn, mote_id) is None:
        return _hold(conn, mote_id, op)
    return None


def _set(conn: sqlite3.Connection, mote_id: str, key: str, value: Any, stamp: Stamp) -> Note:
    current = stamps.property_stamp(conn, mote_id, key)
    if _older_than(stamp, current, stamps.death(conn, "property", mote_id, key)) or stamp == current:
        return _note("shadowed", f"{mote_id}.{key}")
    with oplog.replaying(stamp):
        motes_repo.update_mote(conn, mote_id, {key: value})
    return None


def _set_property(conn: sqlite3.Connection, op: Op, payload: dict, stamp: Stamp) -> Note:
    mote_id = payload["mote_id"]
    return _child_gate(conn, op, mote_id, stamp) or _set(conn, mote_id, payload["key"], payload["value"], stamp)


def _delete_property(conn: sqlite3.Connection, op: Op, payload: dict, stamp: Stamp) -> Note:
    mote_id, key = payload["mote_id"], payload["key"]
    stamps.bury(conn, "property", mote_id, stamp, key)
    current = stamps.property_stamp(conn, mote_id, key)
    if current is None:
        return None
    if current > stamp:
        return _note("shadowed", f"{mote_id}.{key}")
    with oplog.replaying(stamp):
        motes_repo.delete_property(conn, mote_id, key)
    return None


def _delete_mote(conn: sqlite3.Connection, op: Op, payload: dict, stamp: Stamp) -> Note:
    mote_id = payload["id"]
    stamps.bury(conn, "mote", mote_id, stamp)
    life = stamps.mote_life(conn, mote_id)
    if life is None:
        return None
    _prune(conn, mote_id, stamp)
    if life > stamp:
        return None  # brought back since; only what the delete outdates goes
    _hold_survivors(conn, mote_id)
    with oplog.replaying(stamp):
        motes_repo.delete_mote(conn, mote_id)
    return None


def _prune(conn: sqlite3.Connection, mote_id: str, stamp: Stamp) -> None:
    """Drop what this delete of `mote_id` outdates, for good."""
    for row in conn.execute(
        "SELECT key, updated_at, updated_by FROM mote_properties WHERE mote_id = ?", (mote_id,)
    ).fetchall():
        if Stamp.of(row["updated_at"], row["updated_by"]) < stamp:
            conn.execute("DELETE FROM mote_properties WHERE mote_id = ? AND key = ?", (mote_id, row["key"]))
    for row in _touching(conn, mote_id):
        written = Stamp.of(row["updated_at"], row["updated_by"])
        if written < stamp:
            _retire(conn, row["id"], written)


def _hold_survivors(conn: sqlite3.Connection, mote_id: str) -> None:
    """Keep, as held ops, what is newer than the delete about to hide it."""
    for row in conn.execute(
        "SELECT key, value, updated_at, updated_by FROM mote_properties WHERE mote_id = ?", (mote_id,)
    ).fetchall():
        payload = {"mote_id": mote_id, "key": row["key"], "value": json.loads(row["value"])}
        _hold(conn, mote_id, _synthetic("set_property", "mote", mote_id, payload,
                                        Stamp.of(row["updated_at"], row["updated_by"])))
    for row in _touching(conn, mote_id):
        fields = {"id": row["id"], "from_id": row["from_id"], "to_id": row["to_id"], "type": row["type"]}
        written = Stamp.of(row["updated_at"], row["updated_by"])
        _hold(conn, mote_id, _synthetic("create_relationship", "relationship", row["id"], fields, written))
        _retire(conn, row["id"], written)


def _touching(conn: sqlite3.Connection, mote_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM relationships WHERE from_id = ? OR to_id = ?", (mote_id, mote_id)
    ).fetchall()


# ---- relationships ----

def _write_relationship(conn: sqlite3.Connection, op: Op, payload: dict, stamp: Stamp) -> Note:
    """A create or an update: either way, the whole relationship."""
    rel_id = payload["id"]
    fields = _whole(conn, rel_id, payload)
    if fields is None:
        return _hold(conn, rel_id, op)  # an old partial update, before its create
    if _older_than(stamp, stamps.death(conn, "relationship", rel_id)):
        return _note("delete_won", rel_id)
    current = stamps.relationship_stamp(conn, rel_id)
    if current is not None and current >= stamp:
        return _note("shadowed", rel_id)
    ends = (fields["from_id"], fields["to_id"])
    # From here this write is the relationship's latest. If it can't be
    # seen -- it joins a deleted mote, or one not here -- neither can
    # anything older.
    if _older_than(stamp, *(stamps.death(conn, "mote", end) for end in ends)):
        _retire(conn, rel_id, stamp)
        return _note("delete_won", rel_id)
    for end in ends:
        if stamps.mote_life(conn, end) is None:
            _retire(conn, rel_id, stamp)
            return _hold(conn, end, op)
    with oplog.replaying(stamp):
        if current is None:
            rel_repo.create_relationship(conn, *ends, fields["type"], id=rel_id)
        else:
            rel_repo.update_relationship(conn, rel_id, *ends, fields["type"])
    if current is None:
        _release(conn, rel_id)
    return None


def _retire(conn: sqlite3.Connection, rel_id: str, written: Stamp) -> None:
    """Take a relationship out of sight as of its latest write `written`,
    remembering that stamp so a write older than it still loses -- the
    same as it would have on a device where it is still in sight."""
    stamps.bury(conn, "relationship", rel_id, written)
    conn.execute("DELETE FROM relationships WHERE id = ?", (rel_id,))


def _whole(conn: sqlite3.Connection, rel_id: str, payload: dict) -> dict | None:
    """The relationship this op writes, filling in from the stored one
    what an update from before whole-relationship payloads left out."""
    if all(k in payload for k in ("from_id", "to_id", "type")):
        return payload
    row = conn.execute("SELECT from_id, to_id, type FROM relationships WHERE id = ?", (rel_id,)).fetchone()
    return None if row is None else {**dict(row), **payload}


def _delete_relationship(conn: sqlite3.Connection, op: Op, payload: dict, stamp: Stamp) -> Note:
    rel_id = payload["id"]
    stamps.bury(conn, "relationship", rel_id, stamp)
    current = stamps.relationship_stamp(conn, rel_id)
    if current is None:
        return None
    if current > stamp:
        return _note("shadowed", rel_id)  # written again since
    with oplog.replaying(stamp):
        rel_repo.delete_relationship(conn, rel_id)
    return None


_HANDLERS: dict[str, Callable[[sqlite3.Connection, Op, dict, Stamp], Note]] = {
    "create_mote": _create_mote,
    "set_property": _set_property,
    "delete_property": _delete_property,
    "delete_mote": _delete_mote,
    "create_relationship": _write_relationship,
    "update_relationship": _write_relationship,
    "delete_relationship": _delete_relationship,
}

# core/sync.py
"""One garden's push and pull, built on the oplog (core/oplog.py) rather
than the garden's .db file -- syncing a live SQLite file directly risks
corruption from two writers; syncing an *append-only log of small
immutable ops* through the sync server (sync_server/, via
core/sync_remote.py) does not, because no device ever edits an op another
device wrote. core/sync_all.py decides which gardens to run this for.

A push sends this device's oplog rows since the last push; a pull replays
every other device's, through core/merge.py. Replay is the merge's: every
op carries its stamp (core/stamps.py), and a garden is a function of the
stamps alone -- latest write wins per property, a delete beats anything
older than it, and an op that can't land yet (its mote hasn't arrived, or
is deleted and the op is newer) is held rather than refused. So any two
devices that have pulled the same ops hold the same garden, whatever
order the ops came in, and no op ever stops the ones behind it.

Progress is per garden, in its `sync_state` table: how far this device
has pushed (keyed by its own device id), how far it has replayed each
other device, and the server's pull cursor.
"""
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from core import gardens as gardens_repo
from core import merge
from core import motes as motes_repo
from core import oplog
from core import sync_remote
from core.db import get_connection, initialize_database
from core.device import get_device_id
from core.stamps import Stamp, format_ts
from core.sync_remote import Remote

# Reserved key in sync_state (never a real device_id, which is always a
# uuid4) holding how far this garden has paged through the server's ops.
_SERVER_CURSOR_KEY = "__server_cursor__"
BUSY_TIMEOUT_MS = 5000


def open_garden(name: str) -> sqlite3.Connection:
    """A connection for syncing, which waits out the console's or the
    MCP server's writes rather than failing on them."""
    conn = get_connection(name)
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    initialize_database(conn)
    return conn


def _get_state(conn: sqlite3.Connection, device_id: str) -> int:
    row = conn.execute(
        "SELECT last_applied_seq FROM sync_state WHERE remote_device_id = ?", (device_id,)
    ).fetchone()
    return row["last_applied_seq"] if row else 0


def _set_state(conn: sqlite3.Connection, device_id: str, seq: int) -> None:
    conn.execute(
        "INSERT INTO sync_state (remote_device_id, last_applied_seq) VALUES (?, ?) "
        "ON CONFLICT(remote_device_id) DO UPDATE SET last_applied_seq = excluded.last_applied_seq",
        (device_id, seq),
    )


def _all_state(conn: sqlite3.Connection) -> dict[str, int]:
    return {r["remote_device_id"]: r["last_applied_seq"] for r in conn.execute("SELECT * FROM sync_state")}


def _row_to_op(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "op_id": row["op_id"],
        "seq": row["seq"],
        "ts": row["ts"],
        "entity_type": row["entity_type"],
        "entity_id": row["entity_id"],
        "action": row["action"],
        "payload": json.loads(row["payload"]),
    }


def _backfill_orphans(conn: sqlite3.Connection, device_id: str) -> None:
    """Synthesize the create_mote/create_relationship history that never
    got logged for entities created before oplog logging existed in this
    codebase (fc0b379) -- see module docstring. Idempotent: a second call
    finds nothing, since the rows it inserts are themselves the evidence
    it checks for next time. Called at the top of push() so every device
    heals its own local garden before sending anything -- including a
    device that already pushed an incomplete history once before this
    existed, since the next push just sends the missing rows as new ones.

    Only runs while this device has never pulled from a remote. A
    replayed create is never logged locally on purpose (oplog.replaying()
    in core/merge.py, so replay doesn't loop back out as if it were new local
    history) -- so "no local create row" stops meaning "predates oplog"
    the moment a second device's history has ever been merged in; it as
    often just means "I received this from someone else." Only while
    this device's own oplog is the entire story for the garden so far is
    a missing create unambiguous.
    """
    if any(dev != device_id for dev in _all_state(conn)):
        return

    logged_motes: set[str] = set()
    logged_rels: set[str] = set()
    create_ts: dict[str, str] = {}
    for row in conn.execute(
        "SELECT action, payload, ts FROM oplog WHERE device_id = ? "
        "AND action IN ('create_mote', 'create_relationship')", (device_id,)
    ):
        payload = json.loads(row["payload"])
        (logged_motes if row["action"] == "create_mote" else logged_rels).add(payload["id"])
        create_ts[payload["id"]] = row["ts"]

    existing_ts = [r["ts"] for r in conn.execute(
        "SELECT ts FROM oplog WHERE device_id = ?", (device_id,)
    )]
    anchor = format_ts(
        datetime.fromisoformat(min(existing_ts)) - timedelta(seconds=1)
        if existing_ts else datetime(1970, 1, 1, tzinfo=timezone.utc)
    )

    orphan_motes = [r["id"] for r in conn.execute("SELECT id FROM motes") if r["id"] not in logged_motes]
    if orphan_motes:
        props = motes_repo._properties_by_mote(conn, orphan_motes)
        for mote_id in orphan_motes:
            oplog.append(conn, "mote", mote_id, "create_mote",
                         {"id": mote_id, "properties": props.get(mote_id, {})}, Stamp(anchor, device_id))
            create_ts[mote_id] = anchor

    orphan_rels = [r for r in conn.execute("SELECT * FROM relationships") if r["id"] not in logged_rels]
    for rel in orphan_rels:
        # max(), not a flat epsilon after the mote anchor: an orphan
        # relationship could in principle reference an endpoint that
        # *isn't* orphaned (a real, later create_mote already logged for
        # it) -- ts has to sort after THAT mote's real create too, not
        # just after the synthetic anchor, or replay would fail the
        # exact same way against a mote that hasn't "arrived" yet.
        rel_ts = max(create_ts.get(rel["from_id"], anchor), create_ts.get(rel["to_id"], anchor))
        oplog.append(conn, "relationship", rel["id"], "create_relationship",
                     {"id": rel["id"], "from_id": rel["from_id"], "to_id": rel["to_id"],
                      "type": rel["type"]}, Stamp(rel_ts, device_id))

    if orphan_motes or orphan_rels:
        conn.commit()


def push(conn: sqlite3.Connection, remote: Remote, garden_id: str, *, resend: bool = False) -> dict[str, Any]:
    """Send this device's oplog rows since the last push. A device only
    ever adds new history, never edits existing history -- that is what
    makes this safe to retry. `resend` sends the whole history again (the
    server ignores what it has)."""
    device_id = get_device_id()
    _backfill_orphans(conn, device_id)
    last_pushed = 0 if resend else _get_state(conn, device_id)
    rows = conn.execute(
        "SELECT * FROM oplog WHERE device_id = ? AND seq > ? ORDER BY seq", (device_id, last_pushed)
    ).fetchall()
    if not rows:
        return {"pushed": 0}
    result = sync_remote.push(remote, garden_id, [_row_to_op(row) for row in rows])
    _set_state(conn, device_id, rows[-1]["seq"])
    conn.commit()
    return {"pushed": len(rows), **result}


def _fetch(
    remote: Remote, garden_id: str, since_id: int, exclude: str | None, baseline: dict[str, int]
) -> tuple[list[dict[str, Any]], int]:
    """Every op after `since_id` this garden hasn't replayed yet, and the
    cursor after the last page."""
    pending: dict[tuple[str, int], dict[str, Any]] = {}
    while True:
        page, next_since_id = sync_remote.pull_page(remote, garden_id, since_id, exclude)
        if not page:
            return list(pending.values()), since_id
        for op in page:
            if op["seq"] > baseline.get(op["device_id"], 0):
                pending[(op["device_id"], op["seq"])] = op
        since_id = next_since_id


def pull(conn: sqlite3.Connection, remote: Remote, garden_id: str) -> dict[str, Any]:
    """Replay every op this garden hasn't applied yet (core/merge.py), in
    stamp order -- which only saves holding a relationship whose mote is
    in the same pull: the result doesn't depend on the order.

    This device's own ops come back too until it has pushed here once (a
    fresh clone, a merge), and the per-device `seq` baseline drops any op
    already applied -- a resend hands some back."""
    self_id = get_device_id()
    baseline = _all_state(conn)
    exclude = self_id if baseline.get(self_id, 0) > 0 else None
    ops, cursor = _fetch(remote, garden_id, baseline.get(_SERVER_CURSOR_KEY, 0), exclude, baseline)
    ops.sort(key=lambda o: (Stamp.of(o["ts"], o["device_id"]), o["seq"]))

    applied = 0
    skipped: list[dict[str, Any]] = []
    advanced: dict[str, int] = {}
    for op in ops:
        note = merge.apply(conn, op)
        advanced[op["device_id"]] = max(advanced.get(op["device_id"], 0), op["seq"])
        if note is None:
            applied += 1
        else:
            skipped.append({"device_id": op["device_id"], "seq": op["seq"], **note})

    held = merge.retry_held(conn)
    for dev, seq in advanced.items():
        _set_state(conn, dev, max(seq, baseline.get(dev, 0)))
    _set_state(conn, _SERVER_CURSOR_KEY, cursor)
    conn.commit()
    return {"applied": applied, "skipped": skipped, "held": held}


def run(conn: sqlite3.Connection, remote: Remote, garden_id: str) -> dict[str, Any]:
    """push() then pull()."""
    return {"push": push(conn, remote, garden_id), "pull": pull(conn, remote, garden_id)}


def clone(remote: Remote, name: str, garden_id: str) -> dict[str, Any]:
    """Make the garden `name` here from the server's garden `garden_id`."""
    gardens_repo.create_garden(name)
    gardens_repo.set_garden_id(name, garden_id)
    conn = open_garden(name)
    try:
        continue_after(conn, sync_remote.head(remote, garden_id))
        return pull(conn, remote, garden_id)
    finally:
        conn.close()


def continue_after(conn: sqlite3.Connection, seq: int) -> None:
    """Number this device's ops after `seq`, the highest it has stored on
    the server for this garden from an earlier copy of it. Without this a
    garden cloned or merged into again would reuse seqs the server
    already holds, and it would keep the old ops and drop the new ones."""
    if seq <= 0:
        return
    device_id = get_device_id()
    # Through negatives, so no row ever lands on a seq another still holds.
    conn.execute("UPDATE oplog SET seq = -seq WHERE device_id = ?", (device_id,))
    conn.execute("UPDATE oplog SET seq = ? - seq WHERE device_id = ?", (seq, device_id))
    top = conn.execute("SELECT COALESCE(MAX(seq), 0) AS n FROM oplog").fetchone()["n"]
    conn.execute("DELETE FROM sqlite_sequence WHERE name = 'oplog'")
    conn.execute("INSERT INTO sqlite_sequence (name, seq) VALUES ('oplog', ?)", (max(top, seq),))
    conn.commit()


def forget_progress(conn: sqlite3.Connection) -> None:
    """Start push and pull progress over, as for a garden that has just
    become a different garden on the server. Re-sending is safe (the
    server ignores duplicates) and so is re-applying (core/merge.py)."""
    conn.execute("DELETE FROM sync_state")
    conn.commit()


def pending(conn: sqlite3.Connection) -> int:
    """This device's ops not yet pushed."""
    device_id = get_device_id()
    return conn.execute(
        "SELECT COUNT(*) AS n FROM oplog WHERE device_id = ? AND seq > ?", (device_id, _get_state(conn, device_id))
    ).fetchone()["n"]


def status(conn: sqlite3.Connection) -> dict[str, Any]:
    self_id = get_device_id()
    state = _all_state(conn)
    return {
        "device_id": self_id,
        "local_ops_pending": pending(conn),
        "held": merge.held_count(conn),
        "remotes": [{"device_id": dev, "last_applied_seq": seq}
                    for dev, seq in state.items() if dev not in (self_id, _SERVER_CURSOR_KEY)],
    }

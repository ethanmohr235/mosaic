# core/sync.py
"""Multi-device sync, built on the oplog (core/oplog.py) rather than the
garden's own .db file -- syncing a live SQLite file through a folder-
sync tool (Syncthing, Dropbox, iCloud Drive) risks corruption from two
writers; syncing an *append-only log of small immutable files* through
the same tool does not, because no device ever edits a file another
device wrote.

Each device periodically push()es its own new oplog rows out as one
more file under `<sync_dir>/<garden>/ops/`, and pull()s in whatever
files other devices have dropped there, replaying their ops through the
ordinary core/motes.py and core/relationships.py primitives (with
core.oplog.suppressed() active, so replay doesn't re-log itself and
loop back out).

Conflict policy, deliberately simple for one person's own devices:
  - a create is skipped if the id already exists locally (first writer
    wins -- ids are uuid4, so a real collision across devices is not
    expected outside of a clone())
  - a property/relationship edit is last-write-wins by timestamp,
    compared against the target's own `updated_at`, so two devices
    editing *different* properties on the same mote never conflict at
    all
  - a delete always wins over anything that arrives referencing the
    now-missing entity ("delete_won", recorded but not treated as an
    error)
No vector clocks: this assumes each device's OS clock is roughly
correct, which is reasonable for one person's own devices and is not
worth the complexity of building around for v1.
"""
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from core import gardens as gardens_repo
from core import motes as motes_repo
from core import oplog
from core import relationships as rel_repo
from core import sync_remote
from core.db import get_connection, initialize_database
from core.device import get_device_id

OPS_SUBDIR = "ops"

# Reserved key in sync_state (never a real device_id, which is always a
# uuid4) holding how far this garden has paged through the server's
# global ops cursor -- see _get_server_cursor/_set_server_cursor below.
_SERVER_CURSOR_KEY = "__server_cursor__"


def _ops_dir(sync_dir: str | Path, garden: str) -> Path:
    return Path(sync_dir) / garden / OPS_SUBDIR


def _resolve_sync_dir(garden: str, sync_dir: str | Path | None, *, required: bool = True):
    if sync_dir is not None:
        return sync_dir
    configured = gardens_repo.get_sync_dir(garden)
    if configured is None and required:
        raise ValueError(f"No sync folder configured for {garden!r} -- run sync init() first")
    return configured


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


def _get_server_cursor(conn: sqlite3.Connection) -> int:
    return _get_state(conn, _SERVER_CURSOR_KEY)


def _set_server_cursor(conn: sqlite3.Connection, since_id: int) -> None:
    _set_state(conn, _SERVER_CURSOR_KEY, since_id)


def _mote_exists(conn: sqlite3.Connection, mote_id: str) -> bool:
    return conn.execute("SELECT 1 FROM motes WHERE id = ?", (mote_id,)).fetchone() is not None


def _relationship_exists(conn: sqlite3.Connection, rel_id: str) -> bool:
    return conn.execute("SELECT 1 FROM relationships WHERE id = ?", (rel_id,)).fetchone() is not None


def _row_to_op(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "op_id": row["op_id"],
        "device_id": row["device_id"],
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
    replayed create is never logged locally on purpose (oplog.suppressed()
    in pull(), so replay doesn't loop back out as if it were new local
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
    anchor = (
        (datetime.fromisoformat(min(existing_ts)) - timedelta(seconds=1)).isoformat()
        if existing_ts else datetime(1970, 1, 1, tzinfo=timezone.utc).isoformat()
    )

    orphan_motes = [r["id"] for r in conn.execute("SELECT id FROM motes") if r["id"] not in logged_motes]
    if orphan_motes:
        props = motes_repo._properties_by_mote(conn, orphan_motes)
        for mote_id in orphan_motes:
            oplog.append(conn, device_id, "mote", mote_id, "create_mote",
                         {"id": mote_id, "properties": props.get(mote_id, {})}, ts=anchor)
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
        oplog.append(conn, device_id, "relationship", rel["id"], "create_relationship",
                     {"id": rel["id"], "from_id": rel["from_id"], "to_id": rel["to_id"],
                      "type": rel["type"]}, ts=rel_ts)

    if orphan_motes or orphan_rels:
        conn.commit()


def push(
    conn: sqlite3.Connection,
    garden: str,
    sync_dir: str | Path | None = None,
    *,
    resend: bool = False,
) -> dict[str, Any]:
    """Write this device's oplog rows since the last push as one new,
    immutable batch (a file under ops/ for a local folder, one POST for
    a remote server). A device only ever adds new history, never edits
    existing history -- that is what makes this safe for a naive
    folder-sync tool to carry, and safe to retry against a server.

    `resend=True` ignores this device's own push progress and resends
    its entire local history -- needed the first time a garden already
    synced through one sync_dir is pointed at a different, empty one
    (see the migration note in core/sync.py's module docstring): without
    it, a device that already pushed everything to the old sync_dir
    would believe it has nothing left to push to the new one."""
    sync_dir = _resolve_sync_dir(garden, sync_dir)
    device_id = get_device_id()
    _backfill_orphans(conn, device_id)
    last_pushed = 0 if resend else _get_state(conn, device_id)

    rows = conn.execute(
        "SELECT * FROM oplog WHERE device_id = ? AND seq > ? ORDER BY seq",
        (device_id, last_pushed),
    ).fetchall()
    if not rows:
        return {"pushed": 0}

    to_seq = rows[-1]["seq"]

    if sync_remote.is_remote(sync_dir):
        ops = [_row_to_op(row) for row in rows]
        result = sync_remote.push(sync_dir, garden, device_id, ops)
        _set_state(conn, device_id, to_seq)
        conn.commit()
        return {"pushed": len(rows), **result}

    ops_dir = _ops_dir(sync_dir, garden)
    ops_dir.mkdir(parents=True, exist_ok=True)
    from_seq = rows[0]["seq"]
    path = ops_dir / f"{device_id}__{from_seq}-{to_seq}.jsonl"
    with path.open("w") as f:
        for row in rows:
            f.write(json.dumps(_row_to_op(row)) + "\n")

    _set_state(conn, device_id, to_seq)
    conn.commit()
    return {"pushed": len(rows), "file": str(path)}


def _apply_op(conn: sqlite3.Connection, op: dict[str, Any]) -> dict[str, Any] | None:
    """Replay one remote op. Returns a note describing what was skipped
    and why, or None if it applied cleanly."""
    action = op["action"]
    payload = op["payload"]
    ts = op["ts"]

    if action == "create_mote":
        if not _mote_exists(conn, payload["id"]):
            motes_repo.create_mote(conn, payload["properties"], id=payload["id"])
        return None

    if action == "set_property":
        mote_id, key, value = payload["mote_id"], payload["key"], payload["value"]
        if not _mote_exists(conn, mote_id):
            return {"reason": "delete_won", "entity": mote_id}
        local = conn.execute(
            "SELECT updated_at FROM mote_properties WHERE mote_id = ? AND key = ?", (mote_id, key)
        ).fetchone()
        if local is not None and local["updated_at"] is not None and local["updated_at"] >= ts:
            return {"reason": "shadowed", "entity": f"{mote_id}.{key}"}
        motes_repo.update_mote(conn, mote_id, {key: value})
        return None

    if action == "delete_property":
        mote_id, key = payload["mote_id"], payload["key"]
        local = conn.execute(
            "SELECT updated_at FROM mote_properties WHERE mote_id = ? AND key = ?", (mote_id, key)
        ).fetchone()
        if local is None:
            return None
        if local["updated_at"] is not None and local["updated_at"] >= ts:
            return {"reason": "shadowed", "entity": f"{mote_id}.{key}"}
        motes_repo.delete_property(conn, mote_id, key)
        return None

    if action == "delete_mote":
        motes_repo.delete_mote(conn, payload["id"])
        return None

    if action == "create_relationship":
        if not _relationship_exists(conn, payload["id"]):
            rel_repo.create_relationship(
                conn, payload["from_id"], payload["to_id"], payload["type"], id=payload["id"]
            )
        return None

    if action == "update_relationship":
        rel_id = payload["id"]
        if not _relationship_exists(conn, rel_id):
            return {"reason": "delete_won", "entity": rel_id}
        local = conn.execute(
            "SELECT updated_at FROM relationships WHERE id = ?", (rel_id,)
        ).fetchone()
        if local is not None and local["updated_at"] is not None and local["updated_at"] >= ts:
            return {"reason": "shadowed", "entity": rel_id}
        fields = {k: v for k, v in payload.items() if k != "id"}
        if "type" in fields:
            fields["type_"] = fields.pop("type")
        rel_repo.update_relationship(conn, rel_id, **fields)
        return None

    if action == "delete_relationship":
        rel_repo.delete_relationship(conn, payload["id"])
        return None

    raise ValueError(f"Unknown oplog action: {action!r}")


def pull(conn: sqlite3.Connection, garden: str, sync_dir: str | Path | None = None) -> dict[str, Any]:
    """Replay every op from every other device that we haven't applied
    yet. Ops are merged and sorted by timestamp before replay (not
    applied batch-by-batch) so a relationship created moments after its
    mote, even from a different device, is very likely to land in the
    right order -- this relies on the same roughly-synced-clocks
    assumption noted at the top of this module.

    The per-device `seq` baseline filter below is applied regardless of
    transport, including against the server: the server's own cursor is
    coarser than per-device seq, so a resend (push(resend=True), see the
    migration note above) can hand back ops a device already applied --
    this filter silently no-ops those, same as it already silently
    no-ops a stale local `.jsonl` file today."""
    sync_dir = _resolve_sync_dir(garden, sync_dir)
    self_id = get_device_id()
    baseline = _all_state(conn)
    pending: dict[tuple[str, int], dict[str, Any]] = {}

    if sync_remote.is_remote(sync_dir):
        since_id = _get_server_cursor(conn)
        next_since_id = since_id
        while True:
            page, next_since_id = sync_remote.pull_page(sync_dir, garden, self_id, next_since_id)
            if not page:
                break
            for op in page:
                if op["seq"] > baseline.get(op["device_id"], 0):
                    pending[(op["device_id"], op["seq"])] = op
    else:
        ops_dir = _ops_dir(sync_dir, garden)
        if not ops_dir.exists():
            return {"applied": 0, "skipped": []}
        for path in sorted(ops_dir.glob("*.jsonl")):
            for line in path.read_text().splitlines():
                if not line.strip():
                    continue
                op = json.loads(line)
                if op["device_id"] == self_id:
                    continue  # our own history, echoed back by the sync folder
                if op["seq"] > baseline.get(op["device_id"], 0):
                    pending[(op["device_id"], op["seq"])] = op

    ordered = sorted(pending.values(), key=lambda o: (o["ts"], o["device_id"], o["seq"]))

    applied = 0
    skipped: list[dict[str, Any]] = []
    advanced: dict[str, int] = {}
    stalled: set[str] = set()
    # An op left unapplied because its device stalled must still be
    # fetchable on the *next* pull -- the local .jsonl file it came from
    # just sits in ops_dir untouched, so the existing per-device seq
    # baseline naturally re-offers it next time. The server transport has
    # no such file to re-read: advancing the cursor past it would lose it
    # for good. So the cursor only ever advances up to the earliest
    # unresolved op, leaving it (and everything after it) to be re-fetched
    # and re-attempted on a later pull.
    unresolved_since_id: int | None = None
    with oplog.suppressed():
        for op in ordered:
            dev = op["device_id"]
            if dev in stalled:
                if "server_id" in op:
                    unresolved_since_id = min(unresolved_since_id or op["server_id"], op["server_id"])
                continue
            try:
                note = _apply_op(conn, op)
            except sqlite3.IntegrityError as e:
                skipped.append({"device_id": dev, "seq": op["seq"], "reason": str(e)})
                stalled.add(dev)
                if "server_id" in op:
                    unresolved_since_id = min(unresolved_since_id or op["server_id"], op["server_id"])
                continue
            applied += 1
            advanced[dev] = op["seq"]
            if note is not None:
                skipped.append({"device_id": dev, "seq": op["seq"], **note})

    for dev, seq in advanced.items():
        _set_state(conn, dev, seq)
    if sync_remote.is_remote(sync_dir):
        cursor = next_since_id if unresolved_since_id is None else unresolved_since_id - 1
        _set_server_cursor(conn, cursor)
    conn.commit()
    return {"applied": applied, "skipped": skipped}


def run(
    conn: sqlite3.Connection, garden: str, sync_dir: str | Path | None = None, *, resend: bool = False
) -> dict[str, Any]:
    """push() then pull() -- the everyday one-shot sync. `resend` is for
    moving an already-synced garden onto a new, empty sync_dir -- see
    push()'s docstring."""
    sync_dir = _resolve_sync_dir(garden, sync_dir)
    return {"push": push(conn, garden, sync_dir, resend=resend), "pull": pull(conn, garden, sync_dir)}


def status(conn: sqlite3.Connection, garden: str, sync_dir: str | Path | None = None) -> dict[str, Any]:
    sync_dir = _resolve_sync_dir(garden, sync_dir, required=False)
    self_id = get_device_id()
    state = _all_state(conn)
    own_pushed = state.get(self_id, 0)
    own_total = conn.execute(
        "SELECT COUNT(*) AS n FROM oplog WHERE device_id = ?", (self_id,)
    ).fetchone()["n"]
    remotes = [
        {"device_id": dev, "last_applied_seq": seq}
        for dev, seq in state.items() if dev not in (self_id, _SERVER_CURSOR_KEY)
    ]
    return {
        "device_id": self_id,
        "sync_dir": str(sync_dir) if sync_dir else None,
        "local_ops": own_total,
        "local_ops_pushed": own_pushed,
        "local_ops_pending": own_total - own_pushed,
        "remotes": remotes,
    }


def init(garden: str, sync_dir: str | Path, clone: bool = False) -> dict[str, Any]:
    """Point `garden` at a shared folder or sync server. If the target
    already holds history for it and there's no local garden by that
    name yet, the caller must pass clone=True -- otherwise this would
    silently create an empty garden that then quietly diverges from real
    history the user clearly meant to join."""
    if sync_remote.is_remote(sync_dir):
        has_history = sync_remote.has_history(sync_dir, garden)
    else:
        ops_dir = _ops_dir(sync_dir, garden)
        ops_dir.mkdir(parents=True, exist_ok=True)
        has_history = any(ops_dir.glob("*.jsonl"))

    if not gardens_repo.garden_exists(garden):
        if has_history and not clone:
            raise ValueError(
                f"{str(sync_dir)!r} already has sync history for {garden!r} -- "
                "pass clone=True to materialize it locally"
            )
        gardens_repo.create_garden(garden)

    gardens_repo.set_sync_dir(garden, str(sync_dir))

    cloned = False
    if has_history:
        conn = get_connection(garden)
        try:
            initialize_database(conn)
            pull(conn, garden, sync_dir)
            cloned = True
        finally:
            conn.close()

    return {"garden": garden, "sync_dir": str(sync_dir), "cloned": cloned}

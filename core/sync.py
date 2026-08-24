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
from pathlib import Path
from typing import Any

from core import gardens as gardens_repo
from core import motes as motes_repo
from core import oplog
from core import relationships as rel_repo
from core.db import get_connection, initialize_database
from core.device import get_device_id

OPS_SUBDIR = "ops"


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


def push(conn: sqlite3.Connection, garden: str, sync_dir: str | Path | None = None) -> dict[str, Any]:
    """Write this device's oplog rows since the last push as one new,
    immutable batch file. A device only ever creates files under ops/,
    never edits one -- that is what makes this safe for a naive folder-
    sync tool to carry."""
    sync_dir = _resolve_sync_dir(garden, sync_dir)
    device_id = get_device_id()
    last_pushed = _get_state(conn, device_id)

    rows = conn.execute(
        "SELECT * FROM oplog WHERE device_id = ? AND seq > ? ORDER BY seq",
        (device_id, last_pushed),
    ).fetchall()
    if not rows:
        return {"pushed": 0}

    ops_dir = _ops_dir(sync_dir, garden)
    ops_dir.mkdir(parents=True, exist_ok=True)
    from_seq, to_seq = rows[0]["seq"], rows[-1]["seq"]
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
    """Replay every op from every other device's files that we haven't
    applied yet. Ops from *all* pending files are merged and sorted by
    timestamp before replay (not applied file-by-file) so a relationship
    created moments after its mote, even from a different device, is
    very likely to land in the right order -- this relies on the same
    roughly-synced-clocks assumption noted at the top of this module."""
    sync_dir = _resolve_sync_dir(garden, sync_dir)
    self_id = get_device_id()
    ops_dir = _ops_dir(sync_dir, garden)
    if not ops_dir.exists():
        return {"applied": 0, "skipped": []}

    baseline = _all_state(conn)
    pending: dict[tuple[str, int], dict[str, Any]] = {}
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
    with oplog.suppressed():
        for op in ordered:
            dev = op["device_id"]
            if dev in stalled:
                continue
            try:
                note = _apply_op(conn, op)
            except sqlite3.IntegrityError as e:
                skipped.append({"device_id": dev, "seq": op["seq"], "reason": str(e)})
                stalled.add(dev)
                continue
            applied += 1
            advanced[dev] = op["seq"]
            if note is not None:
                skipped.append({"device_id": dev, "seq": op["seq"], **note})

    for dev, seq in advanced.items():
        _set_state(conn, dev, seq)
    conn.commit()
    return {"applied": applied, "skipped": skipped}


def run(conn: sqlite3.Connection, garden: str, sync_dir: str | Path | None = None) -> dict[str, Any]:
    """push() then pull() -- the everyday one-shot sync."""
    sync_dir = _resolve_sync_dir(garden, sync_dir)
    return {"push": push(conn, garden, sync_dir), "pull": pull(conn, garden, sync_dir)}


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
        for dev, seq in state.items() if dev != self_id
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
    """Point `garden` at a shared folder. If the folder already holds
    history for it and there's no local garden by that name yet, the
    caller must pass clone=True -- otherwise this would silently create
    an empty garden that then quietly diverges from real history the
    user clearly meant to join."""
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

# core/oplog.py
"""The change log core/sync.py replicates between devices. The 7
mutating primitives in core/motes.py and core/relationships.py each
append one row here, right before their own conn.commit() -- so an op
and the write it describes always land in the same transaction. Every
other mutation in the codebase (species, templates, property models,
simulation's writes via update_mote) bottoms out in one of those 7, so
this covers the whole system without touching any of them.
"""
import contextvars
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from core.db import generate_uuid

# Set while core/sync.py::pull() is replaying a remote device's ops
# through these same primitives -- without it, replaying an op would
# log it again as a new *local* op and push it right back out to the
# sync folder.
_replaying: contextvars.ContextVar[bool] = contextvars.ContextVar("_replaying", default=False)


@contextmanager
def suppressed():
    token = _replaying.set(True)
    try:
        yield
    finally:
        _replaying.reset(token)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def append(
    conn: sqlite3.Connection,
    device_id: str,
    entity_type: str,
    entity_id: str,
    action: str,
    payload: dict,
    ts: str | None = None,
) -> None:
    """Record one mutation. A no-op while suppressed() is active. Does
    not commit -- the caller's own commit covers this row too."""
    if _replaying.get():
        return
    conn.execute(
        "INSERT INTO oplog (op_id, device_id, ts, entity_type, entity_id, action, payload) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (generate_uuid(), device_id, ts or now_iso(), entity_type, entity_id, action, json.dumps(payload)),
    )

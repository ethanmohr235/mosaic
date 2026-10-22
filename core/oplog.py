# core/oplog.py
"""The change log core/sync.py replicates between devices. The 7
mutating primitives in core/motes.py and core/relationships.py each
append one row here, right before their own conn.commit() -- so an op
and the write it describes always land in the same transaction. Every
other mutation in the codebase (species, templates, property models,
simulation's writes via update_mote) bottoms out in one of those 7, so
this covers the whole system without touching any of them.

Each row's `ts` and `device_id` are the write's stamp (core/stamps.py).
"""
import contextvars
import json
import sqlite3
from contextlib import contextmanager
from typing import Iterator

from core.db import generate_uuid
from core.stamps import Stamp

# The stamp of the remote op core/merge.py is replaying through these
# same primitives, or None for an ordinary local write. While it is set
# the primitives write that stamp instead of minting one, and log
# nothing -- otherwise replaying an op would log it again as a new
# *local* op and push it right back out to the server.
_replaying: contextvars.ContextVar[Stamp | None] = contextvars.ContextVar("_replaying", default=None)


@contextmanager
def replaying(stamp: Stamp) -> Iterator[None]:
    token = _replaying.set(stamp)
    try:
        yield
    finally:
        _replaying.reset(token)


def replay_stamp() -> Stamp | None:
    return _replaying.get()


def append(
    conn: sqlite3.Connection,
    entity_type: str,
    entity_id: str,
    action: str,
    payload: dict,
    stamp: Stamp,
) -> None:
    """Record one mutation. A no-op while replaying. Does not commit --
    the caller's own commit covers this row too."""
    if _replaying.get() is not None:
        return
    conn.execute(
        "INSERT INTO oplog (op_id, device_id, ts, entity_type, entity_id, action, payload) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (generate_uuid(), stamp.device, stamp.ts, entity_type, entity_id, action, json.dumps(payload)),
    )

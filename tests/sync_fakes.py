"""An in-memory stand-in for sync_server, installed at the
core/sync_remote.py boundary, and simulated devices -- so the merge
(core/merge.py) is tested in every interleaving without Docker. The real
transport is tested against a live server in test_sync.py and
test_sync_server.py.

Each simulated device is its own gardens/ directory (its own database
and device_id), as in test_sync.py; `Device.active()` repoints
core.db.GARDENS_DIR for the calls that belong to it, and can skew that
device's clock.
"""
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

import core.db as db
from core import stamps, sync, sync_remote
from core.device import get_device_id
from core.motes import list_motes
from core.relationships import list_relationships
from core.sync_remote import Remote

REMOTE = Remote("http://fake-server", "token")
GARDEN_ID = "00000000-0000-4000-8000-000000000001"


@dataclass
class FakeServer:
    """One garden's ops, as sync_server keeps them. The pushing device is
    whichever is active (the real server reads it off the token)."""
    page_limit: int = 7
    ops: list[dict[str, Any]] = field(default_factory=list)

    def push(self, _remote: Remote, garden_id: str, ops: list[dict]) -> dict:
        device_id = get_device_id()
        have = {(o["garden"], o["device_id"], o["seq"]) for o in self.ops}
        fresh = [op for op in ops if (garden_id, device_id, op["seq"]) not in have]
        for op in fresh:
            self.ops.append({**op, "garden": garden_id, "device_id": device_id, "server_id": len(self.ops) + 1})
        return {"accepted": len(fresh), "duplicate": len(ops) - len(fresh)}

    def pull_page(self, _remote: Remote, garden_id: str, since_id: int,
                  exclude: str | None) -> tuple[list[dict], int]:
        page = [o for o in self.ops
                if o["garden"] == garden_id and o["server_id"] > since_id and o["device_id"] != exclude]
        page = page[: self.page_limit]
        return page, (page[-1]["server_id"] if page else since_id)

    def head(self, _remote: Remote, garden_id: str) -> int:
        device_id = get_device_id()
        return max((o["seq"] for o in self.ops if o["garden"] == garden_id and o["device_id"] == device_id),
                   default=0)

    def install(self, monkeypatch) -> None:
        monkeypatch.setattr(sync_remote, "push", self.push)
        monkeypatch.setattr(sync_remote, "pull_page", self.pull_page)
        monkeypatch.setattr(sync_remote, "head", self.head)


@dataclass
class Device:
    root: Path
    monkeypatch: Any
    skew: timedelta = timedelta(0)
    garden: str = "g"

    @contextmanager
    def active(self) -> Iterator[None]:
        skew = self.skew

        class _Skewed(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.now(tz) + skew

        with self.monkeypatch.context() as m:
            m.setattr(db, "GARDENS_DIR", self.root)
            m.setattr(stamps, "datetime", _Skewed)
            yield

    def conn(self):
        with self.active():
            return db.get_connection(self.garden)

    def run(self, conn) -> dict:
        with self.active():
            return sync.run(conn, REMOTE, GARDEN_ID)


def snapshot(conn) -> tuple[dict, dict]:
    """What a person would see: every mote's properties, every
    relationship's ends and type."""
    motes = {m.id: m.properties for m in list_motes(conn)}
    rels = {r.id: (r.from_id, r.to_id, r.type) for r in list_relationships(conn)}
    return motes, rels

# core/sync_status.py
"""What the last sync pass found, for every surface to read: the console's
top bar, Home and `sync` pane, `monoss sync status`, MCP. core/sync_all.py
writes it after every pass to `gardens/.sync_status.json`; the worker
(core/sync_worker.py) adds who is syncing. Nothing here is synced.
"""
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core import db

SYNCED, PENDING, LOCAL, CONFLICT, WAITING, ERROR = "synced", "pending", "local", "conflict", "waiting", "error"


@dataclass
class GardenState:
    name: str
    state: str
    garden_id: str | None = None
    pending: int = 0
    held: int = 0
    sent: int = 0
    received: int = 0
    detail: str | None = None


@dataclass
class Conflict:
    """A garden here and one on the server share a name. Nothing syncs
    under that name until `sync merge` or `sync rename` answers it."""
    name: str
    server_id: str
    other: str

    @property
    def question(self) -> str:
        return f"{self.other} also has a garden named {self.name!r}: merge or rename?"


@dataclass
class Report:
    ok: bool = True
    error: str | None = None
    finished: str | None = None
    gardens: list[GardenState] = field(default_factory=list)
    conflicts: list[Conflict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    received: list[str] = field(default_factory=list)

    def conflict(self, name: str) -> Conflict | None:
        return next((c for c in self.conflicts if c.name == name), None)

    def to_json(self) -> dict[str, Any]:
        out = asdict(self)
        for c, conflict in zip(out["conflicts"], self.conflicts):
            c["question"] = conflict.question
        return out


def status_path() -> Path:
    return db.GARDENS_DIR / ".sync_status.json"


def write(report: Report, worker: dict[str, Any] | None = None) -> None:
    """Atomically, so a reader never sees half a file."""
    report.finished = report.finished or datetime.now(timezone.utc).isoformat(timespec="seconds")
    body = {**report.to_json(), "worker": worker}
    path = status_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(body, indent=2))
    tmp.replace(path)


def read() -> dict[str, Any] | None:
    """The last pass, or None if this machine has never synced."""
    try:
        return json.loads(status_path().read_text())
    except (OSError, ValueError):
        return None


def conflicts() -> list[Conflict]:
    found = read() or {}
    return [Conflict(c["name"], c["server_id"], c["other"]) for c in found.get("conflicts", [])]

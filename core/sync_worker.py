# core/sync_worker.py
"""The one sync worker on a machine (docs/architecture.md -> Sync): it runs
core/sync_all.py's pass whenever there is something to do, for as long
as it lives. Either `monoss sync daemon` (a systemd user service; the
home machine runs one always) or a thread in the console, which starts
one only when no service holds the machine's worker lock -- so there is
never more than one, and the console just reads status beside a service.

A pass runs when:
- something here wants sending: a garden's .db changed and it now has
  unsent ops, or a garden appeared, went or was renamed (polled every
  `poll` seconds, then `debounce` seconds of quiet);
- the server says something changed (a long poll on /changes);
- `poke()` asks (the console's `refresh`);
- `heartbeat` seconds pass anyway.

Offline, passes back off up to a minute and the status says why.
"""
import fcntl
import logging
import os
import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO

from core import db
from core import gardens as gardens_repo
from core import sync
from core import sync_account
from core import sync_remote
from core.sync_all import sync_all
from core.sync_remote import SyncError

logger = logging.getLogger(__name__)

SERVICE, CONSOLE = "service", "console"
MAX_BACKOFF = 60.0


def _lock_path() -> Path:
    return db.GARDENS_DIR / ".sync.lock"


def running() -> bool:
    """Whether some process on this machine is the sync worker."""
    path = _lock_path()
    if not path.exists():
        return False
    with path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(handle, fcntl.LOCK_UN)
        return False


def _signature() -> dict[str, tuple[int, int]]:
    """Each garden's files' (mtime, size): cheap to read every few seconds."""
    out: dict[str, tuple[int, int]] = {}
    for name in gardens_repo.list_gardens():
        path = db.garden_db_path(name)
        stamps = [p.stat() for p in (path, path.with_name(path.name + "-wal")) if p.exists()]
        out[name] = (max(s.st_mtime_ns for s in stamps), sum(s.st_size for s in stamps))
    return out


def _wants_sending(names: set[str]) -> bool:
    """Whether any of these gardens has ops not yet sent. Only looks: a
    plain connection, no schema setup, so watching never writes."""
    for name in names:
        try:
            conn = db.get_connection(name)
        except Exception:
            continue
        try:
            if sync.pending(conn):
                return True
        except sqlite3.DatabaseError:
            continue  # not a garden, or one from before sync's tables
        finally:
            conn.close()
    return False


@dataclass
class Worker:
    kind: str
    poll: float = 2.0
    debounce: float = 3.0
    heartbeat: float = 300.0
    long_poll: float = 25.0
    _lock: IO[str] | None = None
    _wake: threading.Event = field(default_factory=threading.Event)
    _stop: threading.Event = field(default_factory=threading.Event)
    _passed: threading.Event = field(default_factory=threading.Event)
    _threads: list[threading.Thread] = field(default_factory=list)
    _full: bool = True

    @classmethod
    def acquire(cls, kind: str, **timing: float) -> "Worker | None":
        """This machine's worker, or None when another process is it."""
        path = _lock_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("a")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            return None
        return cls(kind, _lock=handle, **timing)

    @property
    def info(self) -> dict:
        return {"kind": self.kind, "pid": os.getpid()}

    def start(self) -> "Worker":
        for target in (self._run, self._watch, self._listen):
            thread = threading.Thread(target=target, name=f"sync-{target.__name__}", daemon=True)
            thread.start()
            self._threads.append(thread)
        return self

    def poke(self, full: bool = False) -> None:
        self._full = self._full or full
        self._wake.set()

    def stop(self, final_pass: bool = True, timeout: float = 5.0) -> None:
        """Stop, sending what is waiting first if the server answers
        within `timeout`."""
        self._stop.set()
        self._wake.set()
        for thread in self._threads:
            thread.join(timeout=timeout)
        if final_pass and sync_account.load() is not None:
            finisher = threading.Thread(target=self._pass, daemon=True)
            finisher.start()
            finisher.join(timeout=timeout)
        if self._lock is not None:
            self._lock.close()
            self._lock = None

    def request_stop(self) -> None:
        """Safe from a signal handler: run_forever() returns soon after."""
        self._stop.set()
        self._wake.set()

    def run_forever(self) -> None:
        """The service's main loop: start, wait for request_stop(), then
        stop, sending what is waiting."""
        self.start()
        while not self._stop.wait(1.0):
            pass
        self.stop()

    # ---- threads ----

    def _pass(self) -> bool:
        full, self._full = self._full, False
        report = sync_all(full=full, worker=self.info)
        self._passed.set()
        return report.ok

    def _run(self) -> None:
        backoff = 0.0
        while not self._stop.is_set():
            self._wake.clear()
            try:
                ok = self._pass() if sync_account.load() is not None else True
            except Exception:
                logger.exception("sync pass failed")
                ok = False
            backoff = 0.0 if ok else min(MAX_BACKOFF, max(2.0, backoff * 2))
            self._wake.wait(backoff or self.heartbeat)

    def _watch(self) -> None:
        seen = _signature()
        while not self._stop.wait(self.poll):
            now = _signature()
            if now == seen:
                continue
            changed = {n for n in now if now[n] != seen.get(n)}
            appeared_or_went = set(now) != set(seen)
            seen = now
            if appeared_or_went or _wants_sending(changed):
                self._settle()
                seen = _signature()
                self.poke()

    def _settle(self) -> None:
        """Wait for `debounce` seconds without a write: typing is many
        writes, and one pass after it is enough."""
        last = _signature()
        while not self._stop.wait(self.debounce):
            now = _signature()
            if now == last:
                return
            last = now

    def _listen(self) -> None:
        backoff = 0.0
        while not self._stop.is_set():
            account = sync_account.load()
            if account is None:
                self._stop.wait(self.heartbeat)
                continue
            try:
                gardens, _ = sync_remote.changes(account.remote(), account.cursor, wait=self.long_poll)
                backoff = 0.0
            except SyncError:
                backoff = min(MAX_BACKOFF, max(2.0, backoff * 2))
                self._stop.wait(backoff)
                continue
            if gardens and not self._stop.is_set():
                # Wait for the pass this wakes, so the next poll asks
                # from the cursor it leaves rather than the same one.
                self._passed.clear()
                self.poke()
                self._passed.wait(timeout=self.heartbeat)


# console/session.py
"""The one garden connection the console's window holds.

docs/console.md -> Architecture: one process, one window, and every app
namespace on the root API shares **one** connection owned by the root
rather than opening its own. This is that connection, plus the two
things that must be shared with it: the lock that serializes access (
pywebview calls in from a pool of worker threads) and the undo stack
(Undo stays global -- one Ctrl+Z, whichever pane made the edit).

A namespace holds the Session as `self._session` -- private, so
pywebview's js_api walk never reaches the connection -- and wraps each
bridge method in `locked`."""
import functools
import logging
import sqlite3
import threading
from dataclasses import dataclass, field
from typing import Callable, TypeVar

from api_common import best_effort_sync
from core import gardens as gardens_repo
from core.db import get_connection, initialize_database
from core.undo import UndoManager

logger = logging.getLogger(__name__)

# Same reasoning as motion/api.py: the console is routinely open beside
# an old window, the CLI or the MCP server on the same garden file.
BUSY_TIMEOUT_MS = 5000

F = TypeVar("F", bound=Callable)


def open_connection(garden: str) -> sqlite3.Connection:
    """A garden connection opened the way every app opens one."""
    conn = get_connection(garden)
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    initialize_database(conn)
    return conn


@dataclass
class Session:
    garden: str
    conn: sqlite3.Connection | None
    lock: threading.RLock = field(default_factory=threading.RLock)
    undo: UndoManager = field(default_factory=UndoManager)

    @classmethod
    def open(cls, garden: str | None = None) -> "Session":
        """`garden` None means the user's default (core/gardens.py::
        resolve_garden), as for every app. A best-effort sync starts on
        a background thread."""
        resolved = gardens_repo.resolve_garden(garden)
        session = cls(resolved, open_connection(resolved))
        best_effort_sync(resolved)
        return session

    def switch(self, garden: str) -> None:
        """Move to another garden: new connection, fresh undo stack (an
        undo recorded against the old garden's connection means nothing
        in the new one). The new garden is opened before the old one is
        closed, so an unknown name leaves the session as it was. Both
        gardens get a best-effort sync: leaving is a close, arriving an
        open."""
        with self.lock:
            resolved = gardens_repo.resolve_garden(garden)
            conn = open_connection(resolved)
            previous = self.garden
            self._close_conn()
            self.garden, self.conn = resolved, conn
            self.undo = UndoManager()
        best_effort_sync(previous)
        best_effort_sync(resolved)

    def close(self) -> None:
        with self.lock:
            if self.conn is None:
                return
            self._close_conn()
        best_effort_sync(self.garden)

    def _close_conn(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None


def locked(method: F) -> F:
    """Serialize a bridge method on its namespace's Session lock -- the
    console's `api_common.synchronized`, over `self._session` instead of
    a lock per Api. Reentrant, so a locked method may call another."""
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._session.lock:
            return method(self, *args, **kwargs)
    return wrapper  # type: ignore[return-value]

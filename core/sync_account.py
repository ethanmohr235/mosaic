# core/sync_account.py
"""This machine's sync account: which server, and the token it paired
with (docs/architecture.md -> Sync). One per machine, covering every
garden, in `gardens/.sync.json` -- readable only by its owner, since the
token is a password -- beside `.device_id` and never synced.

It also remembers, per garden id, the name and trash state the last sync
agreed on with the server (`known`). That third view is how
core/sync_all.py tells "renamed here" from "renamed elsewhere" when this
copy and the server disagree. `cursor` is how far this machine has read
the server's change feed.
"""
import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from core import db
from core import sync_remote
from core.device import get_device_id
from core.sync_remote import Remote


@dataclass
class Account:
    url: str
    device_name: str
    token: str
    cursor: int = 0
    known: dict[str, dict[str, Any]] = field(default_factory=dict)

    def remote(self) -> Remote:
        return Remote(self.url, self.token)

    def remember(self, garden_id: str, name: str, trashed: bool = False) -> None:
        self.known[garden_id] = {"name": name, "trashed": trashed}


def account_path() -> Path:
    return db.GARDENS_DIR / ".sync.json"


def load() -> Account | None:
    path = account_path()
    if not path.exists():
        return None
    return Account(**json.loads(path.read_text()))


def save(account: Account) -> None:
    """Atomically, and private from the first byte."""
    path = account_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(asdict(account), f, indent=2)
    tmp.replace(path)


def login(url: str, secret: str, device_name: str) -> Account:
    """Pair this machine with the server at `url` using the server's
    secret, and keep the token it hands back. Logging in again to the same
    server keeps what the last sync agreed on; another server starts
    over."""
    url = url.rstrip("/")
    token = sync_remote.pair(url, secret, get_device_id(), device_name)
    previous = load()
    account = Account(url=url, device_name=device_name, token=token)
    if previous is not None and previous.url == url:
        account.cursor, account.known = previous.cursor, previous.known
    save(account)
    return account


def logout() -> bool:
    path = account_path()
    if not path.exists():
        return False
    path.unlink()
    return True

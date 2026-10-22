# core/sync_all.py
"""One sync pass over every garden on this machine (docs/architecture.md
-> Sync). The worker (core/sync_worker.py) runs it; so does `sync now`.

A pass reads the server's change feed and garden list, then reconciles
three views of each garden -- this copy, the server's, and what the last
pass agreed on (core/sync_account.py's `known`) -- so a rename, a trash
or a restore made on any machine reaches every other:

- a garden here the server doesn't know is registered, unless its name
  is taken there (a conflict);
- a garden the server has and this machine doesn't is cloned, unless the
  name is taken here by another garden (a conflict);
- a rename or trash made on one side is made on the other; a garden held
  open by the console (gardens.is_held) waits for the next pass;
- a garden this machine keeps to itself (`sync local`) is left out both
  ways.

Then push and pull (core/sync.py) run for each garden with something to
send or receive. A conflict is asked, never guessed: `sync merge` and
`sync rename` (core/sync_commands.py) answer it.
"""
import logging
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from core import gardens as gardens_repo
from core import merge
from core import sync
from core import sync_account
from core import sync_remote
from core import sync_status
from core.sync_account import Account
from core.sync_remote import NameTaken, Remote, SyncError
from core.sync_status import Conflict, GardenState, Report

logger = logging.getLogger(__name__)


@dataclass
class _Pass:
    account: Account
    remote: Remote
    report: Report
    server: dict[str, dict[str, Any]] = field(default_factory=dict)
    here: dict[str, str] = field(default_factory=dict)       # garden id -> name
    kept_local: set[str] = field(default_factory=set)        # ids and names this machine keeps
    targets: dict[str, str] = field(default_factory=dict)    # garden id -> name, to push and pull
    fresh: set[str] = field(default_factory=set)             # ids that need a full first run

    def note(self, message: str) -> None:
        self.report.notes.append(message)

    def conflict(self, name: str, server_garden: dict[str, Any]) -> None:
        if self.report.conflict(name) is not None:
            return
        other = server_garden.get("created_by_name") or "another device"
        self.report.conflicts.append(Conflict(name, server_garden["id"], other))
        self.report.gardens.append(GardenState(name, sync_status.CONFLICT, gardens_repo.garden_id(name),
                                               detail=self.report.conflicts[-1].question))


def sync_all(*, full: bool = False) -> Report:
    """Sync every garden. `full` runs push and pull for each one even if
    the feed says nothing changed (the first pass after login does)."""
    account = sync_account.load()
    if account is None:
        report = Report(ok=False, error="not signed in -- run `monoss sync login ‹url›`")
        sync_status.write(report)
        return report
    p = _Pass(account, account.remote(), Report())
    try:
        changed, cursor = sync_remote.changes(p.remote, account.cursor)
        p.server = {g["id"]: g for g in sync_remote.list_gardens(p.remote)}
        _survey(p)
        _adopt_legacy(p)
        _reconcile(p)
        _run_targets(p, set(changed), full)
        if p.report.ok:
            account.cursor = cursor
    except SyncError as e:
        p.report.ok, p.report.error = False, str(e)
    finally:
        sync_account.save(account)
        sync_status.write(p.report)
    return p.report


# ---- what is here ----

def _survey(p: _Pass) -> None:
    for name in gardens_repo.list_gardens():
        try:
            if gardens_repo.is_local_only(name):
                p.kept_local.add(name)
                if (gid := gardens_repo.garden_id(name)) is not None:
                    p.kept_local.add(gid)
                p.report.gardens.append(GardenState(name, sync_status.LOCAL))
                continue
            if gardens_repo.legacy_sync_url(name) is None:
                p.here[gardens_repo.ensure_garden_id(name)] = name
        except sqlite3.DatabaseError as e:
            p.report.gardens.append(GardenState(name, sync_status.ERROR, detail=f"not readable: {e}"))


def _adopt_legacy(p: _Pass) -> None:
    """A garden synced the old way (one URL per garden, gardens/‹name›.db.
    sync_dir) becomes the server garden of the same name, which the
    server's v2 migration gave an id. Its progress starts over, which is
    safe: re-sending and re-applying change nothing."""
    by_name = {g["name"]: g for g in p.server.values() if not g["trashed"]}
    for name in gardens_repo.list_gardens():
        if name in p.kept_local or gardens_repo.legacy_sync_url(name) is None:
            continue
        found = by_name.get(name)
        gid = found["id"] if found else gardens_repo.ensure_garden_id(name)
        gardens_repo.set_garden_id(name, gid)
        conn = sync.open_garden(name)
        try:
            sync.forget_progress(conn)
        finally:
            conn.close()
        gardens_repo.drop_legacy_sync(name)
        p.here[gid] = name
        if found:
            p.account.remember(gid, name)
        p.fresh.add(gid)


# ---- reconciling ----

def _reconcile(p: _Pass) -> None:
    for gid, server_garden in p.server.items():
        if gid in p.kept_local:
            continue
        if gid in p.here:
            _match(p, gid, p.here[gid], server_garden)
        else:
            _absent_here(p, gid, server_garden)
    for gid, name in p.here.items():
        if gid not in p.server:
            _register(p, gid, name)


def _match(p: _Pass, gid: str, name: str, theirs: dict[str, Any]) -> None:
    """The same garden is here and on the server: carry a trash or a
    rename whichever way it went."""
    known = p.account.known.get(gid)
    if theirs["trashed"]:
        if known is not None and known["trashed"]:
            return _restore_there(p, gid, name)
        return _trash_here(p, gid, name)
    if theirs["name"] == name:
        p.account.remember(gid, name)
        p.targets[gid] = name
    elif known is not None and known["name"] == theirs["name"]:
        _rename_there(p, gid, name, theirs)
    else:
        _rename_here(p, gid, name, theirs["name"])


def _trash_here(p: _Pass, gid: str, name: str) -> None:
    if gardens_repo.is_held(name):
        return _waiting(p, gid, name, "trashed on another device; moves aside once it isn't open")
    gardens_repo.delete_garden(name)
    p.account.remember(gid, name, trashed=True)
    p.note(f"moved {name!r} aside: it was removed on another device (`garden restore {name}` brings it back)")


def _restore_there(p: _Pass, gid: str, name: str) -> None:
    try:
        sync_remote.patch(p.remote, gid, trashed=False, name=name)
    except NameTaken as e:
        return p.conflict(name, e.garden)
    p.account.remember(gid, name)
    p.targets[gid] = name


def _rename_there(p: _Pass, gid: str, name: str, theirs: dict[str, Any]) -> None:
    try:
        sync_remote.patch(p.remote, gid, name=name)
    except NameTaken as e:
        return p.conflict(name, e.garden)
    p.account.remember(gid, name)
    p.targets[gid] = name


def _rename_here(p: _Pass, gid: str, name: str, new: str) -> None:
    if gardens_repo.garden_exists(new):
        return _waiting(p, gid, name, f"renamed {new!r} on another device, a name this machine already uses")
    if gardens_repo.is_held(name):
        return _waiting(p, gid, name, f"renamed {new!r} on another device; follows once it isn't open")
    gardens_repo.rename_garden(name, new)
    p.account.remember(gid, new)
    p.targets[gid] = new
    p.note(f"renamed {name!r} to {new!r}, as on another device")


def _absent_here(p: _Pass, gid: str, theirs: dict[str, Any]) -> None:
    """The server has a garden this machine doesn't (any more)."""
    known = p.account.known.get(gid)
    if known is not None and gardens_repo.trashed_garden_id(known["name"]) == gid:
        if theirs["trashed"]:
            return p.account.remember(gid, known["name"], trashed=True)
        if not known["trashed"]:
            sync_remote.patch(p.remote, gid, trashed=True)
            p.account.remember(gid, known["name"], trashed=True)
            return p.note(f"removed {known['name']!r} on every device")
        return _restore_here(p, gid, known["name"], theirs)
    if theirs["trashed"]:
        return p.account.remember(gid, theirs["name"], trashed=True)
    _clone_here(p, gid, theirs)


def _restore_here(p: _Pass, gid: str, trashed_name: str, theirs: dict[str, Any]) -> None:
    if gardens_repo.garden_exists(trashed_name):
        return p.conflict(trashed_name, theirs)
    gardens_repo.restore_garden(trashed_name)
    p.here[gid] = trashed_name
    p.note(f"restored {trashed_name!r}, as on another device")
    p.account.remember(gid, trashed_name)
    _match(p, gid, trashed_name, theirs)


def _clone_here(p: _Pass, gid: str, theirs: dict[str, Any]) -> None:
    name = theirs["name"]
    if gardens_repo.garden_exists(name):
        if name not in p.kept_local:
            p.conflict(name, theirs)
        return
    sync.clone(p.remote, name, gid)
    p.account.remember(gid, name)
    p.here[gid] = name
    p.note(f"brought in {name!r} from {theirs.get('created_by_name') or 'the server'}")
    p.report.gardens.append(GardenState(name, sync_status.SYNCED, gid))


def _register(p: _Pass, gid: str, name: str) -> None:
    try:
        sync_remote.register(p.remote, gid, name)
    except NameTaken as e:
        return p.conflict(name, e.garden)
    p.account.remember(gid, name)
    p.targets[gid] = name
    p.fresh.add(gid)


def _waiting(p: _Pass, gid: str, name: str, why: str) -> None:
    p.report.gardens.append(GardenState(name, sync_status.WAITING, gid, detail=why))


# ---- pushing and pulling ----

def _run_targets(p: _Pass, changed: set[str], full: bool) -> None:
    for gid, name in p.targets.items():
        if p.report.conflict(name) is not None:
            continue
        p.report.gardens.append(_run_one(p, gid, name, full or gid in changed or gid in p.fresh))


def _run_one(p: _Pass, gid: str, name: str, pull: bool) -> GardenState:
    """Push what this garden has to send; pull if anything may be
    waiting on the server for it."""
    state = GardenState(name, sync_status.SYNCED, gid)
    try:
        conn = sync.open_garden(name)
    except sqlite3.DatabaseError as e:
        return GardenState(name, sync_status.ERROR, gid, detail=f"not readable: {e}")
    try:
        state.sent = sync.push(conn, p.remote, gid)["pushed"]
        if pull:
            state.received = sync.pull(conn, p.remote, gid)["applied"]
        state.pending, state.held = sync.pending(conn), merge.held_count(conn)
    except SyncError as e:
        state.state, state.detail = sync_status.ERROR, str(e)
        p.report.ok, p.report.error = False, str(e)
    finally:
        conn.close()
    if state.received:
        p.report.received.append(name)
    if state.pending and state.state == sync_status.SYNCED:
        state.state = sync_status.PENDING
    return state

# core/sync_commands.py
"""The `sync` verb (docs/console.md -> Sync), shared by the console, the
CLI and MCP:

    sync                          the sync pane: every garden's state, and conflicts to answer
    sync now                      a full pass over every garden
    sync merge ‹garden›           answer a conflict: this garden joins the server's of that name
    sync rename ‹garden› ‹new›    answer a conflict: rename this one; the server's comes in as ‹garden›
    sync local ‹garden›           keep a garden on this machine only
    sync share ‹garden›           sync it again
    sync devices                  this account's devices
    sync revoke ‹device›          refuse a device's token (by name or id)

Logging in and out is the CLI's alone (`monoss sync login`), so the
server's secret never passes through the console's history or MCP.
"""
import shlex
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Sequence

from core import gardens as gardens_repo
from core import sync
from core import sync_account
from core import sync_remote
from core import sync_status
from core import sync_worker
from core.command_bar import CommandError
from core.sync_all import sync_all
from core.sync_status import Report

SYNC_VERB = "sync"
SHOW = "show"
ACTIONS = {
    "now": "sync every garden now",
    "merge": "join this garden with the server's garden of the same name",
    "rename": "rename this garden; the server's comes in under the old name",
    "local": "keep a garden on this machine only",
    "share": "sync a garden kept local again",
    "devices": "this account's devices",
    "revoke": "refuse a device's token",
}
_ARITY = {"now": (0, 0), "merge": (1, 1), "rename": (2, 2), "local": (1, 1), "share": (1, 1),
          "devices": (0, 0), "revoke": (1, 1)}
_USAGE = {"now": "", "merge": "‹garden›", "rename": "‹garden› ‹new name›", "local": "‹garden›",
          "share": "‹garden›", "devices": "", "revoke": "‹device›"}


@dataclass(frozen=True)
class SyncLine:
    action: str
    args: tuple[str, ...] = field(default_factory=tuple)


def parse(args: Sequence[str]) -> SyncLine:
    args = list(args)
    if not args:
        return SyncLine(SHOW)
    head, rest = args[0], tuple(args[1:])
    if head not in ACTIONS:
        raise CommandError(f"sync {head!r} is not an action -- try {', '.join(ACTIONS)}")
    least, most = _ARITY[head]
    if not least <= len(rest) <= most:
        raise CommandError(f"usage: sync {head} {_USAGE[head]}".rstrip())
    return SyncLine(head, rest)


def run(line: SyncLine) -> str:
    """Do `line` and say what happened. The server's refusals and an
    unreachable server read as command errors, not tracebacks."""
    try:
        return _run(line)
    except sync_remote.SyncError as e:
        raise CommandError(str(e)) from None


def _run(line: SyncLine) -> str:
    a = line.args
    if line.action == "now":
        return summary(sync_all(full=True))
    if line.action == "merge":
        return merge(a[0])
    if line.action == "rename":
        return rename(a[0], a[1])
    if line.action in ("local", "share"):
        return set_local(a[0], line.action == "local")
    if line.action == "devices":
        return devices_text()
    if line.action == "revoke":
        return revoke(a[0])
    raise CommandError(f"sync {line.action} is not an action")


def _account() -> sync_account.Account:
    account = sync_account.load()
    if account is None:
        raise CommandError("not signed in -- run `monoss sync login ‹url›` once on this machine")
    return account


def _exists(name: str) -> None:
    if not gardens_repo.garden_exists(name):
        raise CommandError(f"no garden named {name!r}")


def merge(name: str) -> str:
    """This garden becomes the server's garden of the same name: the two
    histories merge (core/merge.py) on the pass that follows."""
    _exists(name)
    account = _account()
    remote = account.remote()
    theirs = next((g for g in sync_remote.list_gardens(remote) if g["name"] == name and not g["trashed"]), None)
    if theirs is None:
        raise CommandError(f"the server has no garden named {name!r} to merge with")
    if theirs["id"] == gardens_repo.garden_id(name):
        raise CommandError(f"{name!r} is already the server's {name!r}")
    gardens_repo.set_garden_id(name, theirs["id"])
    gardens_repo.set_local_only(name, False)
    conn = sync.open_garden(name)
    try:
        sync.forget_progress(conn)
        sync.continue_after(conn, sync_remote.head(remote, theirs["id"]))
    finally:
        conn.close()
    account.remember(theirs["id"], name)
    sync_account.save(account)
    return f"merged {name!r} with the server's -- " + summary(sync_all(full=True))


def rename(name: str, new: str) -> str:
    """Rename this garden out of the way; the next pass registers it under
    the new name and brings the server's in under the old one."""
    _exists(name)
    if gardens_repo.is_held(name):
        raise CommandError(f"{name!r} is open -- cd to another garden first")
    gardens_repo.rename_garden(name, new)
    return f"renamed {name!r} to {new!r} -- " + summary(sync_all())


def set_local(name: str, local: bool) -> str:
    _exists(name)
    gardens_repo.set_local_only(name, local)
    if local:
        return f"{name!r} stays on this machine (other machines keep their copies)"
    return f"{name!r} syncs again"


def devices_text() -> str:
    account = _account()
    rows = sync_remote.devices(account.remote())
    lines = []
    for d in rows:
        mark = " (this machine)" if d["name"] == account.device_name else ""
        state = "revoked" if d["revoked"] else f"last seen {d['last_seen_at'] or 'never'}"
        lines.append(f"{d['name']}{mark}: {state} -- {d['id']}")
    return "\n".join(lines) or "no devices"


def revoke(which: str) -> str:
    account = _account()
    remote = account.remote()
    rows = sync_remote.devices(remote)
    match = [d for d in rows if which in (d["id"], d["name"])]
    if len(match) != 1:
        raise CommandError(f"{'no' if not match else 'more than one'} device matches {which!r}")
    sync_remote.revoke(remote, match[0]["id"])
    return f"revoked {match[0]['name']!r}: it can't sync until it logs in again"


def summary(report: Report) -> str:
    """One line on what a pass did."""
    if report.error and not report.gardens:
        return report.error
    sent = sum(g.sent for g in report.gardens)
    received = sum(g.received for g in report.gardens)
    parts = [f"{sent} sent, {received} received"]
    if report.conflicts:
        parts.append(f"{len(report.conflicts)} to answer (`sync`)")
    parts += report.notes
    if report.error:
        parts.append(report.error)
    return "; ".join(parts)


# ---- the pane and `monoss sync status` ----

def view() -> dict[str, Any]:
    """Who this machine is and the last pass, as `monoss sync status` and
    MCP read it."""
    account = sync_account.load()
    return {
        "command": SYNC_VERB,
        "account": {"url": account.url, "device": account.device_name} if account else None,
        "last": sync_status.read(),
    }


def pane() -> dict[str, Any]:
    """The `sync` pane: the questions to answer first, then one row per
    garden from the last pass (a garden made since shows as not yet
    synced), and who is doing the syncing."""
    found = view()
    last = found["last"] or {}
    rows = {g["name"]: g for g in last.get("gardens", [])}
    for name in gardens_repo.list_gardens():
        rows.setdefault(name, {"name": name, "state": "new", "pending": 0, "held": 0, "detail": "not synced yet"})
    worker = last.get("worker") or {}
    return {
        **found,
        "conflicts": [{"name": c["name"], "question": c["question"]} for c in last.get("conflicts", [])],
        "rows": sorted((r for r in rows.values() if r["state"] != sync_status.CONFLICT), key=lambda r: r["name"]),
        "notes": last.get("notes", []),
        "worker": {"running": sync_worker.running(), "kind": worker.get("kind")},
        "finished": _local_time(last.get("finished")),
    }


def badge(garden: str, conn) -> dict[str, Any] | None:
    """The top bar's mark for the open garden, or None when this machine
    isn't signed in: `state` (synced, pending, offline, conflict),
    `label`, `title` (the hover), plus `finished` and `received` from the
    last pass. Pending is counted live, so a write shows at once."""
    if sync_account.load() is None:
        return None
    last = sync_status.read() or {"ok": True, "error": None, "conflicts": [], "finished": None, "received": []}
    out = {"finished": last["finished"], "received": last.get("received", [])}
    waiting = sync.pending(conn) if conn is not None else 0
    if not last["ok"] and last.get("error"):
        return {**out, "state": "offline", "label": "offline", "title": last["error"]}
    if last["conflicts"]:
        n = len(last["conflicts"])
        return {**out, "state": "conflict", "label": f"? {n}",
                "title": "; ".join(c["question"] for c in last["conflicts"]) + " -- `sync` to answer"}
    if waiting:
        return {**out, "state": "pending", "label": f"↑{waiting}",
                "title": f"{waiting} change{'s' if waiting != 1 else ''} in {garden!r} not sent yet"}
    return {**out, "state": "synced", "label": "synced",
            "title": f"every garden synced; last pass {_local_time(last['finished'])}"}


def _local_time(iso: str | None) -> str:
    if not iso:
        return "never"
    return datetime.fromisoformat(iso).astimezone().strftime("%Y-%m-%d %H:%M")


def status_text() -> str:
    found = view()
    if found["account"] is None:
        return "not signed in -- run `monoss sync login ‹url›`"
    last = found["last"]
    head = f"{found['account']['device']} -> {found['account']['url']}"
    if last is None:
        return head + "\nnever synced"
    lines = [head, f"last pass {last['finished']}" + ("" if last["ok"] else f": {last['error']}")]
    lines += [f"? {c['question']}  (sync merge {_quote(c['name'])} | sync rename {_quote(c['name'])} ‹new›)"
              for c in last["conflicts"]]
    lines += [f"{g['name']}: {g['state']}" + (f" ({g['detail']})" if g.get("detail") else "")
              + (f", {g['pending']} to send" if g.get("pending") else "")
              + (f", {g['held']} held" if g.get("held") else "")
              for g in sorted(last["gardens"], key=lambda g: g["name"])]
    return "\n".join(lines)


def _quote(word: str) -> str:
    return shlex.quote(word)


def choices(rest: str) -> list[tuple[str, str]]:
    words = rest.split()
    done = words if not rest or rest[-1].isspace() else words[:-1]
    if not done:
        return list(ACTIONS.items())
    action = done[0]
    if action in ("merge", "rename") and len(done) == 1:
        return [(_quote(c.name), c.question) for c in sync_status.conflicts()]
    if action in ("local", "share") and len(done) == 1:
        return [(_quote(g), "garden") for g in gardens_repo.list_gardens()
                if gardens_repo.is_local_only(g) == (action == "share")]
    return []

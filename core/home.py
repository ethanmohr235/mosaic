# core/home.py
"""Home: the console's `gardens` and `garden` panes and the `garden`
subverbs (docs/console.md -> Settled for U9).

    gardens [--archived]                         the ownership tree
    garden [show] [‹name›]                       one garden's details
    garden new ‹name›
    garden rename ‹old› ‹new›
    garden rm ‹name›                             moved aside, not erased
    garden restore ‹name›
    garden archive|unarchive ‹name›
    garden export ‹name› ‹path›
    garden import ‹path› ‹new name›
    garden sync ‹name› ‹url›|off|now|auto on|off

The tree is core/projects.py's: owned gardens under their cover, then
loose gardens, then -- folded unless `--archived` -- archived ones.
Reading it opens every garden, so a surface asks on open and refresh,
never per keystroke.

What used to be the Gardens window's bridge lives here now: the counts
that tolerate a .db that isn't a garden, an import that never merges and
cleans up after itself, and the sync switches. None of it is on a
garden's undo stack -- a garden-level act is not one garden's history --
so removing is moved aside and `garden restore` brings it back."""
import json
import logging
import shlex
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Sequence

from core import garden_io
from core import gardens as gardens_repo
from core import projects
from core import sync as sync_repo
from core.command_bar import CommandError
from core.db import garden_db_path, get_connection, initialize_database

logger = logging.getLogger(__name__)

GARDENS_VERB, GARDEN_VERB = "gardens", "garden"
ARCHIVED_FLAG = "--archived"
SHOW = "show"
ACTIONS = {
    "new": "make a garden",
    "rename": "rename a garden (‹old› ‹new›)",
    "rm": "move a garden aside (garden restore brings it back)",
    "restore": "bring back a removed garden",
    "archive": "archive a garden and everything it owns",
    "unarchive": "un-archive a garden",
    "export": "write a garden to a JSON file",
    "import": "a JSON file into a new garden (‹path› ‹new name›)",
    "sync": "‹url›, off, now, auto on|off",
}
PROJECTS, LOOSE, ARCHIVED = "projects", "loose", "archived"


# ---- the tree ---------------------------------------------------------------

def counts(name: str) -> dict:
    """Mote and relationship counts, or None for both when the file can't
    be read as a garden. A .db in gardens/ is not guaranteed to be one of
    ours -- a half-written copy from a folder-sync tool looks exactly like
    one -- and a list is the wrong place to discover that with a traceback.
    A row with no counts still renames, exports and removes."""
    try:
        conn = get_connection(name)
    except Exception:
        logger.exception("could not open garden %r", name)
        return {"motes": None, "relationships": None}
    try:
        motes = conn.execute("SELECT COUNT(*) AS n FROM motes").fetchone()["n"]
        rels = conn.execute("SELECT COUNT(*) AS n FROM relationships").fetchone()["n"]
        return {"motes": motes, "relationships": rels}
    except sqlite3.DatabaseError:
        logger.exception("could not count %r", name)
        return {"motes": None, "relationships": None}
    finally:
        conn.close()


def gardens_view(show_archived: bool = False, current: str | None = None) -> dict:
    """The `gardens` pane: one row per garden, depth-first under its
    owner, in three groups -- trees that own something, loose gardens,
    archived ones (rows only with `show_archived`, else just the count)."""
    known = gardens_repo.list_gardens()
    nodes = projects.tree(known)
    problems = projects.check(known)
    troubled: dict[str, list[str]] = {}
    for p in problems:
        troubled.setdefault(p.garden, []).append(p.message)

    groups = {PROJECTS: [], LOOSE: [], ARCHIVED: []}
    for node in nodes:
        group = ARCHIVED if node.archived else PROJECTS if node.children else LOOSE
        groups[group].extend(_rows(node, 0, troubled, current))
    archived = groups[ARCHIVED]
    return {
        "command": GARDENS_VERB + (f" {ARCHIVED_FLAG}" if show_archived else ""),
        "current": current,
        "groups": [
            {"key": PROJECTS, "label": "projects", "rows": groups[PROJECTS]},
            {"key": LOOSE, "label": "loose", "rows": groups[LOOSE]},
            {"key": ARCHIVED, "label": "archived", "rows": archived if show_archived else [],
             "count": len(archived), "folded": not show_archived},
        ],
        "total": len(known),
        "problems": [p.message for p in problems],
    }


def _rows(node: projects.Node, depth: int, troubled: dict, current: str | None) -> list[dict]:
    owner = node.owner
    row = {
        "garden": node.garden,
        "depth": depth,
        "label": node.name,
        "cover": owner.cover.to_json() if owner else None,
        "owner": {"garden": owner.garden, "moteId": owner.mote_id} if owner else None,
        "archived": node.archived,
        "syncing": gardens_repo.get_sync_dir(node.garden) is not None,
        "current": node.garden == current,
        "problems": troubled.get(node.garden, []),
        **counts(node.garden),
    }
    out = [row]
    for child in node.children:
        out.extend(_rows(child, depth + 1, troubled, current))
    return out


def path_of(garden: str, claims: dict | None = None) -> list[str]:
    """The ownership path from the top of the tree down to `garden`, the
    breadcrumb's crumbs: `["college-fall-2026", "hw4"]`. A disputed or
    loose garden is its own top. Cycle-safe."""
    claims = projects.owners() if claims is None else claims
    path = [garden]
    while True:
        owner = projects.owner_of(path[0], claims)
        if owner is None or owner.garden in path:
            return path
        path.insert(0, owner.garden)


# ---- one garden ---------------------------------------------------------

def detail(name: str) -> dict:
    """The `garden` pane: where it is, how big, when it last changed, how
    it syncs, what owns it, what it owns, and what is wrong."""
    if not gardens_repo.garden_exists(name):
        raise CommandError(f"no garden named {name!r}")
    known = gardens_repo.list_gardens()
    claims = projects.owners(known)
    owner = projects.owner_of(name, claims)
    db_path = garden_db_path(name)
    stat = db_path.stat()
    sync_dir = gardens_repo.get_sync_dir(name)
    return {
        "command": f"{GARDEN_VERB} {_quote(name)}",
        "garden": name,
        "path": str(db_path),
        "crumbs": path_of(name, claims),
        **counts(name),
        "bytes": stat.st_size,
        "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
        "sync": {"url": sync_dir, "auto": gardens_repo.get_auto_sync_enabled(name)} if sync_dir else None,
        "owner": {"garden": owner.garden, "moteId": owner.mote_id, **owner.cover.to_json()} if owner else None,
        "owns": sorted(g for g, found in claims.items() if any(o.garden == name for o in found)),
        "archivedBy": projects.archived_by(name, known),
        "problems": [p.message for p in projects.check(known) if p.garden == name],
    }


def _quote(word: str) -> str:
    return shlex.quote(word)


# ---- the grammar ------------------------------------------------------------

@dataclass(frozen=True)
class GardenLine:
    action: str                       # SHOW, or a key of ACTIONS
    args: tuple[str, ...] = field(default_factory=tuple)

    @property
    def shows(self) -> bool:
        return self.action == SHOW


_ARITY = {"new": (1, 1), "rename": (2, 2), "rm": (1, 1), "restore": (1, 1), "archive": (1, 1),
          "unarchive": (1, 1), "export": (1, 2), "import": (1, 2), "sync": (2, 3)}


def parse(args: Sequence[str]) -> GardenLine:
    """`garden` alone and `garden ‹name›` show; a first word that is an
    action is the action (a garden named like one is `garden show ‹name›`)."""
    args = list(args)
    if not args:
        return GardenLine(SHOW)
    head, rest = args[0], args[1:]
    if head == SHOW:
        if len(rest) > 1:
            raise CommandError("garden show takes one garden")
        return GardenLine(SHOW, tuple(rest))
    if head not in ACTIONS:
        if rest:
            raise CommandError(f"garden {head!r} is not an action -- try {', '.join(ACTIONS)}")
        return GardenLine(SHOW, (head,))
    least, most = _ARITY[head]
    if not least <= len(rest) <= most:
        raise CommandError(f"garden {head} takes {_usage(head)}")
    if head == "sync":
        _check_sync(rest)
    return GardenLine(head, tuple(rest))


def _usage(action: str) -> str:
    return {"new": "‹name›", "rename": "‹old› ‹new›", "rm": "‹name›", "restore": "‹name›",
            "archive": "‹name›", "unarchive": "‹name›", "export": "‹name› [‹path›]",
            "import": "[‹path›] ‹new name›", "sync": "‹name› ‹url›|off|now|auto on|off"}[action]


def _check_sync(rest: list[str]) -> None:
    what = rest[1]
    if what == "auto":
        if len(rest) != 3 or rest[2] not in ("on", "off"):
            raise CommandError("garden sync ‹name› auto on|off")
    elif len(rest) != 2:
        raise CommandError(f"garden takes {_usage('sync')}")


def parse_text(command: str) -> GardenLine:
    try:
        words = shlex.split(command or "")
    except ValueError as e:
        raise CommandError(f"garden: {e}") from None
    if not words or words[0] != GARDEN_VERB:
        raise CommandError("not a garden line")
    return parse(words[1:])


def parse_gardens(args: Sequence[str]) -> bool:
    """`gardens [--archived]` -> whether archived gardens are unfolded."""
    if list(args) not in ([], [ARCHIVED_FLAG]):
        raise CommandError(f"gardens takes only {ARCHIVED_FLAG}")
    return bool(args)


# ---- acting -----------------------------------------------------------------

@dataclass(frozen=True)
class Done:
    """What a garden action did: the message, and what the console must
    follow -- a rename of the garden it has open, say."""
    message: str
    renamed: tuple[str, str] | None = None
    created: str | None = None


def needs_path(line: GardenLine) -> bool:
    """Export and import without a path: the surface asks for one (a file
    dialog) and runs the line again with it."""
    return (line.action == "export" and len(line.args) == 1) or (line.action == "import" and len(line.args) == 1)


def run(line: GardenLine, current: str | None = None) -> Done:
    a = line.args
    if line.action == "new":
        gardens_repo.create_garden(a[0])
        return Done(f"made the garden {a[0]!r} -- `cd {_quote(a[0])}` opens it", created=a[0])
    if line.action == "rename":
        _exists(a[0])
        gardens_repo.rename_garden(a[0], a[1])
        return Done(f"renamed {a[0]!r} to {a[1]!r}", renamed=(a[0], a[1]))
    if line.action == "rm":
        _exists(a[0])
        if a[0] == current:
            raise CommandError(f"{a[0]!r} is open here -- cd to another garden first")
        gardens_repo.delete_garden(a[0])
        return Done(f"moved {a[0]!r} aside -- `garden restore {_quote(a[0])}` brings it back")
    if line.action == "restore":
        gardens_repo.restore_garden(a[0])
        return Done(f"restored {a[0]!r}")
    if line.action in ("archive", "unarchive"):
        _exists(a[0])
        return Done(projects.set_archived(a[0], line.action == "archive"))
    if line.action == "export":
        if len(a) < 2:
            raise CommandError("garden export needs a path")
        return Done(export_to(a[0], a[1]))
    if line.action == "import":
        if len(a) < 2:
            raise CommandError("garden import needs a path")
        return Done(import_from(a[0], a[1]), created=a[1])
    if line.action == "sync":
        return Done(sync(a[0], a[1:]))
    raise CommandError(f"garden {line.action} is not an action")


def _exists(name: str) -> None:
    if not gardens_repo.garden_exists(name):
        raise CommandError(f"no garden named {name!r}")


def export_to(name: str, path: str) -> str:
    _exists(name)
    target = Path(path).expanduser()
    conn = get_connection(name)
    try:
        data = garden_io.export_garden(conn)
    finally:
        conn.close()
    target.write_text(json.dumps(data, indent=2))
    return f"exported {name!r} to {target}"


def import_from(path: str, new_name: str) -> str:
    """Always into a new garden: garden_io refuses to merge into one that
    holds any incoming id, so the name comes first. A failed import removes
    the garden it just made, so a retry fails on the file, not the name."""
    source = Path(path).expanduser()
    try:
        data = json.loads(source.read_text())
    except (OSError, ValueError) as e:
        raise CommandError(f"cannot read {source}: {e}") from None
    gardens_repo.create_garden(new_name)
    conn = get_connection(new_name)
    try:
        initialize_database(conn)
        motes, rels = garden_io.import_garden(conn, data)
    except Exception:
        conn.close()
        gardens_repo.delete_garden(new_name)
        raise
    conn.close()
    return f"imported {motes} motes and {rels} relationships into the new garden {new_name!r}"


def sync(name: str, what: Sequence[str]) -> str:
    """`‹url›` points the garden at a sync server, `off` stops syncing
    (the oplog stays, so turning it back on resumes), `now` runs one sync
    and surfaces its errors, `auto on|off` gates the console's open,
    switch and close syncs."""
    _exists(name)
    head = what[0]
    if head == "off":
        gardens_repo.clear_sync_dir(name)
        return f"{name!r} no longer syncs"
    if head == "now":
        conn = get_connection(name)
        try:
            result = sync_repo.run(conn, name)
        finally:
            conn.close()
        return f"synced {name!r}: {_summary(result)}"
    if head == "auto":
        gardens_repo.set_auto_sync_enabled(name, what[1] == "on")
        return f"auto-sync for {name!r} is {what[1]}"
    sync_repo.init(name, head, clone=True)
    return f"{name!r} syncs with {head}"


def _summary(result: dict) -> str:
    push, pull = result.get("push") or {}, result.get("pull") or {}
    pushed = push.get("pushed", push.get("ops", 0))
    pulled = pull.get("applied", pull.get("ops", 0))
    return f"{pushed} sent, {pulled} received"


# ---- completion -------------------------------------------------------------

def choices(rest: str) -> list[tuple[str, str]]:
    words = rest.split()
    done = words if not rest or rest[-1].isspace() else words[:-1]
    names = [(_quote(g), "garden") for g in gardens_repo.list_gardens()]
    if not done:
        return [(a, d) for a, d in ACTIONS.items()] + [(SHOW, "a garden's details")] + names
    action = done[0]
    if action == "sync" and len(done) == 2:
        return [("now", "sync once"), ("off", "stop syncing"), ("auto", "on|off")]
    if action == "sync" and len(done) == 3 and done[2] == "auto":
        return [("on", ""), ("off", "")]
    if action in ("new", "import") or (action == "rename" and len(done) == 2):
        return []
    if len(done) == 1:
        return names
    return []

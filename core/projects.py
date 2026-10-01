# core/projects.py
"""Ownership: which mote owns which garden.

One convention carries the whole structure (`overview.md` ->
*Structure*): a mote carrying `garden: <name>` owns that garden, and that
mote is the project's cover. Nesting is recursion, so projects inside
projects need nothing new. A garden has at most one owner, which is what
keeps the shape a tree and the cover unambiguous.

Nothing here is enforced by the store, and every function below still
answers sensibly when the convention is absent: a garden with no owner is
loose, and loose gardens work everywhere.

Reading ownership means reading *other* gardens, because the owner lives
outside the garden it owns. That is the one cross-garden read `overview.md`
allows, and it is read-only: this module opens other gardens to look, and
writes only through the connection its caller handed it.

`start_project` is the only write. It creates the garden and links it in
one step, because creating the project is meant to be the only act of
filing."""
import logging
import re
import sqlite3
from dataclasses import dataclass

from core import db, gardens as gardens_repo, motes as motes_repo, undo

logger = logging.getLogger(__name__)

GARDEN_KEY = "garden"
# `archived: true` on an owner archives the garden it owns and everything
# beneath it; on a mote with no `garden` claim, it archives the garden the
# mote lives in -- which is how a loose garden, with no owner to carry it,
# is archived. Decided 2026-09-30 (`roadmap.md` -> *Structure*).
ARCHIVED_KEY = "archived"
ARCHIVE_MARKER_NAME = "Archived"

# What check() found wrong. A garden may appear under more than one.
DISPUTED = "disputed"
MISSING = "missing"
LOOP = "loop"

_SLUG_SPLIT_RE = re.compile(r"[^A-Za-z0-9]+")
_MAX_NAME = 64


@dataclass(frozen=True)
class Owner:
    """A mote that owns a garden -- the project's cover.

    `garden` is where the owner itself lives; `owns` is what it owns.
    `cover` is the handful of fields Home lists and Mora's title block
    prints; `properties` is everything else the mote carries."""
    garden: str
    mote_id: str
    name: str
    owns: str
    properties: dict

    @property
    def exists(self) -> bool:
        """Whether the garden this mote claims is actually on disk. False
        is a dangling claim -- check() reports it as MISSING."""
        return gardens_repo.garden_exists(self.owns)

    @property
    def cover(self) -> "Cover":
        return cover_of(self.properties, self.mote_id)

    @property
    def archived(self) -> bool:
        """Whether this owner itself says so -- not whether something
        above it does, which is `archived_by()`'s question."""
        return is_marked(self.properties)


# The cover's fields. Ordinary, unreserved properties -- a homework mote
# synced from Canvas already carries `due` and `status`, and reserving
# them would flag every such mote in the inspector as if it were special.
# `revision` is written by Submit (phase 6), not by hand.
COVER_FIELDS = ("due", "status", "author", "revision")


@dataclass(frozen=True)
class Cover:
    """What a project is called and where it stands. Each field is None
    when the owner doesn't carry it; a cover with nothing but a name is a
    perfectly good cover."""
    name: str
    due: str | None = None
    status: str | None = None
    author: str | None = None
    revision: str | None = None

    def to_json(self) -> dict:
        return {"name": self.name, **{key: getattr(self, key) for key in COVER_FIELDS}}


def cover_of(properties: dict, mote_id: str) -> Cover:
    """A mote's cover fields. A number where text was expected (`revision:
    3`) is read as its text rather than dropped, since a hand-typed
    revision is exactly that."""
    def text(key: str) -> str | None:
        value = properties.get(key)
        if isinstance(value, bool) or value is None:
            return None
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, str):
            return value.strip() or None
        return None

    return Cover(name=_name(properties, mote_id), **{key: text(key) for key in COVER_FIELDS})


@dataclass(frozen=True)
class Problem:
    """One thing wrong with the ownership graph, with the sentence a
    surface can show as-is."""
    kind: str
    garden: str
    owners: tuple[Owner, ...]
    message: str


@dataclass(frozen=True)
class Node:
    """A garden in the ownership tree, as Home lists it."""
    garden: str
    owner: Owner | None
    children: tuple["Node", ...] = ()
    # Archived itself or beneath something archived. Home hides these by
    # default; they stay searchable and still open, read-only.
    archived: bool = False

    @property
    def loose(self) -> bool:
        """No mote claims this garden. It is a root of the tree, and
        still openable -- demos and scratch work live here."""
        return self.owner is None

    @property
    def name(self) -> str:
        """What to label the row with: the cover's name where there is a
        cover, and otherwise the garden's own name, which is its identity
        on disk."""
        return self.owner.name if self.owner is not None else self.garden


# --- reading it ------------------------------------------------------

def _name(properties: dict, mote_id: str) -> str:
    """A cover's name, or its id prefix. Deliberately not
    `rules.label_of`: that reads an unnamed mote's `equation` as its
    label, which is right for a rule and wrong for a project's cover."""
    name = properties.get("name")
    return name if isinstance(name, str) and name.strip() else mote_id[:8]


def garden_named_by(properties: dict) -> str | None:
    """The garden a mote claims, or None. A non-string or blank value is
    no claim at all: the property is hand-editable from every surface,
    and half-typed data should read as absent rather than raise."""
    claimed = properties.get(GARDEN_KEY)
    if not isinstance(claimed, str):
        return None
    claimed = claimed.strip()
    return claimed or None


def owners_in(conn: sqlite3.Connection, garden: str) -> list[Owner]:
    """Every mote in this one garden that claims a garden.

    Asks for the `garden` key alone first, then loads the full properties
    of just the motes that carry it -- an ownership scan crosses every
    garden the user has, so reading every property of every mote to find
    the two that matter would make Home's cost the size of the whole
    library."""
    cur = conn.cursor()
    cur.execute("SELECT mote_id FROM mote_properties WHERE key = ?", (GARDEN_KEY,))
    ids = [row["mote_id"] for row in cur.fetchall()]
    found = []
    for mote in motes_repo.get_motes(conn, ids):
        claimed = garden_named_by(mote.properties)
        if claimed is None:
            continue
        found.append(Owner(
            garden=garden,
            mote_id=mote.id,
            name=_name(mote.properties, mote.id),
            owns=claimed,
            properties=dict(mote.properties),
        ))
    return sorted(found, key=lambda owner: (owner.owns, owner.name.casefold()))


def owners(gardens: list[str] | None = None) -> dict[str, list[Owner]]:
    """Every claim in every garden, keyed by the garden claimed.

    A key with two or more owners is the one-owner check failing; a key
    that is not in `list_gardens()` is a dangling claim. Both are
    reported by check() rather than raised here, because Home has to draw
    a garden that is in a bad state as much as a good one."""
    claims: dict[str, list[Owner]] = {}
    for garden in gardens if gardens is not None else gardens_repo.list_gardens():
        for owner in _owners_of(garden):
            claims.setdefault(owner.owns, []).append(owner)
    for found in claims.values():
        found.sort(key=lambda owner: (owner.garden, owner.name.casefold()))
    return claims


def owner_of(garden: str, claims: dict[str, list[Owner]] | None = None) -> Owner | None:
    """The sole owner of a garden, or None -- both when nobody claims it
    and when several do. A disputed garden has no unambiguous cover, so
    answering with one of the claimants would be a guess; callers that
    need to say so read check()."""
    found = (owners() if claims is None else claims).get(garden, [])
    return found[0] if len(found) == 1 else None


def _owners_of(garden: str) -> list[Owner]:
    if not gardens_repo.garden_exists(garden):
        return []
    conn = db.get_connection(garden)
    try:
        return owners_in(conn, garden)
    except sqlite3.DatabaseError:
        # A .db that isn't a garden (a half-written copy from a sync tool)
        # claims nothing; Home still lists it, uncounted.
        logger.exception("could not read ownership in %r", garden)
        return []
    finally:
        conn.close()


# --- the one-owner check ---------------------------------------------

def check(gardens: list[str] | None = None) -> list[Problem]:
    """What is wrong with the ownership graph, worst first.

    Three things can be: two motes claiming one garden, a mote claiming a
    garden that is not on disk, and a loop (a garden that owns, through
    some chain, itself). None of them is an error to raise -- a garden
    in any of these states still opens, and the user fixes it by editing
    a property."""
    known = gardens if gardens is not None else gardens_repo.list_gardens()
    claims = owners(known)
    problems: list[Problem] = []

    for garden in sorted(claims):
        found = claims[garden]
        if len(found) > 1:
            where = ", ".join(f"{o.name} (in {o.garden})" for o in found)
            problems.append(Problem(
                kind=DISPUTED, garden=garden, owners=tuple(found),
                message=f"{len(found)} motes claim the garden {garden!r}: {where} "
                        f"-- a garden has one owner, so clear the `{GARDEN_KEY}` "
                        f"property on all but one",
            ))

    for garden in sorted(claims):
        for owner in claims[garden]:
            if not owner.exists:
                problems.append(Problem(
                    kind=MISSING, garden=garden, owners=(owner,),
                    message=f"{owner.name} (in {owner.garden}) claims the garden "
                            f"{garden!r}, which does not exist -- start the project "
                            f"again to create it, or clear the `{GARDEN_KEY}` property",
                ))

    for cycle in _loops(claims, known):
        problems.append(Problem(
            kind=LOOP, garden=cycle[0], owners=tuple(
                owner for garden in cycle for owner in claims.get(garden, [])
                if owner.garden in cycle
            ),
            message="ownership runs in a circle: "
                    + " owns ".join(cycle) + f" owns {cycle[0]} -- one of these "
                    f"`{GARDEN_KEY}` properties has to go, or the tree has no top",
        ))
    return problems


def _loops(claims: dict[str, list[Owner]], known: list[str]) -> list[list[str]]:
    """Every ownership cycle, each reported once and rotated to start at
    its lowest-named garden, so one loop found from two starts is one
    problem.

    The edge is owner-to-owned: a garden points at each garden that a
    mote *inside* it claims. A mote carrying its own garden's name is a
    one-garden loop, and falls out of the same walk. A depth-first walk
    rather than following one edge at a time, because a hub owns several
    gardens and the loop may not be down the first of them."""
    edges = _edges(claims, known)
    ON_STACK, DONE = 1, 2
    state: dict[str, int] = {}
    found: list[list[str]] = []
    already: set[frozenset[str]] = set()

    def walk(at: str, stack: list[str]) -> None:
        state[at] = ON_STACK
        stack.append(at)
        for nxt in sorted(edges.get(at, ())):
            if state.get(nxt) == ON_STACK:
                cycle = _rotated(stack[stack.index(nxt):])
                if frozenset(cycle) not in already:
                    already.add(frozenset(cycle))
                    found.append(cycle)
            elif state.get(nxt) is None:
                walk(nxt, stack)
        stack.pop()
        state[at] = DONE

    for start in sorted(edges):
        if state.get(start) is None:
            walk(start, [])
    return found


def _edges(claims: dict[str, list[Owner]], known: list[str]) -> dict[str, set[str]]:
    edges: dict[str, set[str]] = {garden: set() for garden in known}
    for garden, found in claims.items():
        for owner in found:
            edges.setdefault(owner.garden, set()).add(garden)
        edges.setdefault(garden, set())
    return edges


def _rotated(cycle: list[str]) -> list[str]:
    lowest = min(range(len(cycle)), key=lambda i: cycle[i])
    return cycle[lowest:] + cycle[:lowest]


# --- the tree Home draws ---------------------------------------------

def tree(gardens: list[str] | None = None) -> list[Node]:
    """The ownership forest: every garden, each under its owner's garden.

    Roots are the gardens nobody claims -- a semester hub and a loose
    demo alike, since nothing marks a hub as special. A garden caught in
    a loop has no root to hang from, so it becomes a root of its own
    rather than disappearing from Home; check() says why it looks odd.

    A disputed garden hangs under every claimant, because hiding one of
    the two claims is how a duplicate stays invisible."""
    known = gardens if gardens is not None else gardens_repo.list_gardens()
    claims = owners(known)
    children: dict[str, list[Owner]] = {}
    for garden, found in claims.items():
        for owner in found:
            children.setdefault(owner.garden, []).append(owner)

    owned = {garden for garden, found in claims.items() if found and garden in known}
    roots = [garden for garden in known if garden not in owned]

    marked = _marked_gardens(known)
    nodes = [_node(garden, None, children, known, frozenset(), marked, False)
             for garden in sorted(roots)]
    drawn = {garden for node in nodes for garden in _reached(node)}
    # Whatever the walk never reached is in a loop (see check()).
    nodes += [
        _node(garden, owner_of(garden, claims), children, known, frozenset(), marked,
              any(o.archived for o in claims.get(garden, [])))
        for garden in sorted(set(known) - drawn)
    ]
    return nodes


def _node(
    garden: str,
    owner: Owner | None,
    children: dict[str, list[Owner]],
    known: list[str],
    path: frozenset[str],
    marked: set[str],
    inherited: bool,
) -> Node:
    archived = inherited or garden in marked or (owner is not None and owner.archived)
    beneath = [
        _node(child.owns, child, children, known, path | {garden}, marked, archived)
        for child in sorted(children.get(garden, ()), key=lambda o: o.name.casefold())
        if child.owns in known and child.owns not in path and child.owns != garden
    ]
    return Node(garden=garden, owner=owner, children=tuple(beneath), archived=archived)


def _reached(node: Node) -> set[str]:
    found = {node.garden}
    for child in node.children:
        found |= _reached(child)
    return found


# --- Start project ---------------------------------------------------

def start_project(
    conn: sqlite3.Connection,
    mote_id: str,
    garden: str | None = None,
) -> tuple[str, undo.Command]:
    """Give this mote a garden of its own, creating it if need be.

    One step, because filing a project by hand -- create the garden, then
    remember to link it -- is exactly the separate act of filing that
    `philosophy.md` says nobody does. Returns the garden's name.

    With no name, one is derived from the mote's, and a derived name never
    adopts an existing garden: it steps aside to a free one, because a
    slug collision with somebody's unrelated scratch garden is a
    coincidence, not an intention. A name passed in *does* adopt a loose
    garden, which is how existing work gets a cover.

    Undo clears the property and leaves the garden on disk. A garden is a
    file with its own sync state that another surface may already have
    open, so undo in one workspace is the wrong thing to delete it from;
    the garden is simply loose again, and starting the project a second
    time adopts it back."""
    mote = motes_repo.get_mote(conn, mote_id)
    if mote is None:
        raise LookupError(f"No such mote: {mote_id!r}")

    already = garden_named_by(mote.properties)
    if already is not None:
        raise ValueError(
            f"{_name(mote.properties, mote_id)} already owns the garden "
            f"{already!r} -- a mote is the cover of one project"
        )

    claims = owners()
    if garden is None:
        garden = _free_name(_slug(mote.properties, mote_id), claims)
        created = gardens_repo.create_garden(garden)
    else:
        db.validate_garden_name(garden)
        taken = claims.get(garden, [])
        if taken:
            where = ", ".join(f"{o.name} (in {o.garden})" for o in taken)
            raise ValueError(
                f"The garden {garden!r} is already owned by {where} -- a garden has "
                "one owner"
            )
        created = garden if gardens_repo.garden_exists(garden) else gardens_repo.create_garden(garden)

    # Only the one key changes. A blank or non-string `garden` left by a
    # hand edit is no claim (garden_named_by), but undo still puts it back.
    old = {GARDEN_KEY: mote.properties[GARDEN_KEY]} if GARDEN_KEY in mote.properties else {}
    command = undo.for_update_mote(
        conn, mote_id,
        old_props=old,
        new_props={GARDEN_KEY: created},
        label=f"start project {created}",
    )
    command.redo()
    return created, command


def _slug(properties: dict, mote_id: str) -> str:
    """A garden name from the mote's own name, in the character set
    `validate_garden_name` allows. A name that slugs away to nothing
    (punctuation, or another script) falls back to the id, so a project
    always starts rather than refusing over its title."""
    name = properties.get("name")
    parts = _SLUG_SPLIT_RE.split(name if isinstance(name, str) else "")
    slug = "-".join(part for part in parts if part).lower()[:_MAX_NAME].strip("-")
    return slug or mote_id[:8]


def _free_name(slug: str, claims: dict[str, list[Owner]]) -> str:
    """`slug`, or `slug-2`, `slug-3`, ... until nothing on disk and
    nothing claimed answers to it."""
    def taken(name: str) -> bool:
        return gardens_repo.garden_exists(name) or bool(claims.get(name))

    if not taken(slug):
        return slug
    for n in range(2, 1000):
        suffix = f"-{n}"
        candidate = slug[:_MAX_NAME - len(suffix)] + suffix
        if not taken(candidate):
            return candidate
    raise ValueError(f"Cannot find a free garden name for {slug!r}")


def stop_owning(conn: sqlite3.Connection, mote_id: str) -> tuple[str | None, undo.Command | None]:
    """Drop this mote's claim, leaving the garden loose and on disk.

    The inverse of the property half of start_project, and what a surface
    offers when a cover is on the wrong mote. Deleting the garden is a
    separate, louder act (`gardens.delete_garden`), because the work
    inside it is not the claim."""
    mote = motes_repo.get_mote(conn, mote_id)
    if mote is None:
        raise LookupError(f"No such mote: {mote_id!r}")
    claimed = garden_named_by(mote.properties)
    if claimed is None and GARDEN_KEY not in mote.properties:
        return None, None

    old = mote.properties.get(GARDEN_KEY)
    command = undo.for_delete_property(
        conn, mote_id, GARDEN_KEY, old,
        label=f"stop owning {claimed or old}",
    )
    command.redo()
    return claimed, command



# --- Archive ---------------------------------------------------------

class ArchivedGarden(ValueError):
    """A write was asked of a garden that is archived. Raised by the CLI
    and MCP write paths through `ensure_writable`; the store itself never
    raises it, so sync, undo and un-archiving keep working."""


def is_marked(properties: dict) -> bool:
    """`archived: true`, or the text "true" as a hand edit writes it --
    the same reading `rules.is_check` gives its flag."""
    value = properties.get(ARCHIVED_KEY)
    if isinstance(value, str):
        return value.strip().lower() == "true"
    return value is True


def _markers_in(conn: sqlite3.Connection) -> list[str]:
    """Motes in this garden archiving the garden itself: marked, and
    claiming no garden (a marked owner archives what it owns instead)."""
    cur = conn.cursor()
    cur.execute("SELECT mote_id FROM mote_properties WHERE key = ?", (ARCHIVED_KEY,))
    ids = [row["mote_id"] for row in cur.fetchall()]
    return [
        mote.id for mote in motes_repo.get_motes(conn, ids)
        if is_marked(mote.properties) and garden_named_by(mote.properties) is None
    ]


def _marked_gardens(gardens: list[str]) -> set[str]:
    marked = set()
    for garden in gardens:
        if not gardens_repo.garden_exists(garden):
            continue
        conn = db.get_connection(garden)
        try:
            if _markers_in(conn):
                marked.add(garden)
        except sqlite3.DatabaseError:
            logger.exception("could not read archive markers in %r", garden)
        finally:
            conn.close()
    return marked


def archived_by(garden: str, gardens: list[str] | None = None) -> str | None:
    """Why this garden is archived, as a sentence, or None if it isn't.

    Its own marker, an owner that says so, or anything above it: archiving
    a hub archives the semester beneath it. Cycle-safe, like the tree."""
    known = gardens if gardens is not None else gardens_repo.list_gardens()
    claims = owners(known)
    marked = _marked_gardens(known)
    seen: set[str] = set()
    at = garden
    while at not in seen:
        seen.add(at)
        if at in marked:
            where = "" if at == garden else f", which holds {garden!r}"
            return f"the garden {at!r} is archived by a mote inside it{where}"
        found = claims.get(at, [])
        for owner in found:
            if owner.archived:
                where = "" if at == garden else f" (above {garden!r})"
                return (f"{owner.name} (in {owner.garden}) archives the garden "
                        f"{at!r}{where}")
        if not found:
            return None
        at = found[0].garden
    return None


def is_archived(garden: str) -> bool:
    return archived_by(garden) is not None


def ensure_writable(garden: str) -> None:
    """Refuse a write to an archived garden, saying what archived it and
    how to undo that. For the CLI and MCP write paths; see ArchivedGarden."""
    why = archived_by(garden)
    if why is not None:
        raise ArchivedGarden(
            f"{garden!r} is archived and read-only: {why}. Un-archive it first "
            f"(`monoss project unarchive {garden}`)."
        )


def set_archived(garden: str, archived: bool) -> str:
    """Archive or un-archive one garden and everything beneath it. Returns
    a sentence saying what was written.

    Archiving writes `archived: true` onto the garden's sole owner, in the
    owner's garden; a loose (or disputed) garden gets a marker mote inside
    it instead. Un-archiving clears the flag from its owners and from every
    marker inside it, and refuses if something *above* still archives it --
    the answer then is to un-archive that, and the message names it.

    Written directly, not through `ensure_writable`: un-archiving has to
    write to a garden that is, by definition, archived."""
    if not gardens_repo.garden_exists(garden):
        raise LookupError(f"No such garden: {garden!r}")
    claims = owners()
    found = claims.get(garden, [])

    if archived:
        already = archived_by(garden)
        if already is not None:
            return f"{garden!r} is already archived: {already}"
        if len(found) == 1:
            _write(found[0].garden, found[0].mote_id, {ARCHIVED_KEY: True})
            return f"archived {garden!r} on its owner, {found[0].name} (in {found[0].garden})"
        conn = db.get_connection(garden)
        try:
            motes_repo.create_mote(conn, {"name": ARCHIVE_MARKER_NAME, ARCHIVED_KEY: True})
        finally:
            conn.close()
        return f"archived {garden!r} with a marker mote inside it (it has no single owner)"

    cleared = []
    for owner in found:
        if owner.archived:
            _clear(owner.garden, owner.mote_id)
            cleared.append(f"{owner.name} (in {owner.garden})")
    conn = db.get_connection(garden)
    try:
        for marker in _markers_in(conn):
            mote = motes_repo.get_mote(conn, marker)
            if set(mote.properties) <= {"name", ARCHIVED_KEY} and \
                    mote.properties.get("name") in (None, ARCHIVE_MARKER_NAME):
                motes_repo.delete_mote(conn, marker)  # the marker we made, and nothing else
            else:
                motes_repo.delete_property(conn, marker, ARCHIVED_KEY)
            cleared.append(f"a marker in {garden}")
    finally:
        conn.close()
    still = archived_by(garden)
    if still is not None:
        raise ArchivedGarden(
            f"{garden!r} is still archived from above: {still} -- un-archive that garden")
    if not cleared:
        return f"{garden!r} was not archived"
    return f"un-archived {garden!r} (cleared {', '.join(cleared)})"


def _write(garden: str, mote_id: str, properties: dict) -> None:
    conn = db.get_connection(garden)
    try:
        motes_repo.update_mote(conn, mote_id, properties)
    finally:
        conn.close()


def _clear(garden: str, mote_id: str) -> None:
    conn = db.get_connection(garden)
    try:
        motes_repo.delete_property(conn, mote_id, ARCHIVED_KEY)
    finally:
        conn.close()

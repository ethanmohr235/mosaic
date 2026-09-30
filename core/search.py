# core/search.py
"""Search every garden at once, read-only.

The payoff of many small gardens (`roadmap.md` phase 9) depends on
finding things across them without remembering which garden they are in.
This is the one place a query crosses gardens, and it only reads: each
garden is opened, searched with the same `motes.search_motes` a single
garden uses (so `key = value` and substring queries mean the same thing
everywhere), and closed.

Each hit carries the project it belongs to -- the garden's cover, where it
has one -- because a result is opened *as* its project, not as a loose
mote. Archived gardens are searched too and say so: archiving puts a tree
out of the way, not out of reach."""
from dataclasses import dataclass

from core import db, gardens as gardens_repo, motes as motes_repo, projects


@dataclass(frozen=True)
class Hit:
    garden: str
    mote_id: str
    name: str
    properties: dict
    owner: projects.Owner | None = None
    archived: bool = False

    @property
    def project(self) -> str:
        """What to call the project this hit is in: its cover's name, or
        the garden's own name for a loose garden."""
        return self.owner.name if self.owner is not None else self.garden


@dataclass(frozen=True)
class Results:
    hits: tuple[Hit, ...] = ()
    # Gardens holding more hits than `per_garden` returned; a surface says
    # "and more in hw4" rather than implying it saw everything.
    truncated: tuple[str, ...] = ()


def search_all(
    query: str,
    per_garden: int = 20,
    include_archived: bool = True,
    gardens: list[str] | None = None,
) -> Results:
    """Every garden's matches for `query`, live gardens first, then by
    garden name; within a garden, in `search_motes`'s own order.

    A blank query is refused rather than returning the first twenty motes
    of every garden -- a whole library's worth of noise is not a search."""
    if not (query or "").strip():
        return Results()
    known = gardens if gardens is not None else gardens_repo.list_gardens()
    claims = projects.owners(known)
    archived = _archived(known)

    hits: list[Hit] = []
    truncated: list[str] = []
    for garden in sorted(known, key=lambda g: (g in archived, g)):
        if garden in archived and not include_archived:
            continue
        found = _search(garden, query, per_garden + 1)
        if len(found) > per_garden:
            truncated.append(garden)
            found = found[:per_garden]
        owner = projects.owner_of(garden, claims)
        hits += [
            Hit(garden, mote.id, _name(mote.properties, mote.id), dict(mote.properties),
                owner, garden in archived)
            for mote in found
        ]
    return Results(tuple(hits), tuple(truncated))


def _search(garden: str, query: str, limit: int) -> list:
    if not gardens_repo.garden_exists(garden):
        return []
    conn = db.get_connection(garden)
    try:
        return motes_repo.search_motes(conn, query, limit)
    finally:
        conn.close()


def _archived(gardens: list[str]) -> set[str]:
    """Read off the tree, which already walks archive inheritance once for
    every garden, rather than asking `archived_by` garden by garden."""
    flags: dict[str, bool] = {}

    def walk(node: projects.Node) -> None:
        flags[node.garden] = flags.get(node.garden, False) or node.archived
        for child in node.children:
            walk(child)

    for root in projects.tree(gardens):
        walk(root)
    return {garden for garden, archived in flags.items() if archived}


def _name(properties: dict, mote_id: str) -> str:
    name = properties.get("name")
    return name if isinstance(name, str) and name else mote_id[:8]

# core/graphview.py
"""The graph pane's grammar and answer (docs/console.md -> Settled for U8).

    graph [@sel|#id|‹name›] [--depth N] [--hide-rel a,b] [--view ‹saved›] [‹find conditions›]

With a root, the pane draws that mote's neighbourhood: every mote within
`--depth` hops (default 2), relationships counted in either direction.
With none, the whole garden. Find conditions and a saved view (`--view`,
as `find` reads it) keep the motes they match; the root always stays.
`--hide-rel` drops relationships of those types -- from the drawing and
from the hop count, so hiding `instance_of` stops a species pulling in
every instance it has.

A pane is its command, so `[` and `]` rewrite `--depth` (rewrite()), and
a layout reopens on the same neighbourhood. Edits are never answered
here: a gesture in the pane puts a statement on the command line."""
import shlex
from dataclasses import dataclass, replace
from typing import Sequence

from core import findtext
from core import motes as motes_repo
from core import query
from core import species as species_repo
from core import table as table_repo
from core import views as views_repo
from core.command_bar import CommandError
from core.entities import Mote, Relationship

VERB = "graph"
DEPTH_FLAG, HIDE_FLAG, VIEW_FLAG = "--depth", "--hide-rel", findtext.VIEW_FLAG
FLAGS = (DEPTH_FLAG, HIDE_FLAG, VIEW_FLAG)
DEFAULT_DEPTH = 2
SEL = "@sel"


# ---- the grammar ----------------------------------------------------------

@dataclass(frozen=True)
class GraphLine:
    root: str | None = None          # as typed: @sel, #id or a name
    depth: int | None = None         # None: the default, when there is a root
    hide: tuple[str, ...] = ()
    view: str | None = None
    conditions: tuple[str, ...] = ()

    def text(self) -> str:
        words = [VERB]
        if self.root:
            words.append(self.root)
        if self.depth is not None:
            words += [DEPTH_FLAG, str(self.depth)]
        if self.hide:
            words += [HIDE_FLAG, ",".join(self.hide)]
        if self.view:
            words += [VIEW_FLAG, self.view]
        return " ".join(_quote(w) for w in words + list(self.conditions))

    @property
    def hops(self) -> int | None:
        if not self.root:
            return None
        return self.depth if self.depth is not None else DEFAULT_DEPTH


def _quote(word: str) -> str:
    # `@sel` and `#id` read the same unquoted; shlex.quote would wrap `#…`.
    return word if word.startswith(("@", "#")) and " " not in word else shlex.quote(word)


def parse(args: Sequence[str]) -> GraphLine:
    fields: dict = {}
    conditions: list[str] = []
    root = None
    i = 0
    while i < len(args):
        word = args[i]
        if word.startswith("--"):
            if word not in FLAGS:
                raise CommandError(f"graph takes {', '.join(FLAGS)} -- not {word}")
            if i + 1 >= len(args):
                raise CommandError(f"{word} needs a value")
            value = args[i + 1]
            i += 2
            if word == DEPTH_FLAG:
                fields["depth"] = _depth(value)
            elif word == HIDE_FLAG:
                fields["hide"] = tuple(t for t in (p.strip() for p in value.split(",")) if t)
            else:
                fields["view"] = value
            continue
        i += 1
        if word == findtext.JOIN_WORD or _is_condition(word):
            conditions.append(word)
        elif root is None:
            root = word
        else:
            raise CommandError(f"graph has one root -- {root!r}, then {word!r} is not a condition")
    if "depth" in fields and root is None:
        raise CommandError("--depth needs a root: graph @sel --depth 2")
    if conditions:
        findtext.parse_find(shlex.join(conditions))  # raises FindSyntaxError naming the word
    return GraphLine(root, conditions=tuple(conditions), **fields)


def parse_text(command: str) -> GraphLine:
    try:
        words = shlex.split(command or "")
    except ValueError as e:
        raise CommandError(f"graph: {e}") from None
    if not words or words[0] != VERB:
        raise CommandError("not a graph line")
    return parse(words[1:])


def _is_condition(word: str) -> bool:
    """A word with a condition's operator is one, so a malformed condition
    is refused naming it rather than read as a root. A name with `:` in it
    is a root by its #id."""
    return ":" in word or "~" in word


def _depth(text: str) -> int:
    try:
        depth = int(text)
    except ValueError:
        raise CommandError(f"--depth takes a whole number of hops, not {text!r}") from None
    if depth < 1:
        raise CommandError("--depth is at least 1")
    return depth


def rewrite(line: GraphLine, change: dict) -> GraphLine:
    """A pane gesture as a line: `depth` (+1/-1) widens or narrows the
    neighbourhood, `root` re-roots it (None: the whole garden)."""
    if "root" in change:
        root = change["root"]
        return replace(line, root=root or None, depth=line.depth if root else None)
    if "depth" in change:
        if line.hops is None:
            raise CommandError("the whole garden has no depth -- give the graph a root")
        return replace(line, depth=max(1, line.hops + int(change["depth"])))
    raise CommandError(f"no such change: {sorted(change)}")


# ---- the answer -----------------------------------------------------------

def root_id(conn, ref: str, selection=()) -> str:
    """The mote `ref` names: @sel (the primary), #id or a unique name."""
    if ref == SEL:
        if not selection:
            raise CommandError("@sel is empty -- select a mote first")
        ref = f"#{selection[0]}"
    if ref.startswith("#"):
        if motes_repo.get_mote(conn, ref[1:]) is None:
            raise CommandError(f"no mote {ref}")
        return ref[1:]
    named = [m.id for m in motes_repo.list_motes(conn) if m.properties.get("name") == ref]
    if not named:
        raise CommandError(f"no mote named {ref!r}")
    if len(named) > 1:
        raise CommandError(f"{len(named)} motes are named {ref!r} -- use #id")
    return named[0]


def run(conn, line: GraphLine, selection=()) -> dict:
    motes, relationships, index = table_repo.read(conn)
    problems: list[str] = []
    every_type = sorted({r.type for r in relationships})
    for t in line.hide:
        if t not in every_type:
            problems.append(f"no relationship has type {t!r}")
    rels = [r for r in relationships if r.type not in line.hide]
    root = root_id(conn, line.root, selection) if line.root else None

    shown = _reach(root, line.hops, rels) if root else {m.id for m in motes}
    kept = [m for m in motes if m.id in shown]
    kept = _filtered(conn, line, kept, motes, relationships, index, problems, keep=root)
    ids = {m.id for m in kept}
    edges = [r for r in rels if r.from_id in ids and r.to_id in ids]

    return {
        "command": line.text(),
        "root": root,
        "rootName": index.name_of(root) if root else None,
        "depth": line.hops,
        "motes": [_node(index, m) for m in kept],
        "edges": [{"id": r.id, "from": r.from_id, "to": r.to_id, "type": r.type} for r in edges],
        "shown": len(kept),
        "total": len(motes),
        "relTypes": [{"type": t, "hidden": t in line.hide} for t in every_type],
        "problems": problems,
        "speciesIds": sorted(m.id for m in species_repo.list_species(conn)),
    }


def _reach(root: str, hops: int, rels: list[Relationship]) -> set[str]:
    """Every mote within `hops` of `root`, edges read both ways (as
    core/graph.py::neighborhood, over the relationships still shown)."""
    adjacency: dict[str, set[str]] = {}
    for r in rels:
        adjacency.setdefault(r.from_id, set()).add(r.to_id)
        adjacency.setdefault(r.to_id, set()).add(r.from_id)
    seen, frontier = {root}, {root}
    for _ in range(hops):
        frontier = {n for m in frontier for n in adjacency.get(m, ())} - seen
        if not frontier:
            break
        seen |= frontier
    return seen


def _filtered(conn, line, kept, motes, relationships, index, problems, keep) -> list[Mote]:
    if not line.conditions and not line.view:
        return kept
    spec = findtext.parse_find(shlex.join(line.conditions))
    the_query = spec.query
    if line.view:
        saved = views_repo.find_view_by_name(conn, line.view)
        if saved is None:
            raise CommandError(f"no saved view named {line.view!r}")
        the_query = findtext.with_view(spec, views_repo.query_of(saved))
    schema = table_repo.Schema.of(index, motes, relationships)
    resolved, found = table_repo.resolve_query(the_query, schema)
    problems.extend(found)
    matched = {m.id for m in query.evaluate(index, kept, resolved)}
    return [m for m in kept if m.id in matched or m.id == keep]


def _node(index, mote: Mote) -> dict:
    described = table_repo.describe(index, mote.id)
    return {"id": mote.id, "name": described["name"], "speciesId": described["speciesId"],
            "speciesName": described["speciesName"], "kinds": described["kinds"]}


# ---- completion -----------------------------------------------------------

def choices(conn, rest: str) -> list[tuple[str, str]]:
    """What the command line offers after `graph `: a flag's values after
    the flag, the flags after `-`, else a root (`@sel`, then names)."""
    from core import relationships as rel_repo
    words = rest.split()
    partial = "" if not rest or rest[-1].isspace() else words[-1]
    before = words[-2] if partial and len(words) > 1 else (words[-1] if words and not partial else None)
    if before == HIDE_FLAG:
        return [(t, "relationship type") for t in sorted({r.type for r in rel_repo.list_relationships(conn)})]
    if before == VIEW_FLAG:
        return [(shlex.quote(views_repo.name_of(v)), "saved view") for v in views_repo.list_views(conn)]
    if before == DEPTH_FLAG:
        return [(str(n), f"{n} hop{'s' if n > 1 else ''}") for n in (1, 2, 3, 4)]
    if partial.startswith("-"):
        return [(DEPTH_FLAG, "hops from the root"), (HIDE_FLAG, "leave out relationship types"),
                (VIEW_FLAG, "start from a saved view")]
    names = sorted({m.properties["name"] for m in motes_repo.list_motes(conn)
                    if isinstance(m.properties.get("name"), str)})
    return [(SEL, "the selection")] + [(_quote(n), "root") for n in names]

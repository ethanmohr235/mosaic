# core/flowview.py
"""`graph … --flow`: the garden's solve drawn left to right
(docs/console.md -> Settled for graph --flow).

    graph [@sel|#id|‹name›|‹m.k›] --flow [--depth N] [‹find conditions›]

The picture is the solve itself, read off its journal
(core/simulation.py::trace), not off the relationships. Every step the
solve took -- one equation that determined one value, or a group the
solver found together -- is a **group**, ranked one column right of the
deepest step it read from. Givens stand in the first column and answers
(values nothing else reads) in the last. A mote whose properties fall in
several groups appears as a stub card in each.

`core/incidence.py::blocks` is not used for the grouping: it joins every
value an equation *could* determine, so a chain `I -> S -> sigma` would
be one block. The journal says what the solve actually did.

Folding is computed here, not drawn over: a folded sub-calculation is
contracted into one group before ranking, so folding it really narrows
the picture. The pane sends its fold state with every run (`folds`,
{id: bool} over the defaults); it is view state, like the cursor, and
not part of the line.

Nothing here writes. A gesture in the pane puts a statement on the
command line."""
import hashlib
from collections import defaultdict
from dataclasses import dataclass, field, replace

from core import calc
from core import formula_tex
from core import graphview
from core import health as health_repo
from core import motion
from core import scenarios as scenarios_repo
from core import simulation
from core import species as species_repo
from core import table as table_repo
from core import uncertainty
from core.command_bar import CommandError
from core.formula import Unknown
from core.plotcmd import _Garden
from core.provenance import SOLVER

FLAG = "--flow"
BLOCK_FOLD = 4   # a group of this many equations or more starts folded
CONE_STEPS = 2   # a sub-calculation of this many steps or more can fold sideways
SWEEPS = 4       # barycenter passes over the columns

STEP, CHECK, FAULT = "step", "check", "fault"
GIVENS, ANSWERS = "in", "out"
_WORST = {simulation.FAIL: 2, simulation.NOT_CHECKED: 1, simulation.PASS: 0}

Key = tuple[str, str]  # (rule mote id, anchor mote id): one equation instance


@dataclass
class Group:
    id: str
    kind: str                       # step | check | fault
    equations: tuple[Key, ...]
    values: frozenset[Unknown]      # what it determined (none for a check)
    inputs: frozenset[Unknown]      # what it read
    method: str = ""                # substitution | solver, for a step
    fold: dict | None = None        # set when the group is drawn folded
    rank: int = 0
    members: tuple[str, ...] = ()   # the steps a folded sub-calculation holds


# ---- groups from the journal ------------------------------------------------

def _inputs(trace: simulation.Trace, keys, values) -> frozenset[Unknown]:
    return frozenset(v for k in keys for v in trace.touches[k].touches) - values


def build_groups(trace: simulation.Trace) -> list[Group]:
    """One step per derivation; every relation the solve did not use is a
    check (a limit, a `?=`, an equation already satisfied), or -- when it
    still holds a value the solve could not find -- part of a fault."""
    groups, used = [], set()
    for d in trace.journal.derivations:
        keys = tuple(dict.fromkeys((o.rule_mote_id, o.anchor_mote_id) for o in d.equations))
        keys = tuple(k for k in keys if k in trace.touches)
        used |= set(keys)
        values = frozenset(d.values)
        groups.append(Group(f"s{d.block}", STEP, keys, values, _inputs(trace, keys, values), d.method))
    unsolved = {v for t in trace.touches.values() for v in t.determines} - set(trace.values)
    leftover = [k for k in trace.touches if k not in used]
    faulty = [k for k in leftover if trace.touches[k].determines & unsolved]
    groups += _faults(trace, faulty, unsolved)
    for i, k in enumerate(k for k in leftover if k not in faulty):
        groups.append(Group(f"c{i}", CHECK, (k,), frozenset(), _inputs(trace, (k,), frozenset())))
    return groups


def _faults(trace: simulation.Trace, keys: list[Key], unsolved: set[Unknown]) -> list[Group]:
    """Equations left holding unsolved values, one group per set that
    shares them -- the shape health.py reports a shortfall in."""
    parent: dict[Unknown, Unknown] = {}

    def root(v: Unknown) -> Unknown:
        while parent.setdefault(v, v) != v:
            v = parent[v]
        return v

    for k in keys:
        vs = sorted(trace.touches[k].determines & unsolved)
        for v in vs[1:]:
            parent[root(v)] = root(vs[0])
    members: dict[Unknown, list[Key]] = defaultdict(list)
    for k in keys:
        members[root(min(trace.touches[k].determines & unsolved))].append(k)
    out = []
    for i, (r, ks) in enumerate(sorted(members.items())):
        values = frozenset(v for k in ks for v in trace.touches[k].determines & unsolved)
        out.append(Group(f"f{i}", FAULT, tuple(ks), values, _inputs(trace, ks, values)))
    return out


def producers(groups: list[Group]) -> dict[Unknown, Group]:
    return {v: g for g in groups for v in g.values}


def readers(groups: list[Group]) -> dict[Unknown, set[str]]:
    out: dict[Unknown, set[str]] = defaultdict(set)
    for g in groups:
        for v in g.inputs:
            out[v].add(g.id)
    return out


def answers(groups: list[Group]) -> set[Unknown]:
    """Values the solve found that no other step reads. A check reading
    one does not make it an intermediate: a stress checked against its
    limit is still the answer."""
    read = {v for g in groups if g.kind != CHECK for v in g.inputs}
    return {v for g in groups if g.kind == STEP for v in g.values} - read


def rank(groups: list[Group]) -> None:
    """One column right of the deepest step read from; givens are 0."""
    made = producers(groups)
    by_id = {g.id: g for g in groups}
    done: dict[str, int] = {}

    def depth(g: Group) -> int:
        if g.id not in done:
            done[g.id] = 0  # a cycle (never from a journal) stops here
            done[g.id] = 1 + max((depth(made[v]) for v in g.inputs
                                  if v in made and made[v].id != g.id), default=0)
        return done[g.id]

    for g in groups:
        by_id[g.id].rank = depth(g)


# ---- folds ------------------------------------------------------------------

def _sig(prefix: str, values) -> str:
    text = ",".join(sorted(f"{m}.{k}" for m, k in values))
    return f"{prefix}:{hashlib.sha1(text.encode()).hexdigest()[:10]}"


def cones(groups: list[Group], ends: set[Unknown]) -> dict[str, frozenset[str]]:
    """Each step's private upstream: the steps whose every result is read
    only inside the set, so the set has one way out -- the step's own
    values. Two steps or more can fold sideways into one card."""
    steps = {g.id: g for g in groups if g.kind == STEP}
    made = producers(list(steps.values()))
    read = readers(groups)
    users = {i: set().union(*(read.get(v, set()) for v in g.values)) for i, g in steps.items()}
    out = {}
    for b in steps:
        members = {b}
        grew = True
        while grew:
            near = {made[v].id for m in members for v in steps[m].inputs if v in made} - members
            joining = {i for i in near if users[i] and users[i] <= members and not steps[i].values & ends}
            members |= joining
            grew = bool(joining)
        if len(members) >= CONE_STEPS:
            out[b] = frozenset(members)
    return out


@dataclass(frozen=True)
class Fold:
    id: str
    kind: str                   # block | cone
    owner: str                  # the group it is named for
    members: frozenset[str]
    folded: bool


def folds_of(groups: list[Group], ends: set[Unknown], state: dict[str, bool]) -> list[Fold]:
    by_id = {g.id: g for g in groups}
    out = [Fold(_sig("block", g.values), "block", g.id, frozenset({g.id}),
                state.get(_sig("block", g.values), True))
           for g in groups if g.kind == STEP and len(g.equations) >= BLOCK_FOLD]
    for owner, members in cones(groups, ends).items():
        fid = _sig("cone", by_id[owner].values)
        out.append(Fold(fid, "cone", owner, members, state.get(fid, False)))
    return out


def contract(groups: list[Group], folds: list[Fold]) -> list[Group]:
    """Folded sub-calculations become one step each, largest first; a
    fold inside one already contracted is part of it."""
    by_id = {g.id: g for g in groups}
    taken: set[str] = set()
    merged: list[Group] = []
    for fold in sorted((f for f in folds if f.kind == "cone" and f.folded),
                       key=lambda f: (-len(f.members), f.id)):
        if fold.members & taken:
            continue
        taken |= fold.members
        parts = [by_id[i] for i in sorted(fold.members)]
        made = frozenset(v for p in parts for v in p.values)
        owner = by_id[fold.owner]
        merged.append(Group(
            fold.id, STEP, tuple(k for p in parts for k in p.equations), owner.values,
            frozenset(v for p in parts for v in p.inputs) - made, owner.method,
            fold={"id": fold.id, "kind": "cone", "steps": len(parts)},
            members=tuple(sorted(fold.members))))
    blocks = {f.owner: f for f in folds if f.kind == "block" and f.folded}
    kept = [replace(g, fold={"id": blocks[g.id].id, "kind": "block", "steps": 1})
            if g.id in blocks else g for g in groups if g.id not in taken]
    return kept + merged


# ---- the root, depth and conditions -----------------------------------------

def reach(groups: list[Group], seeds: set[str], hops: int) -> set[str]:
    """Seeds plus `hops` steps upstream and `hops` downstream."""
    made = producers(groups)
    read = readers(groups)
    up = {g.id: {made[v].id for v in g.inputs if v in made} for g in groups}
    down = {g.id: set().union(*(read.get(v, set()) for v in g.values)) for g in groups}
    kept = set(seeds)
    for edges in (up, down):
        frontier = set(seeds)
        for _ in range(hops):
            frontier = {n for i in frontier for n in edges[i]} - kept
            kept |= frontier
    return kept


def _touched(groups: list[Group]) -> set[Unknown]:
    return {v for g in groups for v in g.values | g.inputs}


def root_variables(conn, garden: _Garden, ref: str, selection, touched) -> tuple[set[Unknown], str]:
    """A mote's solve values, or one value (`bd.l`, `#id.l`)."""
    try:
        mote_id = graphview.root_id(conn, ref, selection)
    except CommandError:
        if "." not in ref:
            raise
        variable = garden.ref(ref)
        return {variable}, garden.text_of(*variable)
    return {v for v in touched if v[0] == mote_id}, garden.text_of(mote_id, "")[:-1]


def _matched(conn, line: graphview.GraphLine) -> set[str]:
    motes, relationships, index = table_repo.read(conn)
    problems: list[str] = []
    found = graphview._filtered(conn, line, motes, motes, relationships, index, problems, keep=None)
    return {m.id for m in found}


# ---- the run ----------------------------------------------------------------

@dataclass
class _Run:
    """Everything one answer reads, gathered once."""
    conn: object
    garden: _Garden
    index: table_repo.Index
    trace: simulation.Trace
    scenario: str | None
    groups: list[Group]
    ends: set[Unknown]
    folds: list[Fold]
    series: dict[Unknown, list] | None = None
    sigma: dict[Unknown, float] = field(default_factory=dict)
    culprits: dict[Unknown, str] = field(default_factory=dict)
    health: object = None


def run(conn, line: graphview.GraphLine, selection=(), run: dict | None = None,
        folds: dict | None = None) -> dict:
    settings = run or {}
    garden = _Garden(conn)
    scenario = garden.scenario(settings.get("scenario"))
    trace = simulation.trace(conn, scenario, settings.get("solver") or "linear")
    raw = build_groups(trace)
    ends = answers(raw)
    fold_list = folds_of(raw, ends, {str(k): bool(v) for k, v in (folds or {}).items()})
    groups = contract(raw, fold_list)
    rank(groups)
    r = _Run(conn, garden, table_repo.read(conn)[2], trace, scenario, groups, ends, fold_list)
    root_name, kept = _keep(r, line, selection)
    shown = [g for g in groups if g.id in kept]
    _extras(r, shown, settings)
    return _Payload(r, shown).build(line, root_name)


def _keep(r: _Run, line, selection) -> tuple[str | None, set[str]]:
    kept = {g.id for g in r.groups}
    name = None
    if line.root:
        variables, name = root_variables(r.conn, r.garden, line.root, selection, _touched(r.groups))
        seeds = {g.id for g in r.groups if (g.values | g.inputs) & variables}
        if not seeds:
            raise CommandError(f"{name} takes no part in the solve")
        kept = reach(r.groups, seeds, line.hops)
    if line.conditions or line.view:
        matched = _matched(r.conn, line)
        kept = {g.id for g in r.groups if g.id in kept
                and any(m in matched for m, _k in g.values | g.inputs)}
    return name, kept


def _extras(r: _Run, shown: list[Group], settings: dict) -> None:
    """What only some runs need: a playhead's series, ± and health."""
    variables = _touched(shown)
    if settings.get("ticks"):
        result = motion._series_run(r.conn, int(settings["ticks"]),
                                    settings.get("solver") or "linear", r.scenario)
        case = scenarios_repo.get_scenario(r.conn, r.scenario) if r.scenario else None
        start = scenarios_repo.apply({m.id: dict(m.properties) for m in r.garden.motes}, case)
        r.series = motion._values(start, result.steps, variables)
    if r.scenario is None and uncertainty.has_uncertainty(r.conn):
        report = uncertainty.propagate(r.conn)
        r.sigma = {(p.mote_id, p.key): p.sigma for p in report.results}
    if r.trace.error or any(g.kind == FAULT for g in r.groups):
        r.health = health_repo.check(r.conn, r.scenario)
        r.culprits = {c.variable: c.reason for c in r.health.culprits}


# ---- the payload ------------------------------------------------------------

class _Payload:
    """Columns, groups, cards, stubs, relays and segments for one run.

    Ports: `r:‹card›` is a rule card (or a folded group's card), `v:‹stub›:‹key›`
    a row, `y:‹column›:‹n›` a relay -- a wire passing through a column
    it does not stop in, one per value and column, which is the shared
    trunk every reader further right leaves from."""

    def __init__(self, r: _Run, shown: list[Group]):
        self.r = r
        self.shown = shown
        self.made = producers(shown)
        self.anywhere = producers(r.groups)
        ranks = sorted({g.rank for g in shown})
        self.column = {g.id: ranks.index(g.rank) + 1 for g in shown}
        self.last = len(ranks) + 1
        self.stubs: dict[str, dict] = {}
        self.group_stubs: dict[str, list[str]] = defaultdict(list)
        self.group_cards: dict[str, list[str]] = {}
        self.card_group = {c: g.id for g in shown for c in self.card_ids(g)}
        self.relays: dict[tuple[Unknown, int], str] = {}
        self.segments: dict[tuple[str, str], Unknown] = {}
        self.instances: dict[str, int] = defaultdict(int)
        for rule, _m in r.trace.pairs:
            self.instances[rule.mote.id] += 1

    # -- stubs and rows --

    def stub(self, stub_id: str, mote_id: str, keys) -> None:
        stub = self.stubs.setdefault(stub_id, {"id": stub_id, "moteId": mote_id, "keys": []})
        for k in keys:
            if k not in stub["keys"]:
                stub["keys"].append(k)

    def row_port(self, stub_id: str, key: str) -> str:
        return f"v:{stub_id}:{key}"

    def place_values(self) -> None:
        for g in self.shown:
            for m, k in sorted(g.values - self.r.ends):
                self.stub(f"{g.id}/{m}", m, [k])
                if f"{g.id}/{m}" not in self.group_stubs[g.id]:
                    self.group_stubs[g.id].append(f"{g.id}/{m}")
        for g in self.shown:
            for m, k in sorted(g.values & self.r.ends):
                self.stub(f"{ANSWERS}/{m}", m, [k])
        for v in sorted({v for g in self.shown for v in g.inputs if v not in self.made}):
            self.stub(f"{GIVENS}/{v[0]}", v[0], [v[1]])

    # -- cards --

    def card_ids(self, g: Group, variable: Unknown | None = None) -> list[str]:
        """The cards of `g` a wire for `variable` meets: its folded card,
        or every equation touching it (all of them for None)."""
        if g.fold:
            return [f"fold:{g.id}"]
        touches = self.r.trace.touches
        return [_card_id(k) for k in g.equations
                if variable is None or variable in touches[k].touches]

    # -- wiring --

    def source(self, v: Unknown) -> tuple[str, int]:
        """Where a wire carrying `v` starts, and its column."""
        g = self.made.get(v)
        if g is None:
            return self.row_port(f"{GIVENS}/{v[0]}", v[1]), 0
        if v in self.r.ends:
            return f"r:{self.card_ids(g, v)[0]}", self.column[g.id]
        return self.row_port(f"{g.id}/{v[0]}", v[1]), self.column[g.id]

    def wire(self, v: Unknown, start: tuple[str, int], end: str, column: int) -> None:
        port, at = start
        for c in range(at + 1, column):
            relay = self.relays.setdefault((v, c), f"y:{c}:{len(self.relays)}")
            self.segments.setdefault((port, relay), v)
            port = relay
        self.segments.setdefault((port, end), v)

    def wire_all(self) -> None:
        for g in self.shown:
            for v in sorted(g.inputs):
                for card in self.card_ids(g, v):
                    self.wire(v, self.source(v), f"r:{card}", self.column[g.id])
            for v in sorted(g.values):
                for card in self.card_ids(g, v):
                    if v in self.r.ends:
                        end = self.row_port(f"{ANSWERS}/{v[0]}", v[1])
                        self.wire(v, (f"r:{card}", self.column[g.id]), end, self.last)
                    else:
                        self.segments.setdefault((f"r:{card}", self.row_port(f"{g.id}/{v[0]}", v[1])), v)

    # -- order --

    def item_of(self, port: str) -> str:
        kind, _, rest = port.partition(":")
        if kind == "y":
            return port
        if kind == "r":
            return self.card_group[rest]
        return rest.rpartition(":")[0].split("/")[0]  # the stub's group

    def columns(self) -> list[list[str]]:
        cols: list[list[str]] = [[GIVENS]] + [[] for _ in range(self.last - 1)] + [[ANSWERS]]
        for g in sorted(self.shown, key=lambda g: (self.column[g.id], _order_key(g, self))):
            cols[self.column[g.id]].append(g.id)
        for (v, c), relay in sorted(self.relays.items(), key=lambda kv: kv[1]):
            cols[c].append(relay)
        return _barycenter(cols, self.item_edges())

    def item_edges(self) -> set[tuple[str, str]]:
        edges = set()
        for a, b in self.segments:
            ia, ib = self.item_of(a), self.item_of(b)
            if ia != ib:
                edges.add((ia, ib))
        return edges

    # -- the answer --

    def build(self, line, root_name: str | None) -> dict:
        self.place_values()
        self.wire_all()
        cols = self.columns()
        position = {item: (c, i) for c, items in enumerate(cols) for i, item in enumerate(items)}
        self.order_insides(position)
        trace = self.r.trace
        return {
            "command": line.text(), "flow": True,
            "root": line.root, "rootName": root_name, "depth": line.hops,
            "scenario": self.r.scenario,
            "solve": _solve_state(trace, self.r.groups),
            "columns": [{"kind": "givens" if c == 0 else "answers" if c == self.last else "step",
                         "items": items} for c, items in enumerate(cols)],
            "groups": {g["id"]: g for g in self.group_payloads()},
            "cards": self.card_payloads(),
            "stubs": self.stub_payloads(),
            "relays": {relay: {"ref": self.r.garden.text_of(*v)} for (v, _c), relay in self.relays.items()},
            "segments": [{"from": a, "to": b, "ref": self.r.garden.text_of(*v)}
                         for (a, b), v in self.segments.items()],
            "folds": [{"id": f.id, "kind": f.kind, "folded": f.folded, "steps": len(f.members)}
                      for f in self.r.folds],
            "summary": self.summary(),
            "speciesIds": sorted(m.id for m in species_repo.list_species(self.r.conn)),
            "problems": [],
        }

    def order_insides(self, position: dict) -> None:
        """Cards by where their inputs come from; stubs and rows by the
        cards that feed them (or, for givens, by what they feed)."""
        into: dict[str, list[str]] = defaultdict(list)
        out_of: dict[str, list[str]] = defaultdict(list)
        for a, b in self.segments:
            into[b].append(a)
            out_of[a].append(b)
        spot = lambda ports: _mean([position.get(self.item_of(p), (0, 0))[1] for p in ports], 0.0)
        card_rank: dict[str, int] = {}
        for g in self.shown:
            ids = sorted(self.card_ids(g), key=lambda c: (spot(into[f"r:{c}"]), c))
            self.group_cards[g.id] = ids
            card_rank.update({c: i for i, c in enumerate(ids)})

        def weight(stub_id: str, key: str) -> float:
            port = self.row_port(stub_id, key)
            if stub_id.startswith(f"{GIVENS}/"):
                return spot(out_of[port])
            ranks = [card_rank[p[2:]] for p in into[port] if p[2:] in card_rank]
            return _mean(ranks, spot(into[port]))

        for stub in self.stubs.values():
            stub["keys"].sort(key=lambda k: (weight(stub["id"], k), k))
            stub["weight"] = weight(stub["id"], stub["keys"][0]) if stub["keys"] else 0.0

    def group_payloads(self) -> list[dict]:
        out = [self.edge_group(GIVENS, "givens"), self.edge_group(ANSWERS, "answers")]
        fold_ids = self.fold_candidates()
        for g in self.shown:
            stubs = sorted(self.group_stubs[g.id], key=lambda s: (self.stubs[s]["weight"], s))
            out.append({
                "id": g.id, "kind": g.kind, "column": self.column[g.id],
                "coupled": g.method == SOLVER and len(g.equations) > 1 and not g.fold,
                "equations": len(g.equations), "unknowns": len(g.values),
                "cards": self.group_cards[g.id], "stubs": stubs,
                "fold": self.fold_payload(g), "folds": fold_ids.get(g.id, []),
                "health": self.health_text(g), "level": self.group_level(g),
            })
        return out

    def edge_group(self, gid: str, kind: str) -> dict:
        stubs = sorted((s for s in self.stubs if s.startswith(f"{gid}/")),
                       key=lambda s: (self.stubs[s]["weight"], s))
        column = 0 if gid == GIVENS else self.last
        return {"id": gid, "kind": kind, "column": column, "coupled": False, "equations": 0,
                "unknowns": 0, "cards": [], "stubs": stubs, "fold": None, "folds": [],
                "health": None, "level": "ok"}

    def fold_candidates(self) -> dict[str, list[str]]:
        """Which fold `z` acts on from each group: its own (folded, or a
        block), the sub-calculation it ends, else the smallest it is in."""
        out: dict[str, list[str]] = {}
        for g in self.shown:
            if g.fold:
                out[g.id] = [g.fold["id"]]
                continue
            own = [f.id for f in self.r.folds if f.owner == g.id]
            inside = sorted((f for f in self.r.folds if f.kind == "cone" and g.id in f.members
                             and f.owner != g.id), key=lambda f: len(f.members))
            out[g.id] = own + [f.id for f in inside[:1]]
        return out

    def fold_payload(self, g: Group) -> dict | None:
        if not g.fold:
            return None
        refs = ", ".join(self.r.garden.text_of(*v) for v in sorted(g.values))
        if g.fold["kind"] == "cone":
            title = f"{refs} ← {g.fold['steps']} steps"
        else:
            title = f"block · {len(g.equations)} eq · {len(g.values)} unknowns"
        return {**g.fold, "title": title, "equations": len(g.equations), "card": f"fold:{g.id}"}

    def health_text(self, g: Group) -> str | None:
        if g.kind != FAULT or self.r.health is None:
            return None
        labels = {_health_label(v, self.r.garden) for v in g.values}
        for found in self.r.health.groups:
            if labels & set(found.labels):
                return found.message
        return f"could not determine {', '.join(sorted(labels))}"

    def group_level(self, g: Group) -> str:
        if g.kind == FAULT:
            return "fault"
        statuses = [self.r.trace.results[k].status for k in g.equations if k in self.r.trace.results]
        if g.kind == CHECK and simulation.FAIL in statuses:
            return "fault"
        if g.kind == CHECK and simulation.NOT_CHECKED in statuses:
            return "warn"
        return "ok"

    def card_payloads(self) -> dict[str, dict]:
        out = {}
        for g in self.shown:
            if g.fold:
                fold = self.fold_payload(g)
                out[fold["card"]] = {"id": fold["card"], "fold": True, "group": g.id,
                                     "title": fold["title"], "kind": fold["kind"]}
                continue
            for k in g.equations:
                out[_card_id(k)] = self.card(g, k)
        return out

    def card(self, g: Group, key: Key) -> dict:
        rule = next(r for r, m in self.r.trace.pairs if (r.mote.id, m) == key)
        result = self.r.trace.results.get(key)
        shows = g.kind == CHECK or g.method == SOLVER
        anchor = key[1] if self.instances[key[0]] > 1 else None
        return {
            "id": _card_id(key), "fold": False, "group": g.id,
            "ruleMoteId": key[0], "name": rule.name, "anchorId": anchor,
            "anchor": self.r.garden.text_of(anchor, "")[:-1] if anchor else None,
            "equation": rule.equation, "tex": formula_tex.to_tex(rule.equation, rule.is_check),
            "check": rule.is_check, "constraint": g.kind == CHECK,
            "status": result.status if result and shows else None,
            "offBy": result.off_by if result and shows else None,
            "detail": result.detail if result and shows else "",
        }

    def stub_payloads(self) -> dict[str, dict]:
        siblings: dict[str, list[str]] = defaultdict(list)
        for s in self.stubs.values():
            siblings[s["moteId"]].append(s["id"])
        out = {}
        for s in self.stubs.values():
            mote = self.r.garden.by_id.get(s["moteId"])
            shown = set(s["keys"])
            more = sum(1 for k, v in (mote.properties if mote else {}).items()
                       if k not in shown and isinstance(v, (int, float)) and not isinstance(v, bool))
            out[s["id"]] = {
                "id": s["id"], "moteId": s["moteId"],
                "name": self.r.garden.text_of(s["moteId"], "")[:-1],
                "speciesId": table_repo.describe(self.r.index, s["moteId"])["speciesId"],
                "siblings": [x for x in siblings[s["moteId"]] if x != s["id"]],
                "rows": [self.row(s["moteId"], k) for k in s["keys"]], "more": more,
            }
        return out

    def row(self, mote_id: str, key: str) -> dict:
        r, v = self.r, (mote_id, key)
        props = r.trace.view.properties.get(mote_id, {})
        solved = r.trace.values.get(v)
        value = (simulation._in_declared_units(r.trace.view.properties, mote_id, key, solved)
                 if solved is not None else _number(props.get(key)))
        unit = r.garden.unit(mote_id, key) if mote_id in r.garden.by_id else None
        return {
            "key": key, "ref": r.garden.text_of(mote_id, key), "value": value,
            "unit": unit or r.trace.inferred.get(mote_id, {}).get(key),
            "sigma": uncertainty.sigma_of(props, key) if solved is None else r.sigma.get(v),
            "source": self.source_of(v, props, solved is not None),
            "check": self.check_of(v), "problem": r.culprits.get(v),
            "series": r.series.get(v) if r.series is not None else None,
        }

    def source_of(self, v: Unknown, props: dict, solved: bool) -> str:
        declared = calc.parse_source_property(props.get(calc.SOURCE_KEY)).get(v[1])
        if declared:
            return declared.kind
        if solved:
            return "solved"
        return "unsolved" if v in self.anywhere else "given"

    def check_of(self, v: Unknown) -> str | None:
        statuses = [self.r.trace.results[k].status for g in self.r.groups if g.kind == CHECK
                    and v in g.inputs for k in g.equations if k in self.r.trace.results]
        return max(statuses, key=lambda s: _WORST[s]) if statuses else None

    def summary(self) -> dict:
        checks = [self.r.trace.results[g.equations[0]] for g in self.shown
                  if g.kind == CHECK and g.equations[0] in self.r.trace.results]
        return {
            "steps": self.last - 1,
            "coupled": sum(1 for g in self.shown if g.method == SOLVER and len(g.equations) > 1),
            "answers": sum(len(g.values & self.r.ends) for g in self.shown),
            "checks": len(checks), "passed": sum(1 for c in checks if c.passed),
            "faults": sum(1 for g in self.shown if g.kind == FAULT),
        }


def _card_id(key: Key) -> str:
    return f"{key[0]}@{key[1]}"


def _order_key(g: Group, payload: _Payload) -> str:
    return min((payload.r.garden.text_of(*v) for v in g.values | g.inputs), default=g.id)


def _barycenter(cols: list[list[str]], edges: set[tuple[str, str]]) -> list[list[str]]:
    """Order each column by the mean place of its neighbours, sweeping
    right then left; ties keep their order, so the answer is stable."""
    preds: dict[str, list[str]] = defaultdict(list)
    succs: dict[str, list[str]] = defaultdict(list)
    for a, b in sorted(edges):
        preds[b].append(a)
        succs[a].append(b)
    place = {item: i / max(len(col) - 1, 1) for col in cols for i, item in enumerate(col)}
    for sweep in range(SWEEPS):
        down = sweep % 2 == 0
        order = range(1, len(cols)) if down else range(len(cols) - 2, -1, -1)
        for c in order:
            near = preds if down else succs
            cols[c] = sorted(cols[c], key=lambda i: _mean([place[n] for n in near[i] if n in place],
                                                         place[i]))
            place.update({item: i / max(len(cols[c]) - 1, 1) for i, item in enumerate(cols[c])})
    return cols


def _mean(values: list[float], default: float) -> float:
    return sum(values) / len(values) if values else default


def _solve_state(trace: simulation.Trace, groups: list[Group]) -> dict:
    faults = sum(1 for g in groups if g.kind == FAULT)
    if trace.error:
        state = "partial" if trace.journal.derivations else "failed"
        return {"state": state, "message": trace.error}
    if faults:
        return {"state": "partial", "message": f"{faults} group{'s' if faults > 1 else ''} left unsolved"}
    return {"state": "solved", "message": ""}


def _number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _health_label(v: Unknown, garden: _Garden) -> str:
    """As core/health.py labels a value, so a fault finds its message."""
    mote = garden.by_id.get(v[0])
    return f"{(mote.properties.get('name') if mote else None) or v[0][:8]}.{v[1]}"

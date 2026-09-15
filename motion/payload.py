# motion/payload.py
"""A garden shaped as a simulable system, for the JS bridge.

Monolith discovers a table's columns from what the garden contains;
this discovers a *board* the same way. A mote carries no coordinates, no
declared role in a simulation and no notion of being a component rather
than a knob, so everything the canvas draws is derived here:

    what a node is        <- core/kinds.py (rule) + declared freedoms
    what an edge is       <- ordinary relationships, plus the incidence
                             index's finer "this equation reads that value"
    what a lane traces    <- the (mote, key) pairs a run's steps changed
    what a rule binds     <- core/incidence.py's EquationTouch

None of that is stored, and none of it is invented: a node's kind is a
restatement of facts core already answers, in the vocabulary the canvas
needs. Solving, provenance and scenario application stay in core/ --
this module shapes answers and never computes one.

# Node kinds

Four kinds, and the choice of *which four* is the load-bearing decision
here. The obvious carve-up (pump, sensor, valve) is domain modelling and
belongs in the garden, not in a viewer. These four are properties of the
simulation itself, so they are true of any garden:

    rule      a mote with an equation and an applies_to  (core/kinds.RULE)
    free      a mote the solver may determine something on (declared
              `freedoms`) -- the things that move when you press play
    varied    a mote some scenario overrides -- the knobs a scenario turns
    fixed     everything else the rules touch

`varied` is relative to the scenario in hand, so it is computed per
board rather than cached on the node.

# On time

The timeline this feeds is indexed in **ticks**, and a tick is one
re-solve of the equation set -- core/simulation.py has no dt, no seconds
and no integration. Every function here is therefore honest about
speaking ticks, and `Lane.points` is sampled per tick rather than per
unit time. When core grows a real time axis, the seam is `tick_axis()`
below: it is the one place that answers "what is step N called", and a
second entry there (seconds, from whatever dt the engine ends up
carrying) is the whole change on this side.
"""
from dataclasses import dataclass, field

from core import incidence as incidence_repo
from core import kinds as kinds_repo
from core import rules as rules_repo
from core import scenarios as scenarios_repo
from core.entities import Mote, Relationship
from core.units import UNITS_KEY, format_unit, parse_units_property

# Node kinds. Deliberately about the simulation rather than the domain --
# see the module docstring.
RULE = "rule"
FREE = "free"
VARIED = "varied"
FIXED = "fixed"

# Edge kinds, in the order the canvas layers them: a governs edge is
# drawn over a plain relationship, and a reads edge under both.
GOVERNS = "governs"      # rule --applies_to--> its target
READS = "reads"          # an equation references a value on this mote
RELATION = "relation"    # any other relationship the garden declares

# How many lanes the timeline offers before it stops discovering more.
# A run that moves 400 values is a run nobody scrubs lane by lane; the
# cap keeps the payload and the DOM proportional to what can be read.
MAX_LANES = 24


def tick_axis(step_count: int) -> dict:
    """What the timeline's axis is counting.

    One dict rather than a bare integer because this is the seam the
    module docstring names: today ticks are the only axis core can
    describe, and `unit` says so out loud rather than letting the
    frontend assume seconds and quietly mislabel a scrub. A future
    engine that carries dt adds `seconds` here and the dock grows a
    second axis mode against it -- no other shape changes."""
    return {"unit": "tick", "label": "ticks", "count": step_count}


@dataclass(frozen=True)
class Node:
    """One mote as the canvas draws it. Carries no position: layout is
    the frontend's, persisted per garden in localStorage, because where
    a user dragged a node is a fact about their window and not about the
    garden (and writing it back would make every pan a graph edit)."""
    id: str
    label: str
    kind: str
    equation: str | None = None
    freedoms: tuple[str, ...] = ()
    # The mote's numeric properties as the garden currently holds them.
    # Only the numeric ones: these are what a tick can move and what a
    # lane can draw, and a node card showing a paragraph of prose is a
    # node card nobody can read at 60% zoom. The frontend folds a run's
    # changes over these to get the value at a scrubbed-to tick, which is
    # why they travel with the board rather than being re-fetched per
    # tick -- see app.js::valuesAt.
    values: dict = field(default_factory=dict)
    # Each of those keys' declared unit, from the mote's own `units`
    # property (core/units.py) -- {"resistance": "kohm"}. A key with no
    # entry is unitless. Travels alongside `values` so the card can label
    # a number without a second round trip.
    units: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "kind": self.kind,
            "equation": self.equation,
            "freedoms": list(self.freedoms),
            "values": dict(self.values),
            "units": {key: format_unit(value) for key, value in self.units.items()},
        }


@dataclass(frozen=True)
class Edge:
    id: str
    from_id: str
    to_id: str
    type: str
    kind: str

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "from": self.from_id,
            "to": self.to_id,
            "type": self.type,
            "kind": self.kind,
        }


@dataclass(frozen=True)
class Binding:
    """One value an equation references, and which side of it that value
    falls on. `determines` and `reads` are core/incidence.py's partition,
    not this module's guess -- see EquationTouch."""
    mote_id: str
    mote_label: str
    key: str
    role: str          # "determines" | "reads"
    value: float | None

    def to_json(self) -> dict:
        return {
            "moteId": self.mote_id,
            "moteLabel": self.mote_label,
            "key": self.key,
            "role": self.role,
            "value": self.value,
        }


@dataclass(frozen=True)
class RuleCard:
    """A rule as the right-hand panel shows it.

    `equation` is the mote's own text, shown verbatim. There is no
    prettified render beside it: core stores one expression, and a
    second "rendered" form would either be the same string in a nicer
    font or a typesetting layer inventing notation the garden never
    declared."""
    mote_id: str
    name: str
    equation: str
    scope: str
    target_id: str
    anchors: tuple[str, ...] = ()
    bindings: tuple[Binding, ...] = ()

    def to_json(self) -> dict:
        return {
            "moteId": self.mote_id,
            "name": self.name,
            "equation": self.equation,
            "scope": self.scope,
            "targetId": self.target_id,
            "anchors": list(self.anchors),
            "bindings": [b.to_json() for b in self.bindings],
        }


@dataclass(frozen=True)
class Lane:
    """One traced value across a run's ticks.

    `points` is indexed by tick and may hold None before the value is
    first determined -- a lane that starts undetermined draws as a gap
    rather than as a zero, since those mean opposite things."""
    mote_id: str
    mote_label: str
    key: str
    unit: str | None
    points: tuple[float | None, ...]

    @property
    def label(self) -> str:
        return f"{self.mote_label} · {self.key}"

    def to_json(self) -> dict:
        finite = [p for p in self.points if p is not None]
        return {
            "moteId": self.mote_id,
            "moteLabel": self.mote_label,
            "key": self.key,
            "unit": format_unit(self.unit) if self.unit else self.unit,
            "label": self.label,
            "points": list(self.points),
            "min": min(finite) if finite else None,
            "max": max(finite) if finite else None,
        }


def _label(mote: Mote) -> str:
    return str(mote.properties.get("name", mote.id[:8]))


def _numeric_values(mote: Mote) -> dict:
    return {
        key: value
        for key, value in sorted(mote.properties.items())
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    }


def node_kinds(
    motes: list[Mote],
    relationships: list[Relationship],
    varied_ids: set[str],
) -> dict[str, str]:
    """Every mote's node kind, in one pass.

    Precedence is rule, then varied, then free, then fixed -- what a
    mote *is for* before what happens to it, the same ordering rule
    core/kinds.py's KINDS table uses. A rule mote a scenario also
    overrides still reads as a rule: it is the equation that decides how
    the canvas draws it."""
    rule_ids = set(kinds_repo.ids_with_kind(kinds_repo.RULE, motes, relationships))
    kinds = {}
    for mote in motes:
        if mote.id in rule_ids:
            kinds[mote.id] = RULE
        elif mote.id in varied_ids:
            kinds[mote.id] = VARIED
        elif rules_repo.parse_freedoms(mote.properties.get(rules_repo.FREEDOMS_KEY)):
            kinds[mote.id] = FREE
        else:
            kinds[mote.id] = FIXED
    return kinds


def build_nodes(
    motes: list[Mote],
    relationships: list[Relationship],
    varied_ids: set[str],
) -> list[Node]:
    kinds = node_kinds(motes, relationships, varied_ids)
    nodes = []
    for mote in motes:
        equation = mote.properties.get(rules_repo.EQUATION_KEY)
        nodes.append(
            Node(
                id=mote.id,
                label=_label(mote),
                kind=kinds[mote.id],
                equation=equation if isinstance(equation, str) else None,
                freedoms=tuple(
                    rules_repo.parse_freedoms(mote.properties.get(rules_repo.FREEDOMS_KEY))
                ),
                values=_numeric_values(mote),
                units=parse_units_property(mote.properties.get(UNITS_KEY)),
            )
        )
    return nodes


def build_edges(relationships: list[Relationship], index: incidence_repo.Index) -> list[Edge]:
    """Declared relationships, plus the read edges only the incidence
    index knows about.

    A rule's applies_to is drawn as `governs`; every other declared
    relationship keeps its own type and draws as `relation`. The read
    edges are not relationships at all -- nobody drew them -- so they
    carry a synthetic id and exist purely to show why a value moves when
    a neighbour does. They are deduplicated per (rule, mote) pair: one
    equation reading three properties off the same mote is one arrow,
    not three."""
    edges = [
        Edge(
            id=rel.id,
            from_id=rel.from_id,
            to_id=rel.to_id,
            type=rel.type,
            kind=GOVERNS if rel.type == rules_repo.APPLIES_TO else RELATION,
        )
        for rel in relationships
    ]
    seen: set[tuple[str, str]] = set()
    for touch in index.touches:
        for mote_id, _key in touch.reads:
            pair = (mote_id, touch.rule_mote_id)
            if pair in seen or mote_id == touch.rule_mote_id:
                continue
            seen.add(pair)
            edges.append(
                Edge(
                    id=f"reads:{touch.rule_mote_id}:{mote_id}",
                    from_id=mote_id,
                    to_id=touch.rule_mote_id,
                    type="reads",
                    kind=READS,
                )
            )
    return edges


def build_rule_cards(
    rules: list[rules_repo.Rule],
    index: incidence_repo.Index,
    labels: dict[str, str],
    values: dict[tuple[str, str], float] | None = None,
) -> list[RuleCard]:
    """One card per rule mote, with its bindings resolved.

    A rule scoped `each` produces one EquationTouch per instance, so its
    bindings are gathered across every anchor rather than from the first
    one -- otherwise a rule applied to twelve pumps would show the
    bindings of whichever pump happened to sort first."""
    values = values or {}
    cards = []
    for rule in rules:
        touches = [t for t in index.touches if t.rule_mote_id == rule.mote.id]
        bindings: list[Binding] = []
        seen: set[tuple[str, str, str]] = set()
        for touch in touches:
            for role, variables in (("determines", touch.determines), ("reads", touch.reads)):
                for mote_id, key in sorted(variables):
                    if (mote_id, key, role) in seen:
                        continue
                    seen.add((mote_id, key, role))
                    bindings.append(
                        Binding(
                            mote_id=mote_id,
                            mote_label=labels.get(mote_id, mote_id[:8]),
                            key=key,
                            role=role,
                            value=values.get((mote_id, key)),
                        )
                    )
        cards.append(
            RuleCard(
                mote_id=rule.mote.id,
                name=rule.name,
                equation=rule.equation,
                scope=rule.scope,
                target_id=rule.target_id,
                anchors=tuple(sorted({t.anchor_mote_id for t in touches})),
                bindings=tuple(bindings),
            )
        )
    return cards


def build_lanes(
    steps: list[dict],
    base_properties: dict[str, dict],
    labels: dict[str, str],
    units: dict[str, dict[str, str]] | None = None,
    limit: int = MAX_LANES,
) -> list[Lane]:
    """Turn a run's steps into one line per value that moved.

    Takes the *payload* form of steps (core/simulation.py's
    step_payloads) rather than StepResult, because a logged run read back
    through run_ticks only ever exists in that form -- one code path for
    a live preview and a replayed run, which is the same reason
    monarch's scrub bar took its steps that way.

    Point 0 is the value before the run, read off the garden, so a lane
    shows where a value started rather than beginning at its first
    change. Values that never move produce no lane at all: a flat line
    is not a trace, it is a constant, and the inspector already shows it.

    Lanes are ordered by how much each value moved, widest first, so the
    four lanes a short dock can show are the four worth showing. `limit`
    caps the count -- see MAX_LANES."""
    units = units or {}
    moved: dict[tuple[str, str], list[float | None]] = {}
    for (mote_id, key) in _changed_variables(steps):
        start = base_properties.get(mote_id, {}).get(key)
        moved[(mote_id, key)] = [start if isinstance(start, (int, float)) else None]

    for step in steps:
        for series in moved.values():
            series.append(series[-1])
        for change in step.get("changes", []):
            variable = (change["mote_id"], change["key"])
            if variable in moved:
                moved[variable][-1] = change["new_value"]

    lanes = [
        Lane(
            mote_id=mote_id,
            mote_label=labels.get(mote_id, mote_id[:8]),
            key=key,
            unit=units.get(mote_id, {}).get(key),
            points=tuple(series),
        )
        for (mote_id, key), series in moved.items()
    ]
    lanes.sort(key=_span, reverse=True)
    return lanes[:limit]


def _changed_variables(steps: list[dict]) -> list[tuple[str, str]]:
    """Every (mote, key) any step touched, in first-seen order so the
    lane list is stable between two runs that moved the same values."""
    seen: list[tuple[str, str]] = []
    known: set[tuple[str, str]] = set()
    for step in steps:
        for change in step.get("changes", []):
            variable = (change["mote_id"], change["key"])
            if variable not in known:
                known.add(variable)
                seen.append(variable)
    return seen


def _span(lane: Lane) -> float:
    finite = [p for p in lane.points if p is not None]
    if not finite:
        return 0.0
    return abs(max(finite) - min(finite))


def build_events(steps: list[dict]) -> list[dict]:
    """The marks under the timeline: ticks where something happened that
    a person would want to jump to.

    Only two things qualify today, and both are recorded by core rather
    than inferred here -- a constraint the step's values broke, and the
    step a failed run stopped at (the caller passes that in as its own
    mark). Deliberately not "a rule fired": every rule fires on every
    tick, so a mark per firing would be a solid bar."""
    events = []
    for step in steps:
        for problem in step.get("constraint_violations", []):
            events.append({"tick": step["step"], "kind": "constraint", "text": problem})
    return events


def scenario_payload(scenarios: list[scenarios_repo.Scenario]) -> list[dict]:
    return [
        {
            "id": scenario.mote_id,
            "name": scenario.name,
            "overrideCount": len(scenario.overrides),
            "targetIds": sorted(scenario.target_ids),
        }
        for scenario in scenarios
    ]


def varied_ids(scenarios: list[scenarios_repo.Scenario], scenario_id: str | None) -> set[str]:
    """Which motes the active scenario overrides -- the `varied` node
    kind's whole definition. Empty when no scenario is selected, which
    is why VARIED is computed per board and not cached on a node."""
    if scenario_id is None:
        return set()
    for scenario in scenarios:
        if scenario.mote_id == scenario_id:
            return set(scenario.target_ids)
    return set()

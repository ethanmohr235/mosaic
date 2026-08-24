# core/modoc_bind.py
"""Resolving a parsed .modoc against a garden.

core/modoc.py knows what a note *says*; this knows what it *means* right
now -- which mote a `\\mote{...}` points at, what a `\\val{...}` comes to,
what a `\\graph{...}` plots. The split is the reason the grammar can be
tested with strings and this can be tested with a garden.

Two things it deliberately does not do. It never writes: a note is a
reader, and stepping the clock forward is a preview
(`run_time_series(commit=False)`), not a run. And it never raises: every
failure lands in the returned diagnostics, because a note with one bad
expression in it must still render the other nineteen.
"""
import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field

from core import modoc
from core import kinds as kinds_repo
from core.entities import Mote, Relationship
from core.formula import Context, FormulaError, evaluate_expression, evaluate_selection
from core.motes import list_motes
from core.relationships import list_relationships
from core.simulation import SimulationError, expression_context, run_time_series

logger = logging.getLogger(__name__)

NAME_KEY = "name"
# How far `\graph{... over t}` looks ahead when the reader has not
# stepped the clock themselves. A projection of one point is not a
# projection; this is the "10 day projection" of the original design,
# expressed in steps because a step is the only unit a garden has.
DEFAULT_HORIZON = 8

_PLAIN_PROPERTY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MISSING = "—"


@dataclass(frozen=True)
class LinkTarget:
    """What a `\\mote{ref}` came to. `mote_id` is None when nothing
    matched, which the renderers show as a broken link rather than
    silently dropping."""
    ref: str
    mote_id: str | None
    name: str
    kinds: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass(frozen=True)
class ValueResult:
    """`text` is always safe to print -- an error message renders in its
    own colour rather than leaving a hole in the sentence."""
    text: str
    value: object = None
    error: str | None = None


@dataclass(frozen=True)
class Series:
    """One `\\graph`. Both forms reduce to labels + numbers, so a renderer
    only needs to know whether to draw a line or bars."""
    kind: str
    labels: list[str] = field(default_factory=list)
    values: list[float] = field(default_factory=list)
    error: str | None = None


@dataclass(frozen=True)
class Resolution:
    anchor_id: str
    step: int
    links: dict[str, LinkTarget] = field(default_factory=dict)
    values: dict[str, ValueResult] = field(default_factory=dict)
    graphs: dict[str, Series] = field(default_factory=dict)
    diagnostics: list[modoc.Diagnostic] = field(default_factory=list)

    @property
    def projected(self) -> bool:
        """Whether these numbers are the graph's, or a projection of it."""
        return self.step > 0


class _Timeline:
    """Every mote's properties at each step of one nondestructive preview.

    Computed once per resolve(): a note with a `\\val`, a `by` graph and an
    `over t` graph in it asks about several steps, and running the solver
    once per question would be both slow and -- if a rule ever stopped
    being deterministic -- inconsistent between them."""

    def __init__(self, conn: sqlite3.Connection, motes: list[Mote], span: int) -> None:
        self.relationships: list[Relationship] = list_relationships(conn)
        baseline = {m.id: dict(m.properties) for m in motes}
        self.states: list[dict[str, dict]] = [baseline]
        self.error: str | None = None
        if span > 0:
            self._extend(conn, span)

    def _extend(self, conn: sqlite3.Connection, span: int) -> None:
        try:
            result = run_time_series(conn, span, commit=False)
        except (SimulationError, FormulaError, ValueError) as e:
            self.error = f"simulation preview failed: {e}"
            logger.warning("modoc preview failed: %s", e)
            return
        state = self.states[0]
        for step in result.steps:
            state = {mid: dict(props) for mid, props in state.items()}
            for (mote_id, key), value in step.values.items():
                state.setdefault(mote_id, {})[key] = value
            self.states.append(state)
        if result.error is not None:
            self.error = f"simulation stopped at step {result.failed_at}: {result.error}"

    @property
    def reach(self) -> int:
        """The last step actually computed -- a run that failed early
        still answers for the steps before it did."""
        return len(self.states) - 1

    def at(self, step: int) -> dict[str, dict]:
        return self.states[min(step, self.reach)]

    def context(self, step: int, mote_id: str) -> Context:
        return expression_context(
            self.at(step), self.relationships, mote_id,
            tick=float(step), previous=self.at(max(0, step - 1)),
        )


def resolve(
    conn: sqlite3.Connection,
    note_id: str,
    document: modoc.Document,
    step: int = 0,
    horizon: int = DEFAULT_HORIZON,
) -> Resolution:
    """Everything the note's macros come to at `step`. `step` 0 is the
    garden as stored; anything higher is a preview that far ahead and is
    never written back."""
    anchor_id, diagnostics = modoc.anchor(conn, note_id)
    motes = list_motes(conn)
    span = max(step, horizon if _needs_timeline(document) else 0)
    timeline = _Timeline(conn, motes, span)
    if timeline.error is not None:
        diagnostics.append(modoc.Diagnostic(0, "simulation", timeline.error))

    links = {
        node.ref: _link(node.ref, motes, timeline.relationships)
        for node in modoc.links(document)
    }
    values = {
        modoc.value_key(node): _value(node, anchor_id, motes, timeline, step)
        for node in modoc.values(document)
    }
    graphs = {
        modoc.graph_key(node): _graph(node, anchor_id, motes, timeline, step, horizon)
        for node in modoc.graphs(document)
    }
    return Resolution(anchor_id, step, links, values, graphs, diagnostics)


def problems(document: modoc.Document, resolution: Resolution) -> list[modoc.Diagnostic]:
    """Everything wrong with a note, in one list: what wouldn't parse and
    what wouldn't resolve.

    Here rather than in each surface because it is one question with one
    answer. It was briefly the CLI's own -- assembled inside `modoc
    check` -- and Mora promptly reported a note with a dangling
    `\\mote{...}` in it as having no problems at all, which is exactly
    the drift core/kinds.py exists to prevent for a different question.
    Line numbers come off the nodes, not off the resolution, so a
    reference that fails twice is reported at both places it failed."""
    found = list(document.diagnostics) + list(resolution.diagnostics)
    for node in modoc.links(document):
        target = resolution.links.get(node.ref)
        if target is not None and target.error:
            found.append(modoc.Diagnostic(
                node.line, "unresolved_link", f"\\mote{{{node.ref}}}: {target.error}"
            ))
    for node in modoc.values(document):
        result = resolution.values.get(modoc.value_key(node))
        if result is not None and result.error:
            found.append(modoc.Diagnostic(
                node.line, "unresolved_value", f"\\val{{{node.expression}}}: {result.error}"
            ))
    for node in modoc.graphs(document):
        series = resolution.graphs.get(modoc.graph_key(node))
        if series is not None and series.error:
            found.append(modoc.Diagnostic(node.line, "unresolved_graph", f"\\graph: {series.error}"))
    return sorted(found, key=lambda d: d.line)


def _needs_timeline(document: modoc.Document) -> bool:
    return any(
        g.spec is not None and g.spec.kind == modoc.OVER_T for g in modoc.graphs(document)
    )


# --- references ------------------------------------------------------

def _link(ref: str, motes: list[Mote], relationships: list[Relationship]) -> LinkTarget:
    mote, error = _find(ref, motes)
    if mote is None:
        return LinkTarget(ref, None, ref, [], error)
    return LinkTarget(ref, mote.id, _name(mote), kinds_repo.kinds_of(mote, relationships))


def _find(ref: str, motes: list[Mote]) -> tuple[Mote | None, str | None]:
    """An exact id, else a mote whose `name` matches uniquely.

    Ids win outright: the `@` picker in Mora inserts them precisely so a
    rename can't break a link, and a hand-typed name should never shadow
    one. Name matching tries exact first and only then case-insensitively,
    so "atlas" finding "Atlas" is a convenience rather than a way for two
    differently-cased motes to become ambiguous with each other."""
    ref = ref.strip()
    if not ref:
        return None, "empty reference"
    for mote in motes:
        if mote.id == ref:
            return mote, None

    exact = [m for m in motes if m.properties.get(NAME_KEY) == ref]
    matches = exact or [
        m for m in motes
        if isinstance(m.properties.get(NAME_KEY), str)
        and m.properties[NAME_KEY].casefold() == ref.casefold()
    ]
    if len(matches) == 1:
        return matches[0], None
    if matches:
        return None, f"{len(matches)} motes are named {ref!r} -- link by id instead"
    return None, f"no mote with id or name {ref!r}"


def _name(mote: Mote) -> str:
    name = mote.properties.get(NAME_KEY)
    return name if isinstance(name, str) and name else mote.id[:8]


def _anchor_for(
    ref: str | None, anchor_id: str, motes: list[Mote]
) -> tuple[str | None, str | None]:
    """Which mote a macro reads from -- the note's anchor, or the `[ref]`
    it named instead."""
    if ref is None:
        return anchor_id, None
    mote, error = _find(ref, motes)
    return (mote.id if mote else None), error


# --- values ----------------------------------------------------------

def _value(
    node: modoc.Value, anchor_id: str, motes: list[Mote], timeline: _Timeline, step: int
) -> ValueResult:
    mote_id, error = _anchor_for(node.ref, anchor_id, motes)
    if mote_id is None:
        return ValueResult(f"?{node.ref}", None, error)

    properties = timeline.at(step).get(mote_id, {})
    if _PLAIN_PROPERTY_RE.match(node.expression) and node.expression in properties:
        # The fast path, and the reason \val{due} can be a date at all:
        # core/formula.py is numeric by construction, so a plain property
        # read has to happen before the expression evaluator sees it.
        raw = properties[node.expression]
        return ValueResult(format_value(raw), raw)

    try:
        number = evaluate_expression(node.expression, timeline.context(step, mote_id)).value()
    except (FormulaError, ZeroDivisionError) as e:
        return ValueResult(f"?{node.expression}", None, str(e))
    return ValueResult(format_value(number), number)


def format_value(value: object) -> str:
    """One reading of a property for every surface. Integral floats lose
    their `.0` -- a task count solved by the simulation is a count, and
    "12.0 tasks are closed" reads like a bug."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, (list, dict)):
        return json.dumps(value, separators=(", ", ": "))
    return str(value)


# --- graphs ----------------------------------------------------------

def _graph(
    node: modoc.Graph, anchor_id: str, motes: list[Mote],
    timeline: _Timeline, step: int, horizon: int,
) -> Series:
    if node.spec is None:
        return Series("", error="unreadable \\graph specification")
    mote_id, error = _anchor_for(node.ref, anchor_id, motes)
    if mote_id is None:
        return Series(node.spec.kind, error=error)
    if node.spec.kind == modoc.OVER_T:
        return _series_over_t(node.spec, mote_id, timeline, max(step, horizon))
    return _series_by(node.spec, mote_id, timeline, step)


def _series_over_t(
    spec: modoc.GraphSpec, mote_id: str, timeline: _Timeline, span: int
) -> Series:
    labels, values = [], []
    for step in range(min(span, timeline.reach) + 1):
        try:
            number = evaluate_expression(spec.expression, timeline.context(step, mote_id)).value()
        except (FormulaError, ZeroDivisionError) as e:
            return Series(modoc.OVER_T, labels, values, str(e))
        labels.append(f"t+{step}")
        values.append(float(number))
    return Series(modoc.OVER_T, labels, values, timeline.error)


def _series_by(
    spec: modoc.GraphSpec, mote_id: str, timeline: _Timeline, step: int
) -> Series:
    try:
        selected = evaluate_selection(spec.expression, timeline.context(step, mote_id))
    except FormulaError as e:
        return Series(modoc.BY, error=str(e))

    state = timeline.at(step)
    counts: dict[str, int] = {}
    for selected_id in selected:
        label = state.get(selected_id, {}).get(spec.key, _MISSING)
        counts[format_value(label)] = counts.get(format_value(label), 0) + 1

    ordered = sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))
    return Series(modoc.BY, [k for k, _ in ordered], [float(v) for _, v in ordered])

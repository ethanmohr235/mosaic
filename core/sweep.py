# core/sweep.py
"""Sweeping: one garden solved many times over, once per point of a
range, with nothing written down.

A garden holds one state and `core/scenarios.py` lets it hold a second,
named one. Neither answers the question a model is usually built for --
*how does this value move as that one changes* -- because that question
has no single state as its answer. It has a curve.

A sweep is the curve. Pick a property to vary and a range to vary it
over, and this re-solves the whole garden at every step, reading the
properties you asked about out of each solve. The mechanism is entirely
`core/simulation.py`'s: a perturbed copy of the properties dict, run
through `solve_properties()`. What makes it a sweep rather than N runs is
what it deliberately does *not* do -- no write-back, no provenance, no
scenario motes created and destroyed around the edges. A sweep leaves the
garden exactly as it found it, which is what makes it safe to fire on
every frame of a dragged slider.

Three shapes, and they are the same machinery three ways:

    run_sweep         one axis   -> a line (or several)
    run_surface       two axes   -> a grid of cells
    run_correlation   no axis    -> one dot per mote, from a single solve

# Cases, not series

The caller hands in `cases`: the distinct what-ifs to evaluate at every
point. Two curves that differ only in which property they read -- R1's
current and R2's current -- are *one* case and one solve per step; two
that differ in scenario or in an override are two. Series are the
frontend's business, cases are this module's, and keeping them apart is
worth real money: the cost of a sweep is `len(cases) * steps`, and a
garden's worth of series usually shares a handful of cases.

# Failure is a point, not an end

A solve can fail at one x and succeed at the next -- an equation that
divides by the swept value at zero, a Newton run that will not converge
at one end of a range. A failed point carries the solver's own message
and the sweep continues, because 219 good points and a labelled gap is a
better answer than an exception. Only the caller can say whether the gap
matters.
"""
import logging
import sqlite3
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from core import rules as rules_repo
from core import scenarios as scenarios_repo
from core.entities import Relationship
from core.motes import list_motes
from core.relationships import list_relationships
from core.simulation import (
    SimulationError,
    Solved,
    solve_properties,
)
from core.solvers import SolverError

logger = logging.getLogger(__name__)

_DEFAULT_SOLVER = "linear"

# A property named by mote and key -- what a sweep reads out of each solve.
Output = tuple[str, str]

# A saved plot: the property a plot mote carries, holding the *specification*
# a sweep is run from -- which motes, which keys, which ranges -- and never
# the points it produced. That is the whole difference between a saved plot
# and a screenshot: reopening one re-solves the garden as it stands now, so a
# plot embedded in a note cannot go stale the way a pasted number does.
PLOT_KEY = "plot"


def has_plot(properties: dict) -> bool:
    """The test for "this mote is a saved plot", mirroring
    core/query.py::has_query: the property is present, whatever it holds."""
    return isinstance(properties.get(PLOT_KEY), (dict, str))


@dataclass(frozen=True)
class Axis:
    """A property to vary, and the range to vary it over.

    `steps` is the number of intervals, so an axis has `steps + 1` points
    and both ends of the range are evaluated -- a sweep of a resistance
    from 100 to 200 should include 200.
    """
    mote_id: str
    key: str
    lo: float
    hi: float
    steps: int = 200

    def __post_init__(self) -> None:
        if self.steps < 1:
            raise ValueError("an axis needs at least one step")
        if self.hi == self.lo:
            raise ValueError("an axis needs a range, not a point")

    @property
    def count(self) -> int:
        return self.steps + 1

    def at(self, index: int) -> float:
        return self.lo + (self.hi - self.lo) * index / self.steps

    def values(self) -> list[float]:
        return [self.at(i) for i in range(self.count)]


@dataclass(frozen=True)
class Case:
    """One what-if, evaluated at every point of the axis.

    `overrides` are transient: {mote_id: {key: value}}, merged over the
    scenario's own, never written anywhere. They are how a knob works --
    the user drags a resistance and every point re-solves against the new
    value without the garden ever hearing about it.
    """
    key: str
    scenario_id: str | None = None
    overrides: dict[str, dict[str, float]] = field(default_factory=dict)


@dataclass(frozen=True)
class Point:
    """One x, solved for every case.

    `values` is keyed by (case key, mote id, property key) rather than
    nested, because every consumer of it is doing a flat lookup per series
    and nesting would only mean three `.get`s instead of one. `errors` is
    per case: a case that failed here contributes no values at all, and
    the message says why.

    `violations` is the constraints this point's answer does not satisfy,
    per case. A sweep gets them for free -- every solve checks its
    constraints anyway -- and they are the honest form of a limit line:
    rather than reading a fixed bound off a rule and drawing it, the plot
    shades the range where the model actually broke its own rule, which
    stays right even where the bound itself moves with the sweep.
    """
    index: int
    x: float
    values: dict[tuple[str, str, str], float] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    violations: dict[str, list[str]] = field(default_factory=dict)


@dataclass(frozen=True)
class SweepResult:
    """`points` holds only what was actually computed -- a coarse pass or a
    cancelled one returns fewer than the axis has, each carrying its own
    `index` so a caller refining progressively can merge passes without
    recomputing what it already drew."""
    axis: Axis
    points: list[Point]
    case_count: int
    solves: int
    seconds: float
    pinned: bool = False       # the swept property is one of its mote's freedoms
    cancelled: bool = False

    @property
    def failures(self) -> int:
        return sum(len(p.errors) for p in self.points)

    @property
    def broken_constraints(self) -> list[str]:
        """Every distinct constraint broken anywhere in the sweep, in
        first-seen order -- one advisory line per constraint rather than
        the same sentence repeated at every point that breaks it.

        The by-how-much each message ends in is what makes two points'
        reports differ, and it is exactly what a summary should drop: the
        fact worth stating once is *which* rule the answer breaks. Where
        it breaks is already on the plot, shaded."""
        seen: list[str] = []
        for point in self.points:
            for problems in point.violations.values():
                for problem in problems:
                    fact = problem.split(" (off by ")[0]
                    if fact not in seen:
                        seen.append(fact)
        return seen


@dataclass(frozen=True)
class Cell:
    """One solve of a surface: a point on both axes, and the output there."""
    row: int
    col: int
    x: float
    y: float
    value: float | None = None
    error: str | None = None


@dataclass(frozen=True)
class SurfaceResult:
    x_axis: Axis
    y_axis: Axis
    cells: list[Cell]
    output: Output
    solves: int
    seconds: float
    cancelled: bool = False

    @property
    def span(self) -> tuple[float, float] | None:
        """Lowest and highest value across the grid, for a colour scale.
        None where nothing solved."""
        values = [c.value for c in self.cells if c.value is not None]
        return (min(values), max(values)) if values else None


@dataclass(frozen=True)
class Dot:
    """One mote in a correlation: two of its properties, after one solve."""
    mote_id: str
    name: str
    x: float
    y: float


@dataclass(frozen=True)
class Ground:
    """The garden read once, scenarios and all. Every point of a sweep
    perturbs a copy of this rather than going back to the database -- the
    same economy run_time_series() makes across its steps, and the reason
    a 220-point sweep is 220 solves rather than 220 solves plus 220 full
    reads.

    Worth being a public type rather than a private cache because a
    caller can now read one *before* starting, and sweep outside whatever
    lock guards its connection. That is what lets a window stay
    responsive: a sweep that held the connection for its whole run would
    also be holding it against the very keystroke meant to cancel it.
    """
    properties: dict[str, dict]
    relationships: list[Relationship]
    rules: list[rules_repo.Rule]
    scenarios: dict[str, scenarios_repo.Scenario] = field(default_factory=dict)


# Either the connection to read the garden from, or a garden already read.
Source = "sqlite3.Connection | Ground"


def read_garden(
    conn: sqlite3.Connection, scenario_ids: Sequence[str] = ()
) -> Ground:
    """Everything a sweep needs from the database, in one read.

    `scenario_ids` are resolved here too, so that a sweep handed the
    result never touches the connection again -- an unknown id is refused
    now rather than part-way through a run."""
    return Ground(
        properties={m.id: dict(m.properties) for m in list_motes(conn)},
        relationships=list_relationships(conn),
        rules=rules_repo.list_rules(conn),
        scenarios={
            scenario_id: _resolve(conn, scenario_id)
            for scenario_id in dict.fromkeys(s for s in scenario_ids if s)
        },
    )


def _resolve(
    conn: sqlite3.Connection, scenario_id: str
) -> scenarios_repo.Scenario:
    """Mirrors core/simulation.py's own refusal to treat an unknown
    scenario id as "no scenario": it is almost always a typo, and
    answering a different question than the one asked is worse than
    failing."""
    case = scenarios_repo.get_scenario(conn, scenario_id)
    if case is None:
        raise SimulationError(
            f"no scenario {scenario_id[:8]!r} -- a scenario is a mote with at least "
            f"one {scenarios_repo.VARIES!r} relationship to an override"
        )
    return case


def _ground(source, scenario_ids: Sequence[str]) -> Ground:
    """A Ground either way. Passing a connection is the plain case -- a
    test, the CLI, an MCP tool -- and reads it here; passing a Ground is
    the caller that already read it under its own lock."""
    if isinstance(source, Ground):
        missing = [s for s in scenario_ids if s and s not in source.scenarios]
        if missing:
            raise SimulationError(
                f"scenario {missing[0][:8]!r} was not read with this garden -- "
                "name every scenario a sweep will use when calling read_garden()"
            )
        return source
    return read_garden(source, scenario_ids)


def _scenario_of(ground: Ground, scenario_id: str | None):
    return ground.scenarios.get(scenario_id) if scenario_id else None


def _perturbed(
    ground: Ground,
    scenario: scenarios_repo.Scenario | None,
    *overrides: dict[str, dict[str, float]],
) -> dict[str, dict]:
    """The garden's properties with a scenario and then each override
    layer merged over them, in order. Pure -- `ground.properties` is never
    touched, so the next point starts from the same place this one did."""
    properties = dict(scenarios_repo.apply(ground.properties, scenario))
    for layer in overrides:
        for mote_id, values in layer.items():
            merged = dict(properties.get(mote_id, {}))
            merged.update(values)
            properties[mote_id] = merged
    return properties


def _solve(
    ground: Ground,
    scenario: scenarios_repo.Scenario | None,
    overrides: dict[str, dict[str, float]],
    point: dict[str, dict[str, float]],
    solver: str,
    solver_configuration: dict | None,
) -> tuple[Solved | None, str | None]:
    """One solve of one perturbed copy, with its failure caught.

    Every solver failure is a property of this point alone -- the next x
    may well solve -- so it comes back as a message rather than an
    exception. A model_cache is deliberately *not* shared across points:
    a provider is constructed from properties a sweep may be varying.
    """
    try:
        solved = solve_properties(
            _perturbed(ground, scenario, overrides, point),
            ground.relationships,
            ground.rules,
            solver,
            solver_configuration,
            log_constraints=False,
        )
    except (SimulationError, SolverError) as e:
        return None, str(e)
    return solved, None


def run_sweep(
    source,
    axis: Axis,
    cases: Sequence[Case],
    outputs: Sequence[Output],
    solver: str = _DEFAULT_SOLVER,
    solver_configuration: dict | None = None,
    *,
    indices: Sequence[int] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> SweepResult:
    """Solve the garden once per case at every point of `axis`, reading
    `outputs` out of each solve. Writes nothing.

    `indices` restricts the sweep to those points of the axis -- every
    fourth one for a coarse first pass, then the rest to refine it. The
    points come back labelled with their index so the two passes merge.

    `should_cancel` is polled between points. A newer request arriving
    while this one is mid-sweep should not have to wait out the old one,
    so a cancelled sweep returns the points it did compute with
    `cancelled` set rather than raising.
    """
    started = time.monotonic()
    ground = _ground(source, [c.scenario_id for c in cases])
    scenarios = {c.key: _scenario_of(ground, c.scenario_id) for c in cases}
    wanted = list(indices) if indices is not None else list(range(axis.count))

    points: list[Point] = []
    solves = 0
    pinned = False
    cancelled = False
    for index in wanted:
        if should_cancel is not None and should_cancel():
            cancelled = True
            break
        x = axis.at(index)
        point = Point(index=index, x=x, values={}, errors={})
        for case in cases:
            solved, error = _solve(
                ground, scenarios[case.key], case.overrides,
                {axis.mote_id: {axis.key: x}}, solver, solver_configuration,
            )
            solves += 1
            if solved is None:
                point.errors[case.key] = error or "the solve failed"
                continue
            pinned = pinned or solved.is_free(axis.mote_id, axis.key)
            if solved.constraint_violations:
                point.violations[case.key] = list(solved.constraint_violations)
            for mote_id, key in outputs:
                value = solved.value(mote_id, key)
                if value is not None:
                    point.values[(case.key, mote_id, key)] = value
        points.append(point)

    seconds = time.monotonic() - started
    logger.info(
        "swept %s over %s points x %s cases in %.3fs",
        axis.key, len(points), len(cases), seconds,
    )
    return SweepResult(
        axis=axis, points=points, case_count=len(cases), solves=solves,
        seconds=seconds, pinned=pinned, cancelled=cancelled,
    )


def run_surface(
    source,
    x_axis: Axis,
    y_axis: Axis,
    output: Output,
    case: Case | None = None,
    solver: str = _DEFAULT_SOLVER,
    solver_configuration: dict | None = None,
    *,
    stride: int = 1,
    should_cancel: Callable[[], bool] | None = None,
) -> SurfaceResult:
    """One output over two varied properties -- a sweep in both directions
    at once, which is exactly as expensive as that sounds.

    `stride` takes every nth point of both axes, which is how a coarse
    pass happens here: stride 4 on a 52x24 grid is 78 solves rather than
    1248, and the cells it returns carry their real row/col so a finer
    pass overwrites them in place.
    """
    started = time.monotonic()
    ground = _ground(source, [case.scenario_id] if case else [])
    scenario = _scenario_of(ground, case.scenario_id if case else None)
    overrides = case.overrides if case else {}

    cells: list[Cell] = []
    cancelled = False
    for row in range(0, y_axis.count, stride):
        if should_cancel is not None and should_cancel():
            cancelled = True
            break
        y = y_axis.at(row)
        for col in range(0, x_axis.count, stride):
            x = x_axis.at(col)
            solved, error = _solve(
                ground, scenario, overrides,
                {x_axis.mote_id: {x_axis.key: x}, y_axis.mote_id: {y_axis.key: y}},
                solver, solver_configuration,
            )
            cells.append(Cell(
                row=row, col=col, x=x, y=y,
                value=solved.value(*output) if solved is not None else None,
                error=error,
            ))

    seconds = time.monotonic() - started
    logger.info("surfaced %s cells in %.3fs", len(cells), seconds)
    return SurfaceResult(
        x_axis=x_axis, y_axis=y_axis, cells=cells, output=output,
        solves=len(cells), seconds=seconds, cancelled=cancelled,
    )


def run_correlation(
    source,
    x_key: str,
    y_key: str,
    case: Case | None = None,
    solver: str = _DEFAULT_SOLVER,
    solver_configuration: dict | None = None,
) -> list[Dot]:
    """One dot per mote carrying both properties, after a single solve.

    Not a sweep at all -- nothing is varied. The solve is there so a mote
    whose y is a freedom shows its solved value rather than a stale one or
    none, and so knobs and scenarios still move the picture. One solve, N
    dots, regardless of how many motes the garden holds.
    """
    ground = _ground(source, [case.scenario_id] if case else [])
    scenario = _scenario_of(ground, case.scenario_id if case else None)
    solved, error = _solve(
        ground, scenario, case.overrides if case else {}, {},
        solver, solver_configuration,
    )
    if solved is None:
        raise SimulationError(error or "the solve failed")

    dots = []
    for mote_id, properties in ground.properties.items():
        x, y = solved.value(mote_id, x_key), solved.value(mote_id, y_key)
        if x is None or y is None:
            continue
        name = properties.get("name")
        dots.append(Dot(
            mote_id=mote_id,
            name=name if isinstance(name, str) else mote_id[:8],
            x=x, y=y,
        ))
    return sorted(dots, key=lambda d: d.name.casefold())

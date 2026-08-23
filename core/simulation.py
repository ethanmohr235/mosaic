# core/simulation.py
"""Simulation: collect every rule's equations, solve them, and write the
results back onto the motes.

Solving happens in two stages. First a substitution loop (_reduce)
repeatedly picks off any equation that has come down to a single unknown
and feeds the answer back in; this is what lets a formula be nonlinear as
long as the nonlinear part involves values that are already known (`T2 ==
T1 * (p2/p1)**0.286`), and it means a garden of plain assignment-style
rules never touches a solver backend at all. Whatever's left -- a
still-linear system of several unknowns, or something genuinely nonlinear
in its unknowns -- is handed to a pluggable solver backend (see
core/solvers.py) chosen per run via `solver`/`solver_configuration`: the
default `"linear"` is exact and non-iterative but fails on genuine
nonlinearity, `"newton"` is iterative and can solve that.
"""
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone

from core import model_resolver
from core import rules as rules_repo
from core import solver_registry  # noqa: F401 -- registers built-in solver backends
from core import species as species_repo
from core.db import generate_uuid
from core.formula import (
    Context,
    Equation,
    FormulaError,
    NonlinearError,
    Unknown,
    evaluate_equation,
)
from core.dimensions import DimensionContext, check_equation
from core.models import Relationship
from core.motes import list_motes, update_mote
from core.providers import PropertyProvider
from core.relationships import list_relationships
from core.solvers import DeferredEquation, Remainder, SolverError, resolve_solver
from core.units import (
    UNITS_KEY,
    UnitError,
    dimension_of,
    from_base,
    parse_units_property,
    to_base,
)

logger = logging.getLogger(__name__)

_TOLERANCE = 1e-9
_DEFAULT_SOLVER = "linear"


class SimulationError(Exception):
    """A garden's rules could not be solved."""


@dataclass(frozen=True)
class Change:
    mote_id: str
    key: str
    old_value: float | None
    new_value: float


@dataclass(frozen=True)
class SimulationResult:
    run_id: str
    run_number: int
    values: dict[Unknown, float]
    changes: list[Change]
    equation_count: int
    unsolved: list[Unknown] = field(default_factory=list)
    dimension_warnings: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        motes = len({c.mote_id for c in self.changes})
        text = (
            f"Solved {len(self.values)} value{'' if len(self.values) == 1 else 's'} "
            f"across {motes} mote{'' if motes == 1 else 's'} "
            f"from {self.equation_count} equation{'' if self.equation_count == 1 else 's'} "
            f"(run #{self.run_number})."
        )
        if self.unsolved:
            text += f" {len(self.unsolved)} declared unknown(s) had no equation."
        if self.dimension_warnings:
            count = len(self.dimension_warnings)
            text += f" {count} dimensional warning{'' if count == 1 else 's'}."
        return text


@dataclass(frozen=True)
class StepResult:
    """One step of a time-based run -- the same shape as SimulationResult
    minus the audit-log fields (run_id/run_number), since only a
    committed run's *final* cumulative state used to get logged -- now
    every step does, see run_time_series()'s save parameter."""
    step: int
    values: dict[Unknown, float]
    changes: list[Change]
    equation_count: int
    unsolved: list[Unknown] = field(default_factory=list)

    @property
    def summary(self) -> str:
        motes = len({c.mote_id for c in self.changes})
        text = (
            f"Solved {len(self.values)} value{'' if len(self.values) == 1 else 's'} "
            f"across {motes} mote{'' if motes == 1 else 's'} "
            f"from {self.equation_count} equation{'' if self.equation_count == 1 else 's'} "
            f"(step {self.step})."
        )
        if self.unsolved:
            text += f" {len(self.unsolved)} declared unknown(s) had no equation."
        return text


@dataclass(frozen=True)
class TimeSeriesResult:
    """The result of run_time_series(): every step computed before a
    failure (if any) stopped the run early, so a user can still scrub to
    and commit an earlier good step rather than losing the whole run."""
    steps: list[StepResult]
    failed_at: int | None = None   # step index that raised, if the run stopped early
    error: str | None = None
    run_id: str | None = None      # set when commit=True or save=True and at least one step succeeded
    # Checked once against the starting garden rather than per step: the
    # rules and declared units are the same every step, only values move.
    dimension_warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _GardenView:
    """The whole garden read once, so evaluating N equations doesn't mean
    N round trips. Fine at this scale -- MonOSS is explicitly not built
    for millions of records."""
    properties: dict[str, dict]
    unknowns: dict[str, set[str]]
    out_edges: dict[tuple[str, str], list[str]]
    in_edges: dict[tuple[str, str], list[str]]
    units: dict[str, dict[str, str]] = field(default_factory=dict)


def run_simulation(
    conn: sqlite3.Connection,
    solver: str = _DEFAULT_SOLVER,
    solver_configuration: dict | None = None,
    strict_units: bool = False,
) -> SimulationResult:
    """Solve the garden and write the results back. Raises SimulationError
    (after logging the failed run) if the rules can't be satisfied.

    `solver` picks the backend that finishes whatever the substitution
    loop couldn't resolve on its own -- see core/solvers.py. The default,
    `"linear"`, is today's exact non-iterative behavior and fails on
    genuine nonlinearity; `"newton"` can solve that at the cost of being
    iterative/approximate.

    `strict_units` promotes a dimensional mismatch from a warning on the
    result to a SimulationError. Off by default: units are opt-in, so a
    garden that declares none (or only some) must still run exactly as it
    always has."""
    run_number = _next_run_number(conn)
    properties = {m.id: m.properties for m in list_motes(conn)}
    relationships = list_relationships(conn)
    view = _build_view(properties, relationships)
    pairs = _rule_instances(rules_repo.list_rules(conn), view)

    try:
        solved: dict[Unknown, float] = {}
        model_cache: dict[str, PropertyProvider] = {}
        _validate_units(view, pairs)
        warnings = _dimension_warnings(view, pairs, strict_units, model_cache)
        remainder = _reduce(view, pairs, solved, model_cache=model_cache)
        solved.update(_finish(view, remainder, solved, solver, solver_configuration))
    except SimulationError as e:
        _log_run(conn, run_number, "error", str(e), [], committed=False)
        logger.warning("simulation run #%s failed: %s", run_number, e)
        raise
    except SolverError as e:
        _log_run(conn, run_number, "error", str(e), [], committed=False)
        logger.warning("simulation run #%s failed: %s", run_number, e)
        raise SimulationError(str(e)) from e

    changes = _write_back(conn, view.properties, solved)
    run_id = _log_run(conn, run_number, "ok", None, [changes], committed=True)
    logger.info("simulation run #%s solved %s values", run_number, len(solved))
    return SimulationResult(
        run_id=run_id,
        run_number=run_number,
        values=solved,
        changes=changes,
        equation_count=len(remainder.open_equations) + len(remainder.deferred),
        unsolved=_unsolved(view, solved),
        dimension_warnings=warnings,
    )


def run_time_series(
    conn: sqlite3.Connection,
    step_count: int,
    commit: bool,
    save: bool = False,
    solver: str = _DEFAULT_SOLVER,
    solver_configuration: dict | None = None,
    strict_units: bool = False,
) -> TimeSeriesResult:
    """Runs step_count steps entirely in memory, each step's solve
    starting from the *previous* step's solved values rather than
    re-reading the database -- this is what lets a rule use prev(x) to
    express a recurrence. `solver`/`solver_configuration` are the same
    per-run backend choice run_simulation() takes, applied to every step.

    Three mutually exclusive modes:
    - commit=False, save=False (default): a pure preview. Nothing is
      written or logged.
    - commit=True: writes the *cumulative* result (start state -> however
      far the run got) to the graph, and logs every computed step
      individually (see _log_run) so it can later be scrubbed through
      via run_ticks() -- exactly like an ordinary run_simulation() call,
      just with more than one tick.
    - save=True: logs every computed step individually, same as commit,
      but never writes the graph -- for keeping a hypothetical run (e.g.
      one of several parameter sets being compared) around for later
      review without it becoming the garden's canonical state.

    A step that fails to solve stops the run there but keeps every step
    computed before it (see TimeSeriesResult.failed_at/error), so a
    caller can still commit or save an earlier successful step rather
    than losing the whole run.

    `strict_units` is the same opt-in run_simulation() takes. It is
    checked once up front, before any step runs, and raises rather than
    being captured as a failed step -- nothing about it can change
    part-way through a run."""
    if commit and save:
        raise ValueError("commit and save are mutually exclusive")
    if step_count < 1:
        raise ValueError("step_count must be at least 1")

    relationships = list_relationships(conn)
    rules = rules_repo.list_rules(conn)
    baseline = {m.id: dict(m.properties) for m in list_motes(conn)}
    running = {mid: dict(props) for mid, props in baseline.items()}
    cumulative: dict[Unknown, float] = {}
    # Built once and reused across every step: the `uses_model` graph and
    # provider configuration don't change step-to-step (only solved
    # values do), so a provider should be constructed at most once per
    # run rather than re-resolved on every step.
    model_cache: dict[str, PropertyProvider] = {}

    steps: list[StepResult] = []
    failed_at: int | None = None
    error: str | None = None

    # Rules and declared units don't change between steps, so this is
    # checked once against the starting state rather than N times.
    starting_view = _build_view(running, relationships)
    starting_pairs = _rule_instances(rules, starting_view)
    _validate_units(starting_view, starting_pairs)
    warnings = _dimension_warnings(
        starting_view, starting_pairs, strict_units, model_cache
    )

    for step in range(step_count):
        view = _build_view(running, relationships)
        pairs = _rule_instances(rules, view)
        try:
            solved: dict[Unknown, float] = {}
            remainder = _reduce(
                view, pairs, solved, tick=float(step), previous=running, model_cache=model_cache
            )
            solved.update(_finish(view, remainder, solved, solver, solver_configuration))
        except (SimulationError, SolverError) as e:
            failed_at, error = step, str(e)
            logger.warning("simulation preview step %s failed: %s", step, e)
            break

        changes = _changes_for(view.properties, solved)
        cumulative.update(solved)
        # The running state deliberately mirrors the database's own
        # representation -- values in whatever unit each mote declares,
        # not the solver's base units. Every step re-reads it through the
        # same normalization an ordinary property read gets, so storing
        # base-unit values here would convert them a second time.
        next_running = {mid: dict(props) for mid, props in running.items()}
        for mote_id, new_properties in _by_mote(view.properties, solved).items():
            next_running.setdefault(mote_id, {}).update(new_properties)
        running = next_running
        equation_count = len(remainder.open_equations) + len(remainder.deferred)
        steps.append(StepResult(step, solved, changes, equation_count, _unsolved(view, solved)))

    run_id = None
    if (commit or save) and steps:
        run_number = _next_run_number(conn)
        if commit:
            _write_back(conn, baseline, cumulative)
        run_id = _log_run(conn, run_number, "ok", None, [s.changes for s in steps], committed=commit)
        logger.info(
            "simulation time series %s %s steps, %s values",
            "committed" if commit else "saved", len(steps), len(cumulative),
        )

    return TimeSeriesResult(steps, failed_at, error, run_id, warnings)


def _build_view(
    properties: dict[str, dict], relationships: list[Relationship]
) -> _GardenView:
    """Pure -- takes properties/relationships already read, rather than a
    connection, so a multi-step run can rebuild the view from its own
    in-memory state each step without hitting the database again."""
    out_edges: dict[tuple[str, str], list[str]] = {}
    in_edges: dict[tuple[str, str], list[str]] = {}
    unknowns: dict[str, set[str]] = {}

    for rel in relationships:
        out_edges.setdefault((rel.from_id, rel.type), []).append(rel.to_id)
        in_edges.setdefault((rel.to_id, rel.type), []).append(rel.from_id)
        if rel.type != species_repo.INSTANCE_OF:
            continue
        declared = rules_repo.parse_unknowns(
            properties.get(rel.to_id, {}).get(rules_repo.UNKNOWNS_KEY)
        )
        if declared:
            unknowns.setdefault(rel.from_id, set()).update(declared)

    return _GardenView(properties, unknowns, out_edges, in_edges, _units_for(properties))


def _units_for(properties: dict[str, dict]) -> dict[str, dict[str, str]]:
    """Each mote's declared key -> unit map, read once per view rather
    than re-parsed on every property lookup. Motes declaring nothing are
    left out entirely, so an un-annotated garden carries no extra state."""
    declared = {}
    for mote_id, props in properties.items():
        parsed = parse_units_property(props.get(UNITS_KEY))
        if parsed:
            declared[mote_id] = parsed
    return declared


def _declared_units(properties: dict[str, dict], mote_id: str) -> dict[str, str]:
    """One mote's declared units, straight from a properties dict.

    Taken from the properties rather than a _GardenView so the write-back
    and change-logging paths -- which work against a plain baseline dict,
    not a view -- can reach the same information without threading an
    extra argument through."""
    return parse_units_property(properties.get(mote_id, {}).get(UNITS_KEY))


def _in_declared_units(properties: dict[str, dict], mote_id: str, key: str, value: float) -> float:
    """A solved (base-unit) value expressed back in whatever unit the mote
    declares for that key.

    Solving a property must never silently change the unit it is recorded
    in: a resistance the user chose to keep in kohm stays in kohm. With no
    declared unit the base value is written as-is, which is exactly what
    happened before units existed.
    """
    unit_name = _declared_units(properties, mote_id).get(key)
    if not unit_name:
        return value
    try:
        return from_base(value, unit_name)
    except UnitError as e:
        raise SimulationError(
            f"mote {mote_id[:8]} declares unit {unit_name!r} for {key!r}: {e}"
        ) from e


def _rule_instances(
    rules: list[rules_repo.Rule], view: _GardenView
) -> list[tuple[rules_repo.Rule, str]]:
    """Every (rule, instance) pair the run will evaluate. Rules and
    relationships never change mid-run (only property values do), so the
    caller computes `rules` once and reuses it across every step."""
    pairs = []
    for rule in rules:
        for mote_id in view.in_edges.get((rule.species_id, species_repo.INSTANCE_OF), []):
            pairs.append((rule, mote_id))
    return pairs


def _context(
    view: _GardenView,
    mote_id: str,
    solved: dict[Unknown, float],
    tick: float = 0.0,
    previous: dict[str, dict] | None = None,
    *,
    model_cache: dict[str, PropertyProvider],
) -> Context:
    """tick/previous default to values that make a plain run_simulation()
    call behave exactly as it always has: t reads as 0, and prev(x) is
    simply unavailable (there is no "previous step" for a one-shot
    solve). model_cache is mandatory and keyword-only -- it's the same
    dict for every rule/mote pair and every substitution pass within one
    run (see run_simulation()/run_time_series()), which is what lets a
    provider be constructed once per run rather than once per formula
    evaluation."""
    return Context(
        mote_id=mote_id,
        properties=lambda mid: view.properties.get(mid, {}),
        unknown_keys=lambda mid: view.unknowns.get(mid, set()),
        targets=lambda mid, type_: view.out_edges.get((mid, type_), []),
        sources=lambda mid, type_: view.in_edges.get((mid, type_), []),
        solved=solved,
        tick=tick,
        previous=lambda mid: (previous or {}).get(mid, {}),
        evaluate_model=lambda mid, relationship_type, operation, args: _evaluate_model(
            view, model_cache, mid, relationship_type, operation, args
        ),
        units=lambda mid: view.units.get(mid, {}),
    )


def _evaluate_model(
    view: _GardenView,
    cache: dict[str, PropertyProvider],
    mote_id: str,
    relationship_type: str,
    operation: str,
    args: list[float],
) -> float:
    """Backs the property() formula builtin. Navigates relationship_type
    from mote_id the same way targets() does, resolves (and caches) that
    single target's property model, and evaluates the requested
    operation. Every error case here is a FormulaError subclass (see
    core/providers.py, core/model_resolver.py), so it flows straight
    through _evaluate_all()'s existing `except FormulaError` handling
    with no changes needed there."""
    target_ids = view.out_edges.get((mote_id, relationship_type), [])
    if len(target_ids) != 1:
        raise FormulaError(
            f"property() needs exactly one mote via {relationship_type!r} from "
            f"mote {mote_id[:8]}, found {len(target_ids)}"
        )
    target_id = target_ids[0]
    if target_id not in cache:
        cache[target_id] = model_resolver.resolve(
            target_id,
            lambda mid: view.properties.get(mid, {}),
            lambda mid, type_: view.out_edges.get((mid, type_), []),
        )
    provider = cache[target_id]
    capability = provider.capability(operation)
    if len(args) != len(capability.inputs):
        raise FormulaError(
            f"{operation}({', '.join(capability.inputs)}) takes {len(capability.inputs)} "
            f"argument(s), got {len(args)}"
        )
    return provider.evaluate(operation, dict(zip(capability.inputs, args)))


def _capability_units(
    view: _GardenView,
    cache: dict[str, PropertyProvider],
    mote_id: str,
    relationship_type: str,
    operation: str,
) -> str | None:
    """The unit a property model declares for one operation's output, for
    the dimension checker. Every failure -- no model, no such operation,
    no declared unit -- is simply "unknown", because a rule shouldn't be
    unverifiable just because a provider didn't annotate itself."""
    target_ids = view.out_edges.get((mote_id, relationship_type), [])
    if len(target_ids) != 1:
        return None
    target_id = target_ids[0]
    try:
        if target_id not in cache:
            cache[target_id] = model_resolver.resolve(
                target_id,
                lambda mid: view.properties.get(mid, {}),
                lambda mid, type_: view.out_edges.get((mid, type_), []),
            )
        return cache[target_id].capability(operation).units
    except FormulaError:
        return None


def check_dimensions(
    view: _GardenView,
    pairs: list[tuple[rules_repo.Rule, str]],
    model_cache: dict[str, PropertyProvider] | None = None,
) -> list[str]:
    """Every dimensional problem in the rules about to be run, each
    labelled with the rule and instance it was found on.

    Costs one extra AST walk per (rule, instance) pair -- cheap next to
    solving, and it runs before any value is written, so a mismatch can
    be reported against the garden as the user left it.
    """
    cache = model_cache if model_cache is not None else {}
    problems: list[str] = []
    for rule, mote_id in pairs:
        ctx = DimensionContext(
            mote_id=mote_id,
            units=lambda mid: view.units.get(mid, {}),
            targets=lambda mid, type_: view.out_edges.get((mid, type_), []),
            sources=lambda mid, type_: view.in_edges.get((mid, type_), []),
            capability_units=lambda mid, rel, op: _capability_units(view, cache, mid, rel, op),
            label=lambda mid: _mote_label(view, mid),
        )
        for problem in check_equation(rule.equation, ctx):
            problems.append(f"{rule.name} on {_mote_label(view, mote_id)}: {problem}")
    return problems


@dataclass(frozen=True)
class DimensionReport:
    """The result of checking a garden's units without solving it."""
    pairs_checked: int
    problems: list[str]


def check_garden(conn: sqlite3.Connection, rule_id: str | None = None) -> DimensionReport:
    """Check a garden's rules for dimensional consistency, writing
    nothing and solving nothing.

    The same check a run performs, available on its own so units can be
    sorted out before any value is touched. `rule_id` narrows it to a
    single rule mote."""
    properties = {m.id: m.properties for m in list_motes(conn)}
    view = _build_view(properties, list_relationships(conn))
    rules = rules_repo.list_rules(conn)
    if rule_id is not None:
        rules = [rule for rule in rules if rule.mote.id == rule_id]
    pairs = _rule_instances(rules, view)
    return DimensionReport(len(pairs), check_dimensions(view, pairs))


def _mote_label(view: _GardenView, mote_id: str) -> str:
    """A mote's name if it has one -- a units warning naming 'R1' is worth
    much more than one naming a UUID prefix."""
    name = view.properties.get(mote_id, {}).get("name")
    return str(name) if isinstance(name, str) and name.strip() else mote_id[:8]


def _validate_units(
    view: _GardenView, pairs: list[tuple[rules_repo.Rule, str]]
) -> None:
    """Reject unrecognized unit names before solving starts.

    Only the motes this run actually solves are checked. A solved value
    is converted back into its mote's declared unit on write-back, and
    that conversion happens after the run's error handling -- so an
    unrecognized name has to be caught here, while a failure can still be
    reported and logged like any other. Motes merely *read* by a formula
    are already covered: core/formula.py raises when it can't normalize
    one, through the ordinary error path.

    Scoped rather than garden-wide on purpose: units are opt-in, and a
    typo on some unrelated mote shouldn't fail a simulation that never
    touches it.
    """
    problems = []
    for mote_id in sorted({mote_id for _, mote_id in pairs}):
        for key, unit_name in sorted(view.units.get(mote_id, {}).items()):
            try:
                dimension_of(unit_name)
            except UnitError as e:
                problems.append(f"{_mote_label(view, mote_id)}.{key}: {e}")
    if problems:
        raise SimulationError("unrecognized unit(s):\n  " + "\n  ".join(problems))


def _dimension_warnings(
    view: _GardenView,
    pairs: list[tuple[rules_repo.Rule, str]],
    strict: bool,
    model_cache: dict[str, PropertyProvider],
) -> list[str]:
    """Shared pre-flight for both run shapes. Warnings are advisory by
    default -- units are opt-in, and a half-annotated garden must still
    run -- and only become an error when the caller explicitly asks."""
    problems = check_dimensions(view, pairs, model_cache)
    if problems and strict:
        raise SimulationError(
            "dimensional check failed:\n  " + "\n  ".join(problems)
        )
    for problem in problems:
        logger.warning("dimension check: %s", problem)
    return problems


def _reduce(
    view: _GardenView,
    pairs: list[tuple[rules_repo.Rule, str]],
    solved: dict[Unknown, float],
    tick: float = 0.0,
    previous: dict[str, dict] | None = None,
    *,
    model_cache: dict[str, PropertyProvider],
) -> Remainder:
    """Substitution loop. Repeatedly evaluates every pair, picks off any
    equation that comes down to a single unknown, and feeds the answer
    back in -- exact and free of iteration error for anything expressible
    this way, and the only stage every solver backend shares. Whatever's
    left (still-linear multi-unknown equations, or equations genuinely
    nonlinear in their unknowns) is packaged into a Remainder for the
    selected backend (see core/solvers.py) to finish."""
    equations: list[Equation] = []
    deferred: list[DeferredEquation] = []

    while True:
        equations, deferred = _evaluate_all(view, pairs, solved, tick, previous, model_cache=model_cache)
        newly = _solve_singletons(equations, solved)
        if not newly:
            break
        solved.update(newly)

    return Remainder(equations, deferred)


def _evaluate_all(
    view: _GardenView,
    pairs: list[tuple[rules_repo.Rule, str]],
    solved: dict[Unknown, float],
    tick: float = 0.0,
    previous: dict[str, dict] | None = None,
    *,
    model_cache: dict[str, PropertyProvider],
) -> tuple[list[Equation], list[DeferredEquation]]:
    """Nonlinear failures are deferred rather than fatal -- the offending
    unknown may still be determined by another equation, which would make
    this one evaluable on a later pass."""
    equations, deferred = [], []
    for rule, mote_id in pairs:
        try:
            ctx = _context(view, mote_id, solved, tick, previous, model_cache=model_cache)
            equations.append(evaluate_equation(rule.equation, ctx))
        except NonlinearError as e:
            deferred.append(_defer(view, rule, mote_id, solved, tick, previous, model_cache, e))
        except FormulaError as e:
            raise SimulationError(f"rule {rule.name!r} on mote {mote_id[:8]}: {e}") from e
    return equations, deferred


class _RecordingKnown(dict):
    """A dict that records every key actually looked up via `in`, without
    any change to core/formula.py's _lookup() -- which checks membership
    on Context.solved exactly that way. Used once per deferred equation
    to discover which unknowns it actually references (see _defer), not
    on the hot per-iteration path a solver backend runs."""

    def __init__(self, base: dict):
        super().__init__(base)
        self.touched: set[Unknown] = set()

    def __contains__(self, key) -> bool:
        self.touched.add(key)
        return super().__contains__(key)


def _defer(
    view: _GardenView,
    rule: rules_repo.Rule,
    mote_id: str,
    solved: dict[Unknown, float],
    tick: float,
    previous: dict[str, dict] | None,
    model_cache: dict[str, PropertyProvider],
    error: NonlinearError,
) -> DeferredEquation:
    """Builds the DeferredEquation for one rule/mote pair whose equation
    is genuinely nonlinear in its unknowns. `references` -- which
    unknowns the equation actually touches, needed so a solver backend
    doesn't mistake an unrelated declared-but-unsolved unknown for one it
    must also pin down -- is discovered by one throwaway evaluation with
    a placeholder guess (1.0) for every currently unsolved declared
    unknown in the whole view, recorded via _RecordingKnown. If that
    probe itself fails (e.g. a placeholder isn't a valid input to a
    property() model), fall back to the full unsolved set rather than
    under-counting a real dependency."""
    all_unsolved = {
        (mid, key)
        for mid, keys in view.unknowns.items()
        for key in keys
        if (mid, key) not in solved
    }
    placeholder = _RecordingKnown(solved)
    for unknown in all_unsolved:
        dict.setdefault(placeholder, unknown, 1.0)
    try:
        ctx = _context(view, mote_id, placeholder, tick, previous, model_cache=model_cache)
        evaluate_equation(rule.equation, ctx)
        references = frozenset(placeholder.touched) & all_unsolved
    except FormulaError:
        references = frozenset(all_unsolved)

    return DeferredEquation(
        rule_name=rule.name,
        mote_id=mote_id,
        source=rule.equation,
        detail=str(error),
        references=references,
        reevaluate=lambda guess, rule=rule, mote_id=mote_id: evaluate_equation(
            rule.equation,
            _context(view, mote_id, guess, tick, previous, model_cache=model_cache),
        ),
    )


def _solve_singletons(
    equations: list[Equation], known: dict[Unknown, float]
) -> dict[Unknown, float]:
    """Any equation down to `c*x + k == 0` determines x outright."""
    newly: dict[Unknown, float] = {}
    for equation in equations:
        if len(equation.terms) != 1:
            continue
        (unknown, coefficient), = equation.terms.items()
        if unknown in newly or unknown in known or abs(coefficient) < _TOLERANCE:
            continue
        # `+ 0.0` normalizes IEEE negative zero, so a grounded node reads
        # `voltage: 0.0` rather than `-0.0`.
        newly[unknown] = -equation.constant / coefficient + 0.0
    return newly


def _finish(
    view: _GardenView,
    remainder: Remainder,
    known: dict[Unknown, float],
    solver: str,
    solver_configuration: dict | None,
) -> dict[Unknown, float]:
    """Resolves and runs the selected backend (see core/solvers.py) on
    whatever _reduce() couldn't finish alone. `candidates` is deliberately
    narrower than "every declared-but-unsolved unknown" -- it's only the
    ones some equation actually references, so a declared unknown with no
    equation at all keeps surfacing softly via _unsolved() under every
    solver, exactly as it always has, rather than becoming a spurious
    underdetermined failure."""
    candidates = {u for e in remainder.open_equations for u in e.terms}
    for deferred in remainder.deferred:
        candidates |= deferred.references
    backend = resolve_solver(solver)(solver_configuration or {})
    return backend.solve(remainder, known, candidates, _initial_guess(view))


def _initial_guess(view: _GardenView) -> Callable[[Unknown], float]:
    """An iterative solver backend needs a starting value for each
    unknown it has no other information about. `view.properties` already
    holds the right thing to start from either way: for a one-shot
    run_simulation() it's the value freshly read from the garden, and for
    a run_time_series() step it's the state entering that step (view is
    rebuilt from `running` each step) -- so reading it here is a warm
    start from the previous step for free, with no separate plumbing.

    The guess is normalized the same way a formula read is: the solver
    works in base units, so a `resistance` stored as 4.7 kohm has to
    start from 4700, not 4.7."""
    def guess(unknown: Unknown) -> float:
        mote_id, key = unknown
        value = view.properties.get(mote_id, {}).get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            unit_name = view.units.get(mote_id, {}).get(key)
            if not unit_name:
                return float(value)
            try:
                return to_base(float(value), unit_name)
            except UnitError:
                # A bad unit name fails the run properly when the formula
                # layer reads it; a starting guess isn't the place to
                # raise, so fall back to the unconverted value.
                return float(value)
        return 1.0
    return guess


def _changes_for(properties: dict[str, dict], values: dict[Unknown, float]) -> list[Change]:
    """Old/new pairs for a set of solved values against a baseline
    properties dict -- pure, no DB write. Shared by every step of a
    preview (which never writes) and by the final commit (which does).

    Solved values arrive in base units; old values are whatever the mote
    stores. Both sides are put in the mote's own declared unit so a change
    reads as a real change rather than a unit conversion."""
    by_mote = _by_mote(properties, values)

    changes = []
    for mote_id, new_properties in sorted(by_mote.items()):
        existing = properties.get(mote_id, {})
        for key, value in sorted(new_properties.items()):
            old = existing.get(key)
            changes.append(
                Change(mote_id, key, old if isinstance(old, (int, float)) else None, value)
            )
    return changes


def _by_mote(
    properties: dict[str, dict], values: dict[Unknown, float]
) -> dict[str, dict[str, float]]:
    """Solved values regrouped per mote and converted out of base units
    into whatever unit each mote declares -- the one place the solver's
    internal representation is turned back into the garden's."""
    grouped: dict[str, dict[str, float]] = {}
    for (mote_id, key), value in values.items():
        grouped.setdefault(mote_id, {})[key] = _in_declared_units(
            properties, mote_id, key, value
        )
    return grouped


def _write_back(
    conn: sqlite3.Connection, properties: dict[str, dict], values: dict[Unknown, float]
) -> list[Change]:
    changes = _changes_for(properties, values)
    by_mote = _by_mote(properties, values)
    for mote_id, new_properties in sorted(by_mote.items()):
        update_mote(conn, mote_id, new_properties)
    return changes


def _unsolved(view: _GardenView, solved: dict[Unknown, float]) -> list[Unknown]:
    declared = {(mote_id, key) for mote_id, keys in view.unknowns.items() for key in keys}
    return sorted(declared - set(solved))


def _next_run_number(conn: sqlite3.Connection) -> int:
    """Derived rather than stored -- the log already knows how many runs
    this garden has seen."""
    cursor = conn.cursor()
    cursor.execute("SELECT COALESCE(MAX(run_number), 0) + 1 AS next FROM simulation_runs")
    return int(cursor.fetchone()["next"])


def _log_run(
    conn: sqlite3.Connection,
    run_number: int,
    status: str,
    detail: str | None,
    step_changes: list[list[Change]],
    committed: bool,
) -> str:
    """step_changes[i] is tick i's own changes (0-indexed, oldest first).
    A steady-state run logs a single tick; a committed or saved
    time-series run logs one per computed step, so a later run_ticks()
    can scrub through exactly what happened tick by tick. `committed`
    records whether these values were actually written to the graph
    (core/db.py's simulation_runs.committed) -- a saved-only run (see
    run_time_series's save parameter) never touches motes/mote_properties
    at all, only this log."""
    run_id = generate_uuid()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO simulation_runs (id, run_number, ran_at, status, detail, committed) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (run_id, run_number, datetime.now(timezone.utc).isoformat(), status, detail, int(committed)),
    )
    cursor.executemany(
        "INSERT INTO simulation_changes (run_id, step, mote_id, key, old_value, new_value) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
            (run_id, step, c.mote_id, c.key, _text(c.old_value), _text(c.new_value))
            for step, changes in enumerate(step_changes)
            for c in changes
        ],
    )
    conn.commit()
    return run_id


def _text(value: float | None) -> str | None:
    return None if value is None else repr(value)


def _parse(value: str | None) -> float | None:
    return None if value is None else float(value)


@dataclass(frozen=True)
class RunRecord:
    """One logged row from simulation_runs (core/db.py) -- every run is
    kept, successful or not, since a failed run is exactly the one worth
    looking back at. change_count is folded in via a join so listing runs
    doesn't need a separate query per row -- counted as distinct
    (mote_id, key) pairs touched overall, not raw simulation_changes rows,
    since a multi-tick run has one row per property per tick."""
    run_id: str
    run_number: int
    ran_at: str
    status: str
    detail: str | None
    committed: bool
    change_count: int


_RUN_COLUMNS = (
    "r.id AS id, r.run_number AS run_number, r.ran_at AS ran_at, r.status AS status, "
    "r.detail AS detail, r.committed AS committed, "
    "COUNT(DISTINCT c.mote_id || ':' || c.key) AS change_count"
)


def _row_to_run(row: sqlite3.Row) -> RunRecord:
    return RunRecord(
        run_id=row["id"], run_number=row["run_number"], ran_at=row["ran_at"],
        status=row["status"], detail=row["detail"], committed=bool(row["committed"]),
        change_count=row["change_count"],
    )


def list_runs(conn: sqlite3.Connection, limit: int | None = None) -> list[RunRecord]:
    """Every logged run, most recent first."""
    query = (
        f"SELECT {_RUN_COLUMNS} FROM simulation_runs r "
        "LEFT JOIN simulation_changes c ON c.run_id = r.id "
        "GROUP BY r.id ORDER BY r.ran_at DESC"
    )
    params: tuple = ()
    if limit is not None:
        query += " LIMIT ?"
        params = (limit,)
    cursor = conn.cursor()
    cursor.execute(query, params)
    return [_row_to_run(row) for row in cursor.fetchall()]


def get_run(conn: sqlite3.Connection, run_id: str) -> RunRecord | None:
    cursor = conn.cursor()
    cursor.execute(
        f"SELECT {_RUN_COLUMNS} FROM simulation_runs r "
        "LEFT JOIN simulation_changes c ON c.run_id = r.id "
        "WHERE r.id = ? GROUP BY r.id",
        (run_id,),
    )
    row = cursor.fetchone()
    return _row_to_run(row) if row else None


def run_changes(conn: sqlite3.Connection, run_id: str) -> list[Change]:
    """The net old->new change per property across a run's whole
    duration -- old_value from its earliest tick, new_value from its
    latest -- the same shape run_simulation() itself returns, just read
    back rather than freshly solved. For a single-tick run this is
    identical to that tick's own changes. See run_ticks() for the
    tick-by-tick breakdown instead of this folded net view."""
    cursor = conn.cursor()
    cursor.execute(
        "SELECT mote_id, key, old_value, new_value FROM simulation_changes "
        "WHERE run_id = ? ORDER BY step ASC, mote_id, key",
        (run_id,),
    )
    net: dict[tuple[str, str], Change] = {}
    for row in cursor.fetchall():
        key = (row["mote_id"], row["key"])
        new_value = _parse(row["new_value"])
        old_value = net[key].old_value if key in net else _parse(row["old_value"])
        net[key] = Change(row["mote_id"], row["key"], old_value, new_value)
    return sorted(net.values(), key=lambda c: (c.mote_id, c.key))


@dataclass(frozen=True)
class RunTick:
    """One tick within a logged run -- what "entering" a run in the UI
    scrubs through. A steady-state run has exactly one (step 0); a run
    committed or saved from an N-step time-series preview has N."""
    step: int
    changes: list[Change]
    summary: str


def run_ticks(conn: sqlite3.Connection, run_id: str) -> list[RunTick]:
    """A run's own internal ticks, oldest first -- unrelated to any other
    run in the garden's history."""
    cursor = conn.cursor()
    cursor.execute(
        "SELECT step, mote_id, key, old_value, new_value FROM simulation_changes "
        "WHERE run_id = ? ORDER BY step ASC, mote_id, key",
        (run_id,),
    )
    by_step: dict[int, list[Change]] = {}
    for row in cursor.fetchall():
        by_step.setdefault(row["step"], []).append(
            Change(row["mote_id"], row["key"], _parse(row["old_value"]), _parse(row["new_value"]))
        )
    return [
        RunTick(
            step=step, changes=changes,
            summary=f"Tick {step + 1} · {len(changes)} change{'' if len(changes) == 1 else 's'}",
        )
        for step, changes in sorted(by_step.items())
    ]


def mote_history(conn: sqlite3.Connection, mote_id: str) -> dict[str, list[float]]:
    """Every value a mote's properties have taken across the garden's
    *committed* run history, oldest first per key -- feeds the
    inspector's sparklines. Excludes saved-only (uncommitted) runs: those
    are hypothetical and were never this mote's real state, so mixing
    them in would misrepresent the trend."""
    cursor = conn.cursor()
    cursor.execute(
        "SELECT c.key AS key, c.new_value AS new_value FROM simulation_changes c "
        "JOIN simulation_runs r ON r.id = c.run_id "
        "WHERE c.mote_id = ? AND r.committed = 1 "
        "ORDER BY r.ran_at ASC, c.step ASC",
        (mote_id,),
    )
    history: dict[str, list[float]] = {}
    for row in cursor.fetchall():
        history.setdefault(row["key"], []).append(_parse(row["new_value"]))
    return history

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
import dataclasses
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone

from core import derivations as derivations_repo
from core import incidence as incidence_repo
from core import model_resolver
from core import roles as roles_repo
from core import rules as rules_repo
from core import scenarios as scenarios_repo
from core import solver_registry  # noqa: F401 -- registers built-in solver backends
from core import species as species_repo
from core.db import generate_uuid
from core.formula import (
    EQUALS,
    describe as describe_unknowns,
    Context,
    Equation,
    FormulaError,
    NonlinearError,
    Unknown,
    attribute_bases,
    evaluate_equation,
    relation_operator,
)
from core.dimensions import DimensionContext, check_equation, infer_unit
from core.entities import Relationship
from core.motes import list_motes, update_mote
from core.providers import PropertyProvider
from core.provenance import (
    MISSING_READ,
    PREVIOUS_READ,
    PROPERTY_READ,
    SOLVER,
    SUBSTITUTION,
    InputRead,
    Journal,
    Origin,
    Reads,
    RecordingSolved,
)
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
    scenario_id: str | None = None
    # Constraints the answer does not satisfy. Deliberately separate from
    # dimension_warnings: a violated constraint is a statement about the
    # *answer*, a dimensional mismatch one about the *model*, and folding
    # them together would make neither readable.
    constraint_violations: list[str] = field(default_factory=list)

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
            text += f" {len(self.unsolved)} declared freedom(s) had no equation."
        if self.dimension_warnings:
            count = len(self.dimension_warnings)
            text += f" {count} dimensional warning{'' if count == 1 else 's'}."
        if self.constraint_violations:
            count = len(self.constraint_violations)
            text += f" {count} constraint problem{'' if count == 1 else 's'}."
        if self.scenario_id is not None:
            text += " Nothing was written -- a scenario run is always hypothetical."
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
    constraint_violations: list[str] = field(default_factory=list)

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
            text += f" {len(self.unsolved)} declared freedom(s) had no equation."
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
    run_number: int | None = None  # this garden's own run counter, set alongside run_id

    @property
    def constraint_violations(self) -> list[str]:
        """Every step's constraint problems, each labelled with its step.

        Unlike the dimensional check, this cannot be done once up front:
        a constraint is about the values a step found, and those are what
        change from step to step."""
        return [
            f"step {step.step}: {problem}"
            for step in self.steps
            for problem in step.constraint_violations
        ]


@dataclass(frozen=True)
class _GardenView:
    """The whole garden read once, so evaluating N equations doesn't mean
    N round trips. Fine at this scale -- MonOSS is explicitly not built
    for millions of records."""
    properties: dict[str, dict]
    freedoms: dict[str, set[str]]
    out_edges: dict[tuple[str, str], list[str]]
    in_edges: dict[tuple[str, str], list[str]]
    units: dict[str, dict[str, str]] = field(default_factory=dict)
    roles: dict[str, dict[str, roles_repo.Role]] = field(default_factory=dict)
    # Every mote immediately wired to a given mote, any relationship type
    # collapsed together and either direction -- for connected-name
    # references (core/formula.py's Context.neighbors). A coarser index
    # than out_edges/in_edges on purpose: a name reference doesn't care
    # which relationship carried it, only that one did.
    neighbors: dict[str, list[str]] = field(default_factory=dict)


def step_payloads(steps: "list[StepResult] | list[RunTick]") -> list[dict]:
    """The canonical JSON-safe form of a run's steps, shared by every
    adapter that has to put them on a wire (monarch/api.py's JS bridge,
    mcp_server/server.py's tools) so the same object never reaches two
    surfaces in two shapes -- which it previously did, one of them
    silently missing equation_count.

    Lives here rather than in an adapter because the reason this
    conversion exists is a property of the data: StepResult.values and
    .unsolved are keyed by (mote_id, key) tuples, which JSON has no way
    to represent. `changes` already carries everything a caller needs,
    and unsolved counts stay prose-only inside each step's summary.

    RunTick (a logged run read back) has no equation_count -- the log
    records what changed, not how many equations produced it -- so that
    key, and constraint_violations with it, are present only for a
    freshly solved StepResult."""
    payloads = []
    for step in steps:
        payload = {
            "step": step.step,
            "changes": [dataclasses.asdict(c) for c in step.changes],
            "summary": step.summary,
        }
        if isinstance(step, StepResult):
            payload["equation_count"] = step.equation_count
            payload["constraint_violations"] = list(step.constraint_violations)
        payloads.append(payload)
    return payloads


def run_simulation(
    conn: sqlite3.Connection,
    solver: str = _DEFAULT_SOLVER,
    solver_configuration: dict | None = None,
    strict_units: bool = False,
    scenario: str | None = None,
    strict_constraints: bool = False,
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
    always has.

    `scenario` names a mote gathering overrides (see core/scenarios.py).
    Solving under one **never writes to the graph** -- the run is logged
    like a saved one so it can be reviewed and compared, and the returned
    changes say what *would* happen. That is not a limitation to lift
    later: the answer to a hypothetical is not the garden's state, and
    the question of where a scenario run's results would be written has
    no good answer.

    `strict_constraints` promotes an unsatisfied constraint from a report
    on the result to a SimulationError, mirroring `strict_units`. Off by
    default for the same reason: a constraint says something about the
    answer, and seeing an answer that breaks a limit is often exactly why
    you ran it."""
    run_number = _next_run_number(conn)
    properties = {m.id: m.properties for m in list_motes(conn)}
    relationships = list_relationships(conn)
    case = _resolve_scenario(conn, scenario)
    view = _build_view(scenarios_repo.apply(properties, case), relationships)
    pairs = _rule_instances(rules_repo.list_rules(conn), view)
    equations, constraints = _split_relations(pairs)

    try:
        solved: dict[Unknown, float] = {}
        model_cache: dict[str, PropertyProvider] = {}
        journal = Journal()
        _validate_units(view, pairs)
        warnings = _dimension_warnings(view, pairs, strict_units, model_cache)
        reduction = _reduce(view, equations, solved, model_cache=model_cache, journal=journal)
        solved.update(
            _finish(view, reduction, solved, solver, solver_configuration, journal)
        )
        violations = _constraint_report(
            view, constraints, solved, strict_constraints, model_cache=model_cache
        )
    except SimulationError as e:
        _log_run(conn, run_number, "error", str(e), [], committed=False, scenario_id=scenario)
        logger.warning("simulation run #%s failed: %s", run_number, e)
        raise
    except SolverError as e:
        _log_run(conn, run_number, "error", str(e), [], committed=False, scenario_id=scenario)
        logger.warning("simulation run #%s failed: %s", run_number, e)
        raise SimulationError(str(e)) from e

    if case is None:
        inferred_units = _infer_missing_units(view, equations, solved, model_cache)
        properties = _apply_inferred_units(conn, view.properties, inferred_units)
        changes = _write_back(conn, properties, solved)
    else:
        changes = _changes_for(view.properties, solved)
    run_id = _log_run(
        conn, run_number, "ok", None, [changes],
        committed=case is None, step_journals=[journal], scenario_id=scenario,
    )
    logger.info("simulation run #%s solved %s values", run_number, len(solved))
    remainder = reduction.remainder
    return SimulationResult(
        run_id=run_id,
        run_number=run_number,
        values=solved,
        changes=changes,
        equation_count=len(remainder.open_equations) + len(remainder.deferred),
        unsolved=_unsolved(view, solved),
        dimension_warnings=warnings,
        scenario_id=scenario,
        constraint_violations=violations,
    )


def run_time_series(
    conn: sqlite3.Connection,
    step_count: int,
    commit: bool,
    save: bool = False,
    solver: str = _DEFAULT_SOLVER,
    solver_configuration: dict | None = None,
    strict_units: bool = False,
    scenario: str | None = None,
    strict_constraints: bool = False,
    detail: str | None = None,
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
    part-way through a run.

    `scenario` names a mote gathering overrides (see core/scenarios.py).
    It cannot be combined with commit=True: a scenario run is
    hypothetical by construction, and `commit` is an explicit request to
    make something the garden's state, so silently ignoring one of the
    two would be worse than refusing. save=True keeps the run for
    comparison, which is what a scenario is usually for.

    `detail` is an optional free-text note attached to the logged run
    (simulation_runs.detail, the same column a failed run's error message
    lives in) -- a caller can use it to mark runs of a kind a frontend
    wants to tell apart later, e.g. Motion's Fast mode tagging its own
    auto-commits so they can be filtered out of the Saved-runs list
    without losing them from the audit trail."""
    if commit and save:
        raise ValueError("commit and save are mutually exclusive")
    if commit and scenario is not None:
        raise ValueError(
            "a scenario run cannot be committed -- it answers what would happen, "
            "not what did. Use save=True to keep it for comparison."
        )
    if step_count < 1:
        raise ValueError("step_count must be at least 1")

    relationships = list_relationships(conn)
    rules = rules_repo.list_rules(conn)
    case = _resolve_scenario(conn, scenario)
    baseline = scenarios_repo.apply(
        {m.id: dict(m.properties) for m in list_motes(conn)}, case
    )
    running = {mid: dict(props) for mid, props in baseline.items()}
    cumulative: dict[Unknown, float] = {}
    # Built once and reused across every step: the `uses_model` graph and
    # provider configuration don't change step-to-step (only solved
    # values do), so a provider should be constructed at most once per
    # run rather than re-resolved on every step.
    model_cache: dict[str, PropertyProvider] = {}

    steps: list[StepResult] = []
    journals: list[Journal] = []
    failed_at: int | None = None
    error: str | None = None
    # A pure preview is discarded, so recording what it did would be work
    # nobody can ever read -- committing re-solves from the database
    # anyway (see the commit path below), and that solve is journalled.
    recording = commit or save

    # Rules and declared units don't change between steps, so this is
    # checked once against the starting state rather than N times.
    starting_view = _build_view(running, relationships)
    starting_pairs = _rule_instances(rules, starting_view)
    _validate_units(starting_view, starting_pairs)
    # Also the equations _infer_missing_units() below reads from -- rules
    # don't change step-to-step, so the split done here for the pre-flight
    # check is reused rather than repeated at commit time.
    starting_equations, _starting_constraints = _split_relations(starting_pairs)
    warnings = _dimension_warnings(
        starting_view, starting_pairs, strict_units, model_cache
    )

    for step in range(step_count):
        view = _build_view(running, relationships)
        equations, constraints = _split_relations(_rule_instances(rules, view))
        journal = Journal() if recording else None
        try:
            solved: dict[Unknown, float] = {}
            reduction = _reduce(
                view, equations, solved, tick=float(step), previous=running,
                model_cache=model_cache, journal=journal,
            )
            solved.update(
                _finish(view, reduction, solved, solver, solver_configuration, journal)
            )
            violations = _constraint_report(
                view, constraints, solved, strict_constraints,
                tick=float(step), previous=running, model_cache=model_cache,
            )
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
        remainder = reduction.remainder
        equation_count = len(remainder.open_equations) + len(remainder.deferred)
        steps.append(
            StepResult(
                step, solved, changes, equation_count,
                _unsolved(view, solved), violations,
            )
        )
        if journal is not None:
            journals.append(journal)

    run_id = run_number = None
    if (commit or save) and steps:
        run_number = _next_run_number(conn)
        if commit:
            inferred_units = _infer_missing_units(
                starting_view, starting_equations, cumulative, model_cache
            )
            baseline = _apply_inferred_units(conn, baseline, inferred_units)
            _write_back(conn, baseline, cumulative)
        run_id = _log_run(
            conn, run_number, "ok", detail, [s.changes for s in steps],
            committed=commit, step_journals=journals, scenario_id=scenario,
        )
        logger.info(
            "simulation time series %s %s steps, %s values",
            "committed" if commit else "saved", len(steps), len(cumulative),
        )

    return TimeSeriesResult(steps, failed_at, error, run_id, warnings, run_number)


def _build_view(
    properties: dict[str, dict], relationships: list[Relationship]
) -> _GardenView:
    """Pure -- takes properties/relationships already read, rather than a
    connection, so a multi-step run can rebuild the view from its own
    in-memory state each step without hitting the database again."""
    # Override motes hold replacement values for other motes, not real
    # state of their own, so they are left out of every run whether a
    # scenario is applied or not -- the same exclusion a template gets
    # from a species' instances. Their edges stay; only their properties
    # go, which is enough to keep them out of freedoms, roles and
    # write-back alike.
    properties = {
        mote_id: props
        for mote_id, props in properties.items()
        if mote_id not in scenarios_repo.override_mote_ids(relationships)
    }

    out_edges: dict[tuple[str, str], list[str]] = {}
    in_edges: dict[tuple[str, str], list[str]] = {}
    neighbor_sets: dict[str, set[str]] = {}

    for rel in relationships:
        out_edges.setdefault((rel.from_id, rel.type), []).append(rel.to_id)
        in_edges.setdefault((rel.to_id, rel.type), []).append(rel.from_id)
        neighbor_sets.setdefault(rel.from_id, set()).add(rel.to_id)
        neighbor_sets.setdefault(rel.to_id, set()).add(rel.from_id)

    acting_as_species = _acting_as_species(relationships)
    return _GardenView(
        properties,
        _freedoms_for(properties, relationships, acting_as_species),
        out_edges,
        in_edges,
        _units_for(properties),
        _roles_for(properties, relationships, acting_as_species),
        {mote_id: list(ids) for mote_id, ids in neighbor_sets.items()},
    )


def _acting_as_species(relationships: list[Relationship]) -> set[str]:
    """Motes other motes point at as a type -- the same test
    core/species.py's list_species uses.

    It decides how a declaration property is read: `freedoms` and `roles`
    on a mote acting as a species describe its *instances*, and on any
    other mote describe that mote itself. One rule for both, so a mote
    never has to guess which of its properties are about it.
    """
    return {
        rel.to_id for rel in relationships
        if rel.type in (species_repo.INSTANCE_OF, species_repo.TEMPLATE_OF)
    }


def _freedoms_for(
    properties: dict[str, dict],
    relationships: list[Relationship],
    acting_as_species: set[str],
) -> dict[str, set[str]]:
    """Each mote's effective freedoms -- the properties the solver may
    determine for it.

    A mote's own `freedoms` **replaces** what its species declare rather
    than adding to them, so a mote can narrow its freedoms as well as
    widen them. That is the whole point: the same turbine is sized in one
    problem (given power, find mass flow) and rated in another, and
    asking the second question should not mean editing what a turbine
    *is*. Freedoms declared only on species behave exactly as they always
    have.

    Several species merge, because nothing orders them.
    """
    freedoms: dict[str, set[str]] = {}
    for rel in relationships:
        if rel.type != species_repo.INSTANCE_OF:
            continue
        declared = rules_repo.parse_freedoms(
            properties.get(rel.to_id, {}).get(rules_repo.FREEDOMS_KEY)
        )
        if declared:
            freedoms.setdefault(rel.from_id, set()).update(declared)

    for mote_id, props in properties.items():
        if mote_id in acting_as_species:
            continue
        own = rules_repo.parse_freedoms(props.get(rules_repo.FREEDOMS_KEY))
        if own:
            freedoms[mote_id] = set(own)
    return freedoms


def _roles_for(
    properties: dict[str, dict],
    relationships: list[Relationship],
    acting_as_species: set[str],
) -> dict[str, dict[str, roles_repo.Role]]:
    """Each mote's effective declared roles.

    Read the same way freedoms are (see _acting_as_species): `roles` on a
    mote acting as a species declares what its *instances* have; on any
    other mote it declares what that mote itself has. That second reading
    is what lets a one-off assembly declare its parts without inventing a
    species with one instance.

    Unlike freedoms, an instance's own roles are *added* to its species',
    winning only on a name they share -- a part an assembly has in
    addition is far more common than one it must be denied.
    """
    declared: dict[str, dict[str, roles_repo.Role]] = {}
    for rel in relationships:
        if rel.type != species_repo.INSTANCE_OF:
            continue
        from_species = roles_repo.parse_roles(
            properties.get(rel.to_id, {}).get(roles_repo.ROLES_KEY)
        )
        if from_species:
            declared.setdefault(rel.from_id, {}).update(from_species)

    for mote_id, props in properties.items():
        if mote_id in acting_as_species:
            continue
        own = roles_repo.parse_roles(props.get(roles_repo.ROLES_KEY))
        if own:
            declared.setdefault(mote_id, {}).update(own)
    return declared


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


# How far a constraint's residual may sit on the wrong side of zero
# before it counts as violated. Non-strict comparisons get the benefit of
# it, since `efficiency <= 1` landing on exactly 1.0 through floating
# point should pass; strict ones do not, because `< 1` excluding 1 is the
# whole of what makes it strict.
def _satisfied(operator: str, difference: float) -> bool:
    if operator == "<=":
        return difference <= _TOLERANCE
    if operator == ">=":
        return difference >= -_TOLERANCE
    if operator == "<":
        return difference < 0.0
    return difference > 0.0


def _split_relations(
    pairs: list[tuple[rules_repo.Rule, str]]
) -> tuple[list[tuple[rules_repo.Rule, str]], list[tuple[rules_repo.Rule, str]]]:
    """Equations the solver may satisfy, and constraints it may only check.

    Sorted once per rule before anything is solved, so a constraint never
    reaches a solver backend at all. That is the structural form of "an
    inequality cannot determine a value": there is no code path by which
    one could.

    A rule that will not parse is left with the equations, so it fails
    through the ordinary path in _evaluate_all() with its name attached
    rather than being quietly filed as a constraint here.
    """
    operators: dict[str, str] = {}
    equations, constraints = [], []
    for rule, mote_id in pairs:
        if rule.mote.id not in operators:
            try:
                operators[rule.mote.id] = relation_operator(rule.equation)
            except FormulaError:
                operators[rule.mote.id] = EQUALS
        target = equations if operators[rule.mote.id] == EQUALS else constraints
        target.append((rule, mote_id))
    return equations, constraints


def _check_constraints(
    view: _GardenView,
    pairs: list[tuple[rules_repo.Rule, str]],
    solved: dict[Unknown, float],
    tick: float = 0.0,
    previous: dict[str, dict] | None = None,
    *,
    model_cache: dict[str, PropertyProvider],
) -> list[str]:
    """Which constraints the answer does not satisfy, as readable lines.

    Checked once, after solving, against the values that were actually
    found -- a constraint is a statement about an answer, so checking it
    before there is one would be meaningless.

    A constraint still referring to something unsolved is reported as
    *unchecked* rather than assumed satisfied. Silence would be
    indistinguishable from passing, and a limit nobody could evaluate is
    exactly the one worth mentioning.
    """
    problems = []
    for rule, mote_id in pairs:
        label = f"{rule.name} on {_mote_label(view, mote_id)}"
        try:
            ctx = _context(view, mote_id, solved, tick, previous, model_cache=model_cache)
            result = evaluate_equation(rule.equation, ctx)
        except NonlinearError as e:
            problems.append(f"{label}: not checked -- {e}")
            continue
        except FormulaError as e:
            raise SimulationError(f"rule {rule.name!r} on mote {mote_id[:8]}: {e}") from e

        if result.terms:
            problems.append(
                f"{label}: not checked -- depends on values this run did not determine "
                f"({describe_unknowns(result.terms)})"
            )
        elif not _satisfied(result.operator, result.constant):
            problems.append(
                f"{label}: {rule.equation} is not satisfied "
                f"(off by {abs(result.constant):g})"
            )
    return problems


def _constraint_report(
    view: _GardenView,
    pairs: list[tuple[rules_repo.Rule, str]],
    solved: dict[Unknown, float],
    strict: bool,
    tick: float = 0.0,
    previous: dict[str, dict] | None = None,
    *,
    model_cache: dict[str, PropertyProvider],
) -> list[str]:
    """Shared post-flight for both run shapes -- the constraint mirror of
    _dimension_warnings(). Advisory unless the caller asks otherwise."""
    problems = _check_constraints(
        view, pairs, solved, tick, previous, model_cache=model_cache
    )
    if problems and strict:
        raise SimulationError(
            "constraint check failed:\n  " + "\n  ".join(problems)
        )
    for problem in problems:
        logger.warning("constraint: %s", problem)
    return problems


def _rule_instances(
    rules: list[rules_repo.Rule], view: _GardenView
) -> list[tuple[rules_repo.Rule, str]]:
    """Every (rule, instance) pair the run will evaluate. Rules and
    relationships never change mid-run (only property values do), so the
    caller computes `rules` once and reuses it across every step."""
    pairs = []
    for rule in rules:
        if rule.scope == rules_repo.ONCE:
            # Asserted about one mote rather than a type. The mote need
            # not exist as anything special -- an assembly, a one-off
            # constant, a single machine nobody made a species for.
            pairs.append((rule, rule.target_id))
            continue
        for mote_id in view.in_edges.get((rule.target_id, species_repo.INSTANCE_OF), []):
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
    reads: Reads | None = None,
) -> Context:
    """tick/previous default to values that make a plain run_simulation()
    call behave exactly as it always has: t reads as 0, and prev(x) is
    simply unavailable (there is no "previous step" for a one-shot
    solve). model_cache is mandatory and keyword-only -- it's the same
    dict for every rule/mote pair and every substitution pass within one
    run (see run_simulation()/run_time_series()), which is what lets a
    provider be constructed once per run rather than once per formula
    evaluation.

    `reads` records what the evaluation actually looked at, for
    provenance (see core/provenance.py). It is off by default and stays
    off on every hot path -- the probe in _defer() and the per-iteration
    re-evaluation a solver backend drives -- so the values recorded are
    only ever ones a real evaluation used."""
    def properties(mid: str) -> dict:
        values = view.properties.get(mid, {})
        return values if reads is None else reads.properties(mid, values)

    def previous_properties(mid: str) -> dict:
        values = (previous or {}).get(mid, {})
        return values if reads is None else reads.previous(mid, values)

    def evaluate_model(
        mid: str, relationship_type: str, operation: str, args: list[float]
    ) -> float:
        value = _evaluate_model(view, model_cache, mid, relationship_type, operation, args)
        if reads is not None:
            reads.model(mid, operation, value)
        return value

    return Context(
        mote_id=mote_id,
        properties=properties,
        # The one place the modelling word becomes the solver's: a
        # mote's freedoms are exactly the keys a formula may treat as
        # unknowns on it.
        unknown_keys=lambda mid: view.freedoms.get(mid, set()),
        targets=lambda mid, type_: view.out_edges.get((mid, type_), []),
        sources=lambda mid, type_: view.in_edges.get((mid, type_), []),
        neighbors=lambda mid: view.neighbors.get(mid, []),
        solved=solved if reads is None else reads.solved(mote_id, solved),
        tick=tick,
        previous=previous_properties,
        evaluate_model=evaluate_model,
        units=lambda mid: view.units.get(mid, {}),
        roles=lambda mid: view.roles.get(mid, {}),
    )


def _normalizer(view: _GardenView) -> Callable[[str, str, float], float]:
    """Converts a stored value into its dimension's base unit for the
    provenance record, matching what core/formula.py's _normalize() does
    immediately after the same read.

    Deliberately non-raising: an unrecognized unit name fails the run
    through the real path (see _validate_units), and a recording wrapper
    is the wrong place to discover it -- raising here would turn a units
    typo into a provenance crash."""
    def normalize(mote_id: str, key: str, value: float) -> float:
        unit_name = view.units.get(mote_id, {}).get(key)
        if not unit_name:
            return value
        try:
            return to_base(value, unit_name)
        except UnitError:
            return value
    return normalize


def expression_context(
    properties: dict[str, dict],
    relationships: list[Relationship],
    mote_id: str,
    *,
    tick: float = 0.0,
    previous: dict[str, dict] | None = None,
) -> Context:
    """A Context for evaluating one expression against a garden that has
    already been read, for a caller that wants to *read* a value rather
    than pose a system to solve -- core/modoc.py's `\\val`. Built on
    _build_view/_context so a note sees exactly the edge maps, roles,
    units, reserved `t` and prev() semantics a rule sees, rather than a
    second copy of them that could drift.

    The view's freedoms are deliberately cleared. A property a mote
    declares free evaluates to a free variable, which is correct when
    there is a solver waiting for it and useless when there is not: a
    note has no equations to contribute and nowhere to send one.
    Cleared, every property resolves to the value it actually holds --
    which, at step N of a time series, is the value that step solved for
    it.
    """
    view = dataclasses.replace(_build_view(properties, relationships), freedoms={})
    return _context(view, mote_id, {}, tick, previous, model_cache={})


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


def unresolved_equation_names(
    conn: sqlite3.Connection,
    mote_id: str,
    equation: str,
    scenario: str | None = None,
) -> list[dict]:
    """Names `equation` references that aren't reachable from `mote_id`
    -- neither a declared role nor a mote already connected to it (see
    core/formula.py's connected-name resolution) -- but that do match
    some *other* mote's name somewhere in the garden. Each entry names
    the candidate mote id(s) an editor could offer to wire up a
    relationship to, turning an opaque "no property" failure into a
    one-click fix (docs/simulation.md's "Connected-name references").

    Read-only and does not evaluate the equation itself: a name that
    doesn't parse, isn't any mote's name, or is already reachable, is
    simply absent from the result rather than reported as a problem.
    Several candidates for one name means the garden has more than one
    mote by that name -- surfaced as-is, so a caller can list them rather
    than silently connecting to a guess."""
    properties = {m.id: m.properties for m in list_motes(conn)}
    relationships = list_relationships(conn)
    case = _resolve_scenario(conn, scenario)
    view = _build_view(scenarios_repo.apply(properties, case), relationships)

    role_names = set(view.roles.get(mote_id, {}))
    connected_names = {
        view.properties.get(mid, {}).get("name")
        for mid in view.neighbors.get(mote_id, [])
    }

    by_name: dict[str, list[str]] = {}
    for other_id, props in view.properties.items():
        if other_id == mote_id:
            continue
        name = props.get("name")
        if isinstance(name, str) and name:
            by_name.setdefault(name, []).append(other_id)

    results = []
    for name in attribute_bases(equation):
        if name in role_names or name in connected_names:
            continue
        candidates = by_name.get(name)
        if candidates:
            results.append({"name": name, "candidates": candidates})
    return results


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
        ctx = _dimension_context(view, cache, mote_id)
        for problem in check_equation(rule.equation, ctx):
            problems.append(f"{rule.name} on {_mote_label(view, mote_id)}: {problem}")
    return problems


def _dimension_context(
    view: _GardenView, model_cache: dict[str, PropertyProvider], mote_id: str
) -> DimensionContext:
    return DimensionContext(
        mote_id=mote_id,
        units=lambda mid: view.units.get(mid, {}),
        targets=lambda mid, type_: view.out_edges.get((mid, type_), []),
        sources=lambda mid, type_: view.in_edges.get((mid, type_), []),
        capability_units=lambda mid, rel, op: _capability_units(view, model_cache, mid, rel, op),
        label=lambda mid: _mote_label(view, mid),
        roles=lambda mid: view.roles.get(mid, {}),
        neighbors=lambda mid: view.neighbors.get(mid, []),
        mote_name=lambda mid: view.properties.get(mid, {}).get("name"),
    )


def _infer_missing_units(
    view: _GardenView,
    equations: list[tuple[rules_repo.Rule, str]],
    solved: dict[Unknown, float],
    model_cache: dict[str, PropertyProvider],
) -> dict[str, dict[str, str]]:
    """A reasonable unit for every solved freedom that doesn't already
    declare one, read straight off whichever equation determined it.

    Units are opt-in everywhere else in this module -- a garden that
    never annotates anything runs exactly as it always has -- but a
    *freedom* is different: the solver is about to write a number onto
    it regardless, and if the equation that produced it plainly names a
    dimension (`resistance == voltage / current`), leaving the result
    unitless is simply losing information the garden already had. Only
    ever adds a unit; never overwrites one a mote already declared.

    One rule may not be the whole story for a value the solver backend
    (rather than the substitution loop) determined from several equations
    at once, so every equation naming the freedom is tried and the first
    one with a determinable dimension wins -- good enough for the common
    case this exists for, and silent (not a guess) everywhere else."""
    inferred: dict[str, dict[str, str]] = {}
    for mote_id, key in solved:
        if view.units.get(mote_id, {}).get(key):
            continue
        if key in inferred.get(mote_id, {}):
            continue
        for rule, pair_mote_id in equations:
            if pair_mote_id != mote_id:
                continue
            ctx = _dimension_context(view, model_cache, mote_id)
            unit_name = infer_unit(rule.equation, key, ctx)
            if unit_name:
                inferred.setdefault(mote_id, {})[key] = unit_name
                break
    return inferred


def _apply_inferred_units(
    conn: sqlite3.Connection,
    properties: dict[str, dict],
    inferred: dict[str, dict[str, str]],
) -> dict[str, dict]:
    """Merge newly-inferred units into `properties` and persist them, for
    `_write_back()`'s benefit and the mote's own record alike. Returns an
    updated properties dict rather than mutating in place, matching every
    other helper in this module that touches `properties`."""
    if not inferred:
        return properties
    updated = dict(properties)
    for mote_id, units_by_key in inferred.items():
        existing = parse_units_property(updated.get(mote_id, {}).get(UNITS_KEY))
        merged = {**existing, **units_by_key}
        updated[mote_id] = {**updated.get(mote_id, {}), UNITS_KEY: merged}
        update_mote(conn, mote_id, {UNITS_KEY: merged})
    return updated


@dataclass(frozen=True)
class RoleReport:
    """The result of checking how a garden's assemblies are wired."""
    motes_checked: int
    problems: list[str]
    once_scoped: list[str]


def check_roles(conn: sqlite3.Connection) -> RoleReport:
    """Check every declared role's wiring, and report which rules are
    asserted about a single mote rather than a species.

    A lint rather than part of a run, unlike the dimensional check. The
    difference is what happens when each goes wrong: a dimensional
    mismatch is silent and yields a plausible wrong number, so a run has
    to warn about it; a mis-wired role that any rule actually uses fails
    that run outright, naming the role. What is left over is worth
    checking deliberately, not on every solve.

    `once_scoped` exists because rule scope is inferred from what
    `applies_to` points at (see core/rules.py). Making the first instance
    of a mote silently converts every rule asserted about it into one
    asserted about a whole type, so those rules are listed here where the
    change is visible before it produces a different answer.
    """
    properties = {m.id: m.properties for m in list_motes(conn)}
    relationships = list_relationships(conn)
    view = _build_view(properties, relationships)

    problems = []
    for mote_id in sorted(view.roles):
        problems.extend(_role_problems(view, mote_id))

    once = [
        f"{rule.name!r} is asserted once, about {_mote_label(view, rule.target_id)}"
        for rule in rules_repo.list_rules(conn)
        if rule.scope == rules_repo.ONCE
    ]
    return RoleReport(len(view.roles), problems, sorted(once))


def _role_problems(view: _GardenView, mote_id: str) -> list[str]:
    """Every wiring problem with one mote's declared roles."""
    problems = []
    for name in sorted(view.roles[mote_id]):
        role = view.roles[mote_id][name]
        filled = view.out_edges.get((mote_id, role.name), [])
        problem = roles_repo.arity_problem(role, len(filled))
        if problem is not None:
            problems.append(f"{_mote_label(view, mote_id)}: {problem}")
            continue
        for target in filled:
            mismatch = roles_repo.species_problem(
                role, target, view.out_edges.get((target, species_repo.INSTANCE_OF), [])
            )
            if mismatch is not None:
                problems.append(
                    f"{_mote_label(view, mote_id)}: {mismatch} "
                    f"({_mote_label(view, target)})"
                )
    return problems


def incidence_index(
    conn: sqlite3.Connection, scenario: str | None = None
) -> incidence_repo.Index:
    """Which equations touch which values, built fresh from the rules.

    Derived on every call rather than stored (see core/incidence.py):
    the equations are the single source of truth about what depends on
    what, and a persisted index would be a second one.

    Costs one probe evaluation per (rule, instance) pair -- the same
    order of work a dimensional check already does, and far less than
    solving, since nothing here is iterated.

    `scenario` answers the structural question about a hypothetical --
    what a scenario would make solvable, and what it would leave
    missing -- which matters most when the scenario changes `freedoms`
    rather than a value."""
    properties = {m.id: m.properties for m in list_motes(conn)}
    view = _build_view(
        scenarios_repo.apply(properties, _resolve_scenario(conn, scenario)),
        list_relationships(conn),
    )
    pairs = _rule_instances(rules_repo.list_rules(conn), view)
    model_cache: dict[str, PropertyProvider] = {}

    return incidence_repo.Index(
        touches=tuple(_touch(view, rule, mote_id, model_cache) for rule, mote_id in pairs),
        freedoms=frozenset(
            (mote_id, key) for mote_id, keys in view.freedoms.items() for key in keys
        ),
        has_value=lambda variable: _has_value(view, variable),
    )


def _touch(
    view: _GardenView,
    rule: rules_repo.Rule,
    mote_id: str,
    model_cache: dict[str, PropertyProvider],
) -> incidence_repo.EquationTouch:
    """What one (rule, instance) equation references, without solving it.

    _probe() gives both halves at once: the unknowns it asked about, and
    -- because every unknown stands in as a placeholder, so nothing
    defers -- a complete record of the properties it read. Using the real
    evaluator here is the point; a second walk written to answer this
    question would be a second interpretation of the model.

    Reads are permissive here (see core/provenance.py): a property no
    mote holds is the single most useful thing `requires` can report, but
    core/formula.py raises on it, so an ordinary probe would never see
    the read happen and the gap would be invisible. Permissive reads make
    the walk finish and record the absence instead -- which is safe only
    because nothing here is solving.

    A property-model call is deliberately not a read: `property('fluid',
    'enthalpy_pt', ...)` names an operation, not a value any mote holds,
    so recording it as a variable would invent one. A prev() read is kept
    -- a recurrence really does depend on its own previous value, one
    step back.

    A **constraint** determines nothing, whatever it references: no
    solver backend ever receives one. Everything it touches is therefore
    a read, so `producers` never offers `efficiency <= 1` as a way to
    find an efficiency.
    """
    reads = Reads(_normalizer(view), permissive=True)
    referenced = _probe(view, rule, mote_id, {}, model_cache=model_cache, reads=reads)
    read_variables = frozenset(
        (read.mote_id, read.key)
        for read in reads.collected()
        if read.kind in (PROPERTY_READ, PREVIOUS_READ, MISSING_READ)
    )
    try:
        determines = referenced if relation_operator(rule.equation) == EQUALS else frozenset()
    except FormulaError:
        determines = referenced
    read_variables |= referenced - determines
    return incidence_repo.EquationTouch(
        rule_mote_id=rule.mote.id,
        rule_name=rule.name,
        anchor_mote_id=mote_id,
        equation=rule.equation,
        determines=determines,
        reads=read_variables - determines,
    )


def _has_value(view: _GardenView, variable: Unknown) -> bool:
    """Whether a mote actually holds a usable number for this key."""
    mote_id, key = variable
    value = view.properties.get(mote_id, {}).get(key)
    return isinstance(value, (int, float)) and not isinstance(value, bool)


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


@dataclass(frozen=True)
class _Sourced:
    """One evaluated equation, plus where it came from and what it read.

    The equation alone is what every solver backend consumes; the other
    two fields exist only so a solve can be explained afterwards, and are
    dropped on the way into a Remainder."""
    equation: Equation
    origin: Origin
    inputs: tuple[InputRead, ...]


@dataclass(frozen=True)
class _SourcedDeferred:
    """The same, for an equation that stayed nonlinear in its unknowns.
    `inputs` is what the failed evaluation managed to read before giving
    up -- genuine reads, but not a complete list, which is why a solver
    block's record names its equations rather than claiming to account
    for every input."""
    deferred: DeferredEquation
    origin: Origin
    inputs: tuple[InputRead, ...]


@dataclass(frozen=True)
class _Resolved:
    """One value the substitution loop determined outright, with the
    single equation that determined it."""
    value: float
    origin: Origin
    inputs: tuple[InputRead, ...]


@dataclass(frozen=True)
class _Reduction:
    """What the substitution loop left behind. `remainder` is what a
    solver backend needs; `sourced` and `deferred` keep each equation's
    origin and reads alongside it, so _finish() can record which of them
    actually contributed to the values the backend returned."""
    remainder: Remainder
    sourced: tuple[_Sourced, ...]
    deferred: tuple[_SourcedDeferred, ...]


def _reduce(
    view: _GardenView,
    pairs: list[tuple[rules_repo.Rule, str]],
    solved: dict[Unknown, float],
    tick: float = 0.0,
    previous: dict[str, dict] | None = None,
    *,
    model_cache: dict[str, PropertyProvider],
    journal: Journal | None = None,
) -> _Reduction:
    """Substitution loop. Repeatedly evaluates every pair, picks off any
    equation that comes down to a single unknown, and feeds the answer
    back in -- exact and free of iteration error for anything expressible
    this way, and the only stage every solver backend shares. Whatever's
    left (still-linear multi-unknown equations, or equations genuinely
    nonlinear in their unknowns) is packaged into a Remainder for the
    selected backend (see core/solvers.py) to finish.

    Each pass through this loop is one derivation: an equation, the known
    values it read, and the single value it determined. `journal` collects
    them (see core/provenance.py); with no journal the loop behaves
    exactly as it always has, and records nothing."""
    sourced: list[_Sourced] = []
    deferred: list[_SourcedDeferred] = []

    while True:
        sourced, deferred = _evaluate_all(
            view, pairs, solved, tick, previous, model_cache=model_cache, journal=journal
        )
        newly = _solve_singletons(sourced, solved)
        if not newly:
            break
        for unknown, resolved in sorted(newly.items()):
            if journal is not None:
                journal.record(
                    SUBSTITUTION, {unknown: resolved.value},
                    (resolved.origin,), resolved.inputs,
                )
            solved[unknown] = resolved.value

    return _Reduction(
        remainder=Remainder([s.equation for s in sourced], [d.deferred for d in deferred]),
        sourced=tuple(sourced),
        deferred=tuple(deferred),
    )


def _contributors(
    reduction: _Reduction, values: dict[Unknown, float]
) -> tuple[tuple[Origin, ...], tuple[InputRead, ...]]:
    """The equations that actually constrained the values a solver
    backend returned, and what those equations read.

    Filtered rather than taken wholesale, because the remainder still
    contains equations the substitution loop already used up: once
    `voltage == 0` has determined a ground node, re-evaluating it yields
    the trivially true `0 == 0`, which stays in the open set but
    constrains nothing. Listing it as a reason for an answer it took no
    part in would be false.
    """
    determined = set(values)
    origins: list[Origin] = []
    merged: dict[tuple[str, str, str], InputRead] = {}

    contributing = [s for s in reduction.sourced if set(s.equation.terms) & determined]
    contributing += [d for d in reduction.deferred if d.deferred.references & determined]
    for item in contributing:
        origins.append(item.origin)
        for read in item.inputs:
            merged.setdefault((read.kind, read.mote_id, read.key), read)

    return (
        tuple(origins),
        tuple(sorted(merged.values(), key=lambda r: (r.kind, r.mote_id, r.key))),
    )


def _evaluate_all(
    view: _GardenView,
    pairs: list[tuple[rules_repo.Rule, str]],
    solved: dict[Unknown, float],
    tick: float = 0.0,
    previous: dict[str, dict] | None = None,
    *,
    model_cache: dict[str, PropertyProvider],
    journal: Journal | None = None,
) -> tuple[list[_Sourced], list[_SourcedDeferred]]:
    """Nonlinear failures are deferred rather than fatal -- the offending
    unknown may still be determined by another equation, which would make
    this one evaluable on a later pass.

    A deferred equation keeps whatever its failed evaluation read before
    raising. Those reads really happened, so recording them is honest;
    they are simply not the whole story, which is what the solver block's
    own record says."""
    normalize = _normalizer(view) if journal is not None else None
    sourced: list[_Sourced] = []
    deferred: list[_SourcedDeferred] = []

    for rule, mote_id in pairs:
        origin = Origin(rule.mote.id, rule.name, mote_id, rule.equation)
        reads = Reads(normalize) if journal is not None else None
        try:
            ctx = _context(
                view, mote_id, solved, tick, previous, model_cache=model_cache, reads=reads
            )
            equation = evaluate_equation(rule.equation, ctx)
        except NonlinearError as e:
            deferred.append(
                _SourcedDeferred(
                    _defer(view, rule, mote_id, solved, tick, previous, model_cache, e),
                    origin,
                    reads.collected() if reads is not None else (),
                )
            )
        except FormulaError as e:
            raise SimulationError(f"rule {rule.name!r} on mote {mote_id[:8]}: {e}") from e
        else:
            sourced.append(
                _Sourced(equation, origin, reads.collected() if reads is not None else ())
            )
    return sourced, deferred


def _probe(
    view: _GardenView,
    rule: rules_repo.Rule,
    mote_id: str,
    solved: dict[Unknown, float],
    tick: float = 0.0,
    previous: dict[str, dict] | None = None,
    *,
    model_cache: dict[str, PropertyProvider],
    reads: Reads | None = None,
) -> frozenset[Unknown]:
    """Which unknowns an equation actually references.

    Discovered by evaluating it once with a placeholder guess (1.0) for
    every currently-unsolved declared unknown in the whole view, and
    recording which ones core/formula.py's `_lookup` asked about -- it
    checks membership on Context.solved exactly that way, so
    core/provenance.py's RecordingSolved sees every one without any
    change to the evaluator. Placeholders are what make this work on an
    equation that cannot be evaluated symbolically: with every unknown
    standing in as a number nothing defers, and the walk reaches every
    corner of the expression.

    If the probe itself fails -- a placeholder isn't a valid input to a
    property() model, say -- fall back to the full unsolved set rather
    than under-counting. Over-counting makes a solver look for one
    unknown too many; under-counting silently drops an equation's
    dependence on something, which is worse in both callers.

    Passing `reads` also records the properties the probe read on the
    way, which is how the incidence index gets a complete picture of an
    equation that would otherwise raise part-way through. Not used by
    _defer(), and never on the per-iteration path a solver backend runs.
    """
    all_unsolved = {
        (mid, key)
        for mid, keys in view.freedoms.items()
        for key in keys
        if (mid, key) not in solved
    }
    placeholder = RecordingSolved(solved)
    for unknown in all_unsolved:
        dict.setdefault(placeholder, unknown, 1.0)
    try:
        ctx = _context(
            view, mote_id, placeholder, tick, previous,
            model_cache=model_cache, reads=reads,
        )
        evaluate_equation(rule.equation, ctx)
    except FormulaError:
        return frozenset(all_unsolved)
    return frozenset(placeholder.touched) & all_unsolved


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
    must also pin down -- comes from _probe()."""
    references = _probe(
        view, rule, mote_id, solved, tick, previous, model_cache=model_cache
    )

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
    sourced: list[_Sourced], known: dict[Unknown, float]
) -> dict[Unknown, _Resolved]:
    """Any equation down to `c*x + k == 0` determines x outright.

    Returns the equation that determined each value alongside it, not
    just the number: that pairing is the whole of a substitution
    derivation, and it only exists here."""
    newly: dict[Unknown, _Resolved] = {}
    for item in sourced:
        equation = item.equation
        if len(equation.terms) != 1:
            continue
        (unknown, coefficient), = equation.terms.items()
        # Exact zero, not `abs(coefficient) < _TOLERANCE`: a coefficient
        # is a physical scale factor that may legitimately be far smaller
        # than `_TOLERANCE` (an elementary charge is ~1.602e-19 in base
        # units) without being zero -- see core/formula.py's `_prune`,
        # which no longer treats one as pruned-to-nothing either. Only an
        # exact 0.0 term never survives pruning, so this is the only value
        # that would divide-by-zero below.
        if unknown in newly or unknown in known or coefficient == 0.0:
            continue
        # `+ 0.0` normalizes IEEE negative zero, so a grounded node reads
        # `voltage: 0.0` rather than `-0.0`.
        value = -equation.constant / coefficient + 0.0
        newly[unknown] = _Resolved(value, item.origin, item.inputs)
    return newly


def _finish(
    view: _GardenView,
    reduction: _Reduction,
    known: dict[Unknown, float],
    solver: str,
    solver_configuration: dict | None,
    journal: Journal | None = None,
) -> dict[Unknown, float]:
    """Resolves and runs the selected backend (see core/solvers.py) on
    whatever _reduce() couldn't finish alone. `candidates` is deliberately
    narrower than "every declared-but-unsolved unknown" -- it's only the
    ones some equation actually references, so a declared unknown with no
    equation at all keeps surfacing softly via _unsolved() under every
    solver, exactly as it always has, rather than becoming a spurious
    underdetermined failure.

    Everything the backend returns is recorded as one block. That is the
    honest shape: these equations determined these values *together*, and
    no smaller attribution exists to make."""
    remainder = reduction.remainder
    candidates = {u for e in remainder.open_equations for u in e.terms}
    for deferred in remainder.deferred:
        candidates |= deferred.references
    backend = resolve_solver(solver)(solver_configuration or {})
    values = backend.solve(remainder, known, candidates, _initial_guess(view))

    if journal is not None and values:
        origins, inputs = _contributors(reduction, values)
        journal.record(
            SOLVER,
            values,
            origins,
            inputs,
            detail=f"solved together by the {solver!r} solver",
        )
    return values


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
    """Declared freedoms no equation determined -- reported softly rather
    than failing, since a half-built model is a normal state to be in."""
    declared = {(mote_id, key) for mote_id, keys in view.freedoms.items() for key in keys}
    return sorted(declared - set(solved))


def _next_run_number(conn: sqlite3.Connection) -> int:
    """Derived rather than stored -- the log already knows how many runs
    this garden has seen."""
    cursor = conn.cursor()
    cursor.execute("SELECT COALESCE(MAX(run_number), 0) + 1 AS next FROM simulation_runs")
    return int(cursor.fetchone()["next"])


def _resolve_scenario(
    conn: sqlite3.Connection, scenario_id: str | None
) -> scenarios_repo.Scenario | None:
    """A named scenario, or None for the garden as it stands.

    An id that names no scenario is an error rather than a quiet fallback
    to the unperturbed garden: it is almost always a mistyped id, and
    silently answering a different question than the one asked is the
    worst possible response."""
    if scenario_id is None:
        return None
    case = scenarios_repo.get_scenario(conn, scenario_id)
    if case is None:
        raise SimulationError(
            f"no scenario {scenario_id[:8]!r} -- a scenario is a mote with at least one "
            f"{scenarios_repo.VARIES!r} relationship to an override"
        )
    return case


def _log_run(
    conn: sqlite3.Connection,
    run_number: int,
    status: str,
    detail: str | None,
    step_changes: list[list[Change]],
    committed: bool,
    step_journals: list[Journal] | None = None,
    scenario_id: str | None = None,
) -> str:
    """step_changes[i] is tick i's own changes (0-indexed, oldest first).
    A steady-state run logs a single tick; a committed or saved
    time-series run logs one per computed step, so a later run_ticks()
    can scrub through exactly what happened tick by tick. `committed`
    records whether these values were actually written to the graph
    (core/db.py's simulation_runs.committed) -- a saved-only run (see
    run_time_series's save parameter) never touches motes/mote_properties
    at all, only this log.

    step_journals[i] is tick i's provenance (see core/provenance.py),
    written in the same transaction as its changes so the two can never
    disagree about what a run did. A failed run logs neither, exactly as
    it already logged no changes: the partial derivations of a solve that
    did not finish describe a state the garden never reached."""
    run_id = generate_uuid()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO simulation_runs "
        "(id, run_number, ran_at, status, detail, committed, scenario_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (run_id, run_number, datetime.now(timezone.utc).isoformat(), status, detail,
         int(committed), scenario_id),
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
    for step, journal in enumerate(step_journals or []):
        derivations_repo.log_journal(cursor, run_id, step, journal)
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
    scenario_id: str | None = None


_RUN_COLUMNS = (
    "r.id AS id, r.run_number AS run_number, r.ran_at AS ran_at, r.status AS status, "
    "r.detail AS detail, r.committed AS committed, r.scenario_id AS scenario_id, "
    "COUNT(DISTINCT c.mote_id || ':' || c.key) AS change_count"
)


def _row_to_run(row: sqlite3.Row) -> RunRecord:
    return RunRecord(
        run_id=row["id"], run_number=row["run_number"], ran_at=row["ran_at"],
        status=row["status"], detail=row["detail"], committed=bool(row["committed"]),
        change_count=row["change_count"], scenario_id=row["scenario_id"],
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

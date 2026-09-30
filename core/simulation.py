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

from core import derivations as derivations_repo
from core import incidence as incidence_repo
from core import model_resolver
from core import roles as roles_repo
from core import rules as rules_repo
from core import scenarios as scenarios_repo
from core import solver_registry  # noqa: F401 -- registers built-in solver backends
from core import species as species_repo
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
            f"from {self.equation_count} equation{'' if self.equation_count == 1 else 's'}."
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
    """One step of a time-based preview -- the same shape as
    SimulationResult minus the fields that only make sense once per
    solve (dimension warnings, the scenario)."""
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
    failure (if any) stopped the run early, so a user can still scrub
    through the good steps rather than losing the whole run."""
    steps: list[StepResult]
    failed_at: int | None = None   # step index that raised, if the run stopped early
    error: str | None = None
    # Checked once against the starting garden rather than per step: the
    # rules and declared units are the same every step, only values move.
    dimension_warnings: list[str] = field(default_factory=list)

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


# How a constraint came out. `NOT_CHECKED` is deliberately not a third
# flavour of failure: a limit nobody could evaluate is a gap in the model,
# and reporting it as passing would make silence and success look alike.
PASS = "pass"
FAIL = "fail"
NOT_CHECKED = "not checked"


@dataclass(frozen=True)
class ConstraintResult:
    """One comparison relation, and whether the values satisfy it.

    `off_by` is the magnitude of the residual -- how far the answer is from
    the limit, on the side it landed. Carried for a passing constraint too,
    since "within limits by 0.02" and "within limits by 400" are different
    things to report."""
    rule_mote_id: str
    label: str
    equation: str
    status: str
    detail: str = ""
    off_by: float | None = None
    # Set when the relation could not be evaluated at all, which is a
    # broken model rather than an unchecked limit. A run raises on it; a
    # caller that must not raise reports it.
    error: str | None = None

    @property
    def passed(self) -> bool:
        return self.status == PASS


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
    # Every mote id under each `name`, for garden-scope names
    # (core/formula.py's Context.named). Built from the same properties
    # dict the run reads, so an override mote -- whose properties are
    # stripped above -- contributes no name to resolve against.
    by_name: dict[str, list[str]] = field(default_factory=dict)


def step_payloads(steps: list[StepResult]) -> list[dict]:
    """The canonical JSON-safe form of a preview's steps, shared by every
    adapter that has to put them on a wire (motion/api.py's JS bridge,
    mcp_server/server.py's tools) so the same object never reaches two
    surfaces in two shapes.

    Lives here rather than in an adapter because the reason this
    conversion exists is a property of the data: StepResult.values and
    .unsolved are keyed by (mote_id, key) tuples, which JSON has no way
    to represent. `changes` already carries everything a caller needs,
    and unsolved counts stay prose-only inside each step's summary."""
    return [
        {
            "step": step.step,
            "changes": [dataclasses.asdict(c) for c in step.changes],
            "summary": step.summary,
            "equation_count": step.equation_count,
            "constraint_violations": list(step.constraint_violations),
        }
        for step in steps
    ]


@dataclass(frozen=True)
class Solved:
    """One solve of one garden view -- what every run shape actually wants
    back, before any of them decides what to do about it.

    `equations`/`constraints` are the relations this solve ran, carried
    out on the result so a caller needing them again (run_simulation's
    unit inference) doesn't split the same rules a second time."""
    values: dict[Unknown, float]
    equation_count: int
    unsolved: list[Unknown]
    constraint_violations: list[str]
    equations: list[tuple[rules_repo.Rule, str]]
    constraints: list[tuple[rules_repo.Rule, str]]
    view: "_GardenView | None" = None

    def value(self, mote_id: str, key: str) -> float | None:
        """What this solve says one property is, in the mote's own unit.

        A solved value if the run determined one, otherwise whatever the
        mote already carried -- which is the right answer for a property
        that is a given rather than a freedom, and the reason a caller
        reading results does not have to know which it asked for. None
        where the mote has no such number at all.

        Solved values are converted out of base units here, the same
        conversion _by_mote() does on the way to the database: a caller
        reading 4700 for a resistance the mote declares in kohm would be
        reading the solver's representation rather than the garden's."""
        if (mote_id, key) in self.values:
            return _in_declared_units(
                self.view.properties if self.view else {},
                mote_id, key, self.values[(mote_id, key)],
            )
        stored = (self.view.properties if self.view else {}).get(mote_id, {}).get(key)
        if isinstance(stored, bool) or not isinstance(stored, (int, float)):
            return None
        return float(stored)

    def is_free(self, mote_id: str, key: str) -> bool:
        """Whether this mote declared that property a freedom -- something
        the solver is allowed to determine.

        A caller that overrides a freedom (core/sweep.py sweeping one) is
        pinning it, and pinning is worth saying out loud: the answer under
        it is 'what if this were so', not 'what the model determines'."""
        if self.view is None:
            return False
        return key in self.view.freedoms.get(mote_id, set())


def solve_view(
    view: _GardenView,
    pairs: list[tuple[rules_repo.Rule, str]],
    solver: str = _DEFAULT_SOLVER,
    solver_configuration: dict | None = None,
    *,
    tick: float = 0.0,
    previous: dict[str, dict] | None = None,
    strict_constraints: bool = False,
    log_constraints: bool = True,
    model_cache: dict[str, PropertyProvider] | None = None,
    journal: Journal | None = None,
) -> Solved:
    """Solve one view once, in memory, writing and logging nothing.

    The five steps every run shape shares: split the relations, substitute
    whatever comes down to a single unknown, hand the rest to the selected
    backend, check the constraints, report what stayed unsolved.
    run_simulation() and run_time_series() differ from each other only in
    the bookkeeping around this -- whether the answer and its provenance
    are written back, and whether a failure ends the run or only the
    step.

    It is also the entirety of what core/sweep.py needs: a sweep is N of
    these over N overridden copies of the same garden. That is why this is
    a function now rather than a third copy of the same six lines.

    Raises SimulationError or SolverError exactly as the steps it calls
    do. Whether that ends a run is the caller's business, not this one's.
    """
    equations, constraints = _split_relations(pairs)
    if model_cache is None:
        model_cache = {}
    values: dict[Unknown, float] = {}
    reduction = _reduce(
        view, equations, values, tick=tick, previous=previous,
        model_cache=model_cache, journal=journal,
    )
    values.update(
        _finish(view, reduction, values, solver, solver_configuration, journal)
    )
    violations = _constraint_report(
        view, constraints, values, strict_constraints,
        tick=tick, previous=previous, model_cache=model_cache,
        log=log_constraints,
    )
    remainder = reduction.remainder
    return Solved(
        values=values,
        equation_count=len(remainder.open_equations) + len(remainder.deferred),
        unsolved=_unsolved(view, values),
        constraint_violations=violations,
        equations=equations,
        constraints=constraints,
        view=view,
    )


def garden_freedoms(conn: sqlite3.Connection) -> dict[str, set[str]]:
    """Every mote's resolved freedoms: what the solver is allowed to
    determine, per mote, after a mote's own declaration has replaced
    whatever its species declare (see _freedoms_for).

    Public because more than one surface now needs to tell a freedom from
    a given without running anything -- core/sweep.py's callers draw them
    differently, since sweeping a freedom pins it. Exposed here rather
    than re-derived there: the resolution rule is subtle enough that a
    second copy of it would drift."""
    properties = {m.id: m.properties for m in list_motes(conn)}
    return _build_view(properties, list_relationships(conn)).freedoms


def solve_properties(
    properties: dict[str, dict],
    relationships: list[Relationship],
    rules: list[rules_repo.Rule],
    solver: str = _DEFAULT_SOLVER,
    solver_configuration: dict | None = None,
    *,
    strict_constraints: bool = False,
    log_constraints: bool = True,
    model_cache: dict[str, PropertyProvider] | None = None,
) -> Solved:
    """solve_view() from a plain properties dict -- build the view, pair
    the rules with their instances, solve.

    The entry point for a caller holding a *perturbed* copy of the garden
    rather than the garden: core/sweep.py, which makes one such copy per
    point of a sweep and never writes any of them back. Taking properties
    and relationships as arguments rather than a connection is what keeps
    that honest -- there is no database here to write to."""
    view = _build_view(properties, relationships)
    return solve_view(
        view, _rule_instances(rules, view), solver, solver_configuration,
        strict_constraints=strict_constraints, log_constraints=log_constraints,
        model_cache=model_cache,
    )


def run_simulation(
    conn: sqlite3.Connection,
    solver: str = _DEFAULT_SOLVER,
    solver_configuration: dict | None = None,
    strict_units: bool = False,
    scenario: str | None = None,
    strict_constraints: bool = False,
) -> SimulationResult:
    """Solve the garden and write the results back, replacing the
    garden's provenance (core/derivations.py) with this solve's. Raises
    SimulationError if the rules can't be satisfied, and then writes
    nothing: the motes still hold the last good solve's values, so its
    provenance is still the right explanation of them.

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
    Solving under one **never writes to the graph** -- neither values nor
    provenance -- and the returned changes say what *would* happen. That
    is not a limitation to lift later: the answer to a hypothetical is
    not the garden's state, and the question of where a scenario solve's
    results would be written has no good answer.

    `strict_constraints` promotes an unsatisfied constraint from a report
    on the result to a SimulationError, mirroring `strict_units`. Off by
    default for the same reason: a constraint says something about the
    answer, and seeing an answer that breaks a limit is often exactly why
    you ran it."""
    properties = {m.id: m.properties for m in list_motes(conn)}
    relationships = list_relationships(conn)
    case = _resolve_scenario(conn, scenario)
    view = _build_view(scenarios_repo.apply(properties, case), relationships)
    pairs = _rule_instances(rules_repo.list_rules(conn), view)

    try:
        model_cache: dict[str, PropertyProvider] = {}
        journal = Journal()
        _validate_units(view, pairs)
        warnings = _dimension_warnings(view, pairs, strict_units, model_cache)
        result = solve_view(
            view, pairs, solver, solver_configuration,
            strict_constraints=strict_constraints,
            model_cache=model_cache, journal=journal,
        )
        solved = result.values
        violations = result.constraint_violations
    except SimulationError as e:
        logger.warning("simulation failed: %s", e)
        raise
    except SolverError as e:
        logger.warning("simulation failed: %s", e)
        raise SimulationError(str(e)) from e

    if case is None:
        inferred_units = _infer_missing_units(view, result.equations, solved, model_cache)
        properties = _apply_inferred_units(conn, view.properties, inferred_units)
        changes = _write_back(conn, properties, solved)
        _record_provenance(conn, journal)
    else:
        changes = _changes_for(view.properties, solved)
    logger.info("simulation solved %s values", len(solved))
    return SimulationResult(
        values=solved,
        changes=changes,
        equation_count=result.equation_count,
        unsolved=result.unsolved,
        dimension_warnings=warnings,
        scenario_id=scenario,
        constraint_violations=violations,
    )


def run_time_series(
    conn: sqlite3.Connection,
    step_count: int,
    solver: str = _DEFAULT_SOLVER,
    solver_configuration: dict | None = None,
    strict_units: bool = False,
    scenario: str | None = None,
    strict_constraints: bool = False,
) -> TimeSeriesResult:
    """A preview: runs step_count steps entirely in memory, each step's
    solve starting from the *previous* step's solved values rather than
    re-reading the database -- this is what lets a rule use prev(x) to
    express a recurrence. Nothing is written, provenance included.
    `solver`/`solver_configuration` are the same backend choice
    run_simulation() takes, applied to every step.

    A step that fails to solve stops the run there but keeps every step
    computed before it (see TimeSeriesResult.failed_at/error), so a
    caller can still scrub through the good steps rather than losing the
    whole run.

    `strict_units` is the same opt-in run_simulation() takes. It is
    checked once up front, before any step runs, and raises rather than
    being captured as a failed step -- nothing about it can change
    part-way through a run.

    `scenario` names a mote gathering overrides (see core/scenarios.py);
    the garden is previewed as that scenario would have it."""
    if step_count < 1:
        raise ValueError("step_count must be at least 1")

    relationships = list_relationships(conn)
    rules = rules_repo.list_rules(conn)
    case = _resolve_scenario(conn, scenario)
    running = scenarios_repo.apply(
        {m.id: dict(m.properties) for m in list_motes(conn)}, case
    )
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
            result = solve_view(
                view, pairs, solver, solver_configuration,
                tick=float(step), previous=running,
                strict_constraints=strict_constraints,
                model_cache=model_cache,
            )
            solved = result.values
            violations = result.constraint_violations
        except (SimulationError, SolverError) as e:
            failed_at, error = step, str(e)
            logger.warning("simulation preview step %s failed: %s", step, e)
            break

        changes = _changes_for(view.properties, solved)
        # The running state deliberately mirrors the database's own
        # representation -- values in whatever unit each mote declares,
        # not the solver's base units. Every step re-reads it through the
        # same normalization an ordinary property read gets, so storing
        # base-unit values here would convert them a second time.
        next_running = {mid: dict(props) for mid, props in running.items()}
        for mote_id, new_properties in _by_mote(view.properties, solved).items():
            next_running.setdefault(mote_id, {}).update(new_properties)
        running = next_running
        steps.append(
            StepResult(
                step, solved, changes, result.equation_count,
                result.unsolved, violations,
            )
        )

    return TimeSeriesResult(steps, failed_at, error, warnings)


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
        _by_name(properties),
    )


def _by_name(properties: dict[str, dict]) -> dict[str, list[str]]:
    """Every mote id indexed under its `name`, for garden-scope names.

    Unnamed motes and non-string names are simply absent: a name that
    can't be written in an equation can't be resolved from one either."""
    index: dict[str, list[str]] = {}
    for mote_id, props in properties.items():
        name = props.get("name")
        if isinstance(name, str) and name:
            index.setdefault(name, []).append(mote_id)
    return index


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
    and change-reporting paths -- which work against a plain baseline dict,
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


def constraint_results(
    view: _GardenView,
    pairs: list[tuple[rules_repo.Rule, str]],
    solved: dict[Unknown, float],
    tick: float = 0.0,
    previous: dict[str, dict] | None = None,
    *,
    model_cache: dict[str, PropertyProvider],
) -> list[ConstraintResult]:
    """Every constraint, with whether the answer satisfies it.

    Checked against the values that were actually found -- a constraint is
    a statement about an answer, so checking it before there is one would
    be meaningless.

    A constraint still referring to something unsolved is reported as
    *not checked* rather than assumed satisfied. Silence would be
    indistinguishable from passing, and a limit nobody could evaluate is
    exactly the one worth mentioning.

    Structured rather than prose because two callers want different
    things out of it: a run wants the lines that are worth warning about
    (_check_constraints below), and a checklist wants every constraint
    including the passing ones, in a shape it can draw green or red
    (core/calc.py's `\\checks`). One evaluation answers both -- a second
    walk would be a second opinion about the same limit.
    """
    results = []
    for rule, mote_id in pairs:
        label = f"{rule.name} on {_mote_label(view, mote_id)}"
        try:
            ctx = _context(view, mote_id, solved, tick, previous, model_cache=model_cache)
            evaluated = evaluate_equation(rule.equation, ctx)
        except NonlinearError as e:
            results.append(ConstraintResult(
                rule.mote.id, label, rule.equation, NOT_CHECKED, str(e)))
            continue
        except FormulaError as e:
            # Fatal to a *run* -- a rule that cannot be evaluated at all is
            # a broken model, not an unchecked limit. Carried rather than
            # raised so a caller that must not raise (a note) can report it
            # instead; _check_constraints below raises on it.
            results.append(ConstraintResult(
                rule.mote.id, label, rule.equation, NOT_CHECKED, str(e), error=str(e)))
            continue

        if evaluated.terms:
            results.append(ConstraintResult(
                rule.mote.id, label, rule.equation, NOT_CHECKED,
                f"depends on values this run did not determine "
                f"({describe_unknowns(evaluated.terms)})",
            ))
        elif _satisfied(evaluated.operator, evaluated.constant):
            results.append(ConstraintResult(
                rule.mote.id, label, rule.equation, PASS,
                off_by=abs(evaluated.constant),
            ))
        else:
            results.append(ConstraintResult(
                rule.mote.id, label, rule.equation, FAIL,
                f"off by {abs(evaluated.constant):g}",
                off_by=abs(evaluated.constant),
            ))
    return results


def _check_constraints(
    view: _GardenView,
    pairs: list[tuple[rules_repo.Rule, str]],
    solved: dict[Unknown, float],
    tick: float = 0.0,
    previous: dict[str, dict] | None = None,
    *,
    model_cache: dict[str, PropertyProvider],
) -> list[str]:
    """Which constraints the answer does not satisfy, as readable lines --
    `constraint_results()` above kept for the run's own reporting."""
    problems = []
    for result in constraint_results(
        view, pairs, solved, tick, previous, model_cache=model_cache
    ):
        if result.error:
            raise SimulationError(f"rule {result.label!r}: {result.error}")
        if result.status == NOT_CHECKED:
            problems.append(f"{result.label}: not checked -- {result.detail}")
        elif result.status == FAIL:
            problems.append(
                f"{result.label}: {result.equation} is not satisfied ({result.detail})"
            )
    return problems


def check_constraints(
    conn: sqlite3.Connection, scenario: str | None = None
) -> list[ConstraintResult]:
    """Every comparison relation in the garden, checked against the values
    the motes hold right now -- solving nothing and writing nothing.

    The same check a run performs, available on its own, which is what a
    green/red checklist needs: a document reporting on a finished
    calculation should read the answers that are on the motes, not have to
    re-run the solve to find out whether they are within limits.

    The view's freedoms are cleared for exactly the reason
    `expression_context()` clears them -- a property declared free
    evaluates to a free variable, which is right when a solver is waiting
    for it and useless here. Cleared, `sigma <= 250` is checked against the
    sigma the last solve actually wrote, which is the answer the document
    is reporting on. A freedom nothing has determined yet has no value and
    comes back *not checked*, which is correct and is the one thing a
    checklist must not call a pass."""
    properties = {m.id: m.properties for m in list_motes(conn)}
    view = dataclasses.replace(
        _build_view(
            scenarios_repo.apply(properties, _resolve_scenario(conn, scenario)),
            list_relationships(conn),
        ),
        freedoms={},
    )
    pairs = _rule_instances(rules_repo.list_rules(conn), view)
    constraints = [(rule, mote_id) for rule, mote_id in pairs
                   if _operator_of(rule) != "=="]
    return [_unchecked(r) for r in constraint_results(view, constraints, {}, model_cache={})]


def _unchecked(result: ConstraintResult) -> ConstraintResult:
    """A constraint that could not be evaluated here is *not checked*, not
    a broken model. With freedoms cleared, "no property `sigma`" means the
    solve has not written one yet -- which is a gap in the calculation, and
    reads better said that way than as the evaluator's own words about a
    species that declares no unknown."""
    if not result.error:
        return result
    return dataclasses.replace(
        result,
        detail=f"cannot be evaluated yet -- {result.error}",
        error=None,
    )


def _operator_of(rule: rules_repo.Rule) -> str:
    """A rule's comparison, or `==` for one that doesn't parse -- which
    keeps an unparseable rule out of the constraint list rather than
    reporting it there as an unchecked limit."""
    try:
        return relation_operator(rule.equation)
    except FormulaError:
        return "=="


def _constraint_report(
    view: _GardenView,
    pairs: list[tuple[rules_repo.Rule, str]],
    solved: dict[Unknown, float],
    strict: bool,
    tick: float = 0.0,
    previous: dict[str, dict] | None = None,
    *,
    model_cache: dict[str, PropertyProvider],
    log: bool = True,
) -> list[str]:
    """Shared post-flight for both run shapes -- the constraint mirror of
    _dimension_warnings(). Advisory unless the caller asks otherwise.

    `log=False` for a caller solving the same garden hundreds of times
    over (core/sweep.py): a constraint broken across a whole swept range
    is one fact, and writing it to the log once per point would bury
    every other line in the file under it. The problems are still
    returned, which is what a sweep draws."""
    problems = _check_constraints(
        view, pairs, solved, tick, previous, model_cache=model_cache
    )
    if problems and strict:
        raise SimulationError(
            "constraint check failed:\n  " + "\n  ".join(problems)
        )
    if log:
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
        named=lambda name: view.by_name.get(name, []),
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
    """Names `equation` references that the garden cannot resolve to one
    mote from `mote_id` -- neither a declared role, nor a mote connected
    to it, nor a unique garden-scope name (see core/formula.py's
    `_role_or_value`). Each entry names the colliding mote ids, so an
    editor can offer to wire a relationship to whichever one was meant:
    a connected name is tried before the garden-wide scope, so drawing
    that one edge is what settles the ambiguity.

    Garden-scope names are why this is now the *collision* report rather
    than the unwired-reference report it was. A name matching exactly one
    mote anywhere in the garden resolves on its own, with no relationship
    to draw, so listing it would be offering a fix to something that
    isn't broken.

    Read-only and does not evaluate the equation itself: a name that
    doesn't parse, isn't any mote's name, or resolves fine, is simply
    absent from the result rather than reported as a problem."""
    properties = {m.id: m.properties for m in list_motes(conn)}
    relationships = list_relationships(conn)
    case = _resolve_scenario(conn, scenario)
    view = _build_view(scenarios_repo.apply(properties, case), relationships)

    role_names = set(view.roles.get(mote_id, {}))
    connected_names = [
        view.properties.get(mid, {}).get("name")
        for mid in view.neighbors.get(mote_id, [])
    ]

    results = []
    for name in attribute_bases(equation):
        if name in role_names or connected_names.count(name) == 1:
            continue
        candidates = view.by_name.get(name, [])
        if len(candidates) > 1:
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
        named=lambda name: view.by_name.get(name, []),
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
    # An iterative backend re-evaluates a deferred rule at each guess
    # point, so formula evaluation happens again *inside* solve() -- far
    # from the substitution loop, which is the only other place a
    # FormulaError can arise and which normalizes it to SimulationError
    # itself. A guess that walks somewhere arithmetically impossible (`e
    # ** (-k * x)` overflowing once x goes far enough negative) therefore
    # used to escape the run loop's `except (SimulationError,
    # SolverError)` entirely and surface as an unhandled crash. Naming
    # the solver is the useful part of the message: the rule is fine, the
    # point this backend wandered to is not.
    try:
        values = backend.solve(remainder, known, candidates, _initial_guess(view))
    except SolverError:
        raise
    except FormulaError as e:
        raise SolverError(
            f"the {solver!r} solver failed while evaluating a rule: {e}"
        ) from e

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
    preview (which never writes) and by run_simulation()'s write-back.

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


def _record_provenance(conn: sqlite3.Connection, journal: Journal) -> None:
    """Replace the garden's provenance with this solve's, straight after
    its write-back, so `why` always explains the values the motes hold."""
    derivations_repo.replace_journal(conn.cursor(), journal)
    conn.commit()

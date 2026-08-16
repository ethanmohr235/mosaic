# core/simulation.py
"""Steady-state simulation: collect every rule's equations, solve them, and
write the results back onto the motes.

The solver runs in two stages. First a substitution loop repeatedly picks
off any equation that has come down to a single unknown and feeds the
answer back in; this is what lets a formula be nonlinear as long as the
nonlinear part involves values that are already known (`T2 == T1 *
(p2/p1)**0.286`), and it means a garden of plain assignment-style rules
never touches NumPy at all. Whatever is left must be linear, and goes to
a single least-squares solve.

run_simulation() solves one steady state and writes it back.
run_time_series() solves a requested number of discrete steps, each
starting from the previous step's solved values rather than the
database, which is what lets a rule use `t` or `prev(x)`; it is
nondestructive unless asked to commit or save. See docs/simulation.md.
"""
import dataclasses
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone

import numpy as np

from core import model_resolver
from core import rules as rules_repo
from core import species as species_repo
from core.db import generate_uuid
from core.formula import (
    Context,
    Equation,
    FormulaError,
    NonlinearError,
    Unknown,
    describe,
    evaluate_equation,
)
from core.entities import Relationship
from core.motes import list_motes, update_mote
from core.providers import PropertyProvider
from core.relationships import list_relationships

logger = logging.getLogger(__name__)

_TOLERANCE = 1e-9
_RESIDUAL_TOLERANCE = 1e-6


class SimulationError(Exception):
    """A garden's rules could not be solved."""


@dataclass(frozen=True)
class Change:
    mote_id: str
    key: str
    old_value: float | None
    new_value: float


def _summarize(
    values: dict[Unknown, float],
    changes: list[Change],
    equation_count: int,
    unsolved: list[Unknown],
    scope: str,
) -> str:
    """The one sentence a solve reports, whether it's being described as a
    whole run ("run #3") or as one step within one ("step 0"). Both
    dataclasses below said this in their own copy of the same f-string,
    which is how the same solve came to be described two slightly
    different ways depending on which one you were holding."""
    motes = len({c.mote_id for c in changes})
    text = (
        f"Solved {len(values)} value{'' if len(values) == 1 else 's'} "
        f"across {motes} mote{'' if motes == 1 else 's'} "
        f"from {equation_count} equation{'' if equation_count == 1 else 's'} "
        f"({scope})."
    )
    if unsolved:
        text += f" {len(unsolved)} declared unknown(s) had no equation."
    return text


@dataclass(frozen=True)
class SimulationResult:
    run_id: str
    run_number: int
    values: dict[Unknown, float]
    changes: list[Change]
    equation_count: int
    unsolved: list[Unknown] = field(default_factory=list)

    @property
    def summary(self) -> str:
        return _summarize(
            self.values, self.changes, self.equation_count, self.unsolved,
            f"run #{self.run_number}",
        )


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
        return _summarize(
            self.values, self.changes, self.equation_count, self.unsolved, f"step {self.step}"
        )


@dataclass(frozen=True)
class TimeSeriesResult:
    """The result of run_time_series(): every step computed before a
    failure (if any) stopped the run early, so a user can still scrub to
    and commit an earlier good step rather than losing the whole run."""
    steps: list[StepResult]
    failed_at: int | None = None   # step index that raised, if the run stopped early
    error: str | None = None
    run_id: str | None = None      # set when commit=True or save=True and at least one step succeeded
    run_number: int | None = None  # this garden's own run counter, set alongside run_id


def step_payloads(steps: "list[StepResult] | list[RunTick]") -> list[dict]:
    """The canonical JSON-safe form of a run's steps, shared by every
    adapter that has to put them on a wire (ui/api.py's JS bridge,
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
    key is present only for a freshly solved StepResult."""
    payloads = []
    for step in steps:
        payload = {
            "step": step.step,
            "changes": [dataclasses.asdict(c) for c in step.changes],
            "summary": step.summary,
        }
        if isinstance(step, StepResult):
            payload["equation_count"] = step.equation_count
        payloads.append(payload)
    return payloads


@dataclass(frozen=True)
class _GardenView:
    """The whole garden read once, so evaluating N equations doesn't mean
    N round trips. Fine at this scale -- MonOSS is explicitly not built
    for millions of records."""
    properties: dict[str, dict]
    unknowns: dict[str, set[str]]
    out_edges: dict[tuple[str, str], list[str]]
    in_edges: dict[tuple[str, str], list[str]]


def run_simulation(conn: sqlite3.Connection) -> SimulationResult:
    """Solve the garden and write the results back. Raises SimulationError
    (after logging the failed run) if the rules can't be satisfied.

    A steady-state run *is* a one-step committed time series -- the docs
    have always said so, and now the code does too rather than keeping a
    second solve-and-write path in parallel with run_time_series(). What
    stays different is only how the two report themselves: this one
    raises on failure and hands back a single result, because a caller
    asking for one answer wants either the answer or the reason."""
    result = run_time_series(conn, step_count=1, commit=True, log_failure=True)
    if result.error is not None:
        raise SimulationError(result.error)

    step = result.steps[0]
    return SimulationResult(
        run_id=result.run_id,
        run_number=result.run_number,
        values=step.values,
        changes=step.changes,
        equation_count=step.equation_count,
        unsolved=step.unsolved,
    )


def run_time_series(
    conn: sqlite3.Connection,
    step_count: int,
    commit: bool,
    save: bool = False,
    log_failure: bool = False,
) -> TimeSeriesResult:
    """Runs step_count steps entirely in memory, each step's solve
    starting from the *previous* step's solved values rather than
    re-reading the database -- this is what lets a rule use prev(x) to
    express a recurrence. This is the only solve-and-write path there is:
    run_simulation() is this, with step_count=1 and commit=True.

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

    log_failure records a failed run in the audit log rather than leaving
    no trace. Off by default because a discarded preview is meant to
    leave none (docs/architecture.md); a run that intended to write is
    the one worth being able to look back at, which is why
    run_simulation() asks for it.

    A step that fails to solve stops the run there but keeps every step
    computed before it (see TimeSeriesResult.failed_at/error), so a
    caller can still commit or save an earlier successful step rather
    than losing the whole run."""
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

    for step in range(step_count):
        view = _build_view(running, relationships)
        pairs = _rule_instances(rules, view)
        try:
            solved: dict[Unknown, float] = {}
            equations = _resolve(
                view, pairs, solved, tick=float(step), previous=running, model_cache=model_cache
            )
            solved.update(_matrix_solve(equations, solved))
        except SimulationError as e:
            failed_at, error = step, str(e)
            logger.warning("simulation step %s failed: %s", step, e)
            break

        changes = _changes_for(view.properties, solved)
        cumulative.update(solved)
        next_running = {mid: dict(props) for mid, props in running.items()}
        for (mote_id, key), value in solved.items():
            next_running.setdefault(mote_id, {})[key] = value
        running = next_running
        steps.append(StepResult(step, solved, changes, len(equations), _unsolved(view, solved)))

    run_id = run_number = None
    if (commit or save) and steps:
        run_number = _next_run_number(conn)
        if commit:
            _write_back(conn, baseline, cumulative)
        run_id = _log_run(conn, run_number, "ok", None, [s.changes for s in steps], committed=commit)
        logger.info(
            "simulation time series %s %s steps, %s values",
            "committed" if commit else "saved", len(steps), len(cumulative),
        )
    elif log_failure and error is not None:
        # A run that meant to write and couldn't: logged with no changes,
        # so it still shows up in history as the failure it was.
        run_number = _next_run_number(conn)
        run_id = _log_run(conn, run_number, "error", error, [], committed=False)
        logger.warning("simulation run #%s failed: %s", run_number, error)

    return TimeSeriesResult(steps, failed_at, error, run_id, run_number)


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

    return _GardenView(properties, unknowns, out_edges, in_edges)


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


def _resolve(
    view: _GardenView,
    pairs: list[tuple[rules_repo.Rule, str]],
    solved: dict[Unknown, float],
    tick: float = 0.0,
    previous: dict[str, dict] | None = None,
    *,
    model_cache: dict[str, PropertyProvider],
) -> list[Equation]:
    """Substitution loop. Returns the equations as evaluated against the
    final set of substituted values, ready for the matrix solve."""
    equations: list[Equation] = []
    deferred: list[tuple[rules_repo.Rule, str, Exception]] = []

    while True:
        equations, deferred = _evaluate_all(view, pairs, solved, tick, previous, model_cache=model_cache)
        newly = _solve_singletons(equations, solved)
        if not newly:
            break
        solved.update(newly)

    if deferred:
        rule, mote_id, error = deferred[0]
        raise SimulationError(
            f"rule {rule.name!r} on mote {mote_id[:8]} could not be resolved: {error}"
        )
    return equations


def _evaluate_all(
    view: _GardenView,
    pairs: list[tuple[rules_repo.Rule, str]],
    solved: dict[Unknown, float],
    tick: float = 0.0,
    previous: dict[str, dict] | None = None,
    *,
    model_cache: dict[str, PropertyProvider],
) -> tuple[list[Equation], list[tuple[rules_repo.Rule, str, Exception]]]:
    """Nonlinear failures are deferred rather than fatal -- the offending
    unknown may still be determined by another equation, which would make
    this one evaluable on a later pass."""
    equations, deferred = [], []
    for rule, mote_id in pairs:
        try:
            ctx = _context(view, mote_id, solved, tick, previous, model_cache=model_cache)
            equations.append(evaluate_equation(rule.equation, ctx))
        except NonlinearError as e:
            deferred.append((rule, mote_id, e))
        except FormulaError as e:
            raise SimulationError(f"rule {rule.name!r} on mote {mote_id[:8]}: {e}") from e
    return equations, deferred


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


def _matrix_solve(
    equations: list[Equation], solved: dict[Unknown, float]
) -> dict[Unknown, float]:
    open_equations = [e for e in equations if e.terms]
    _check_consistency([e for e in equations if not e.terms])
    if not open_equations:
        return {}

    unknowns = sorted({u for e in open_equations for u in e.terms})
    index = {unknown: i for i, unknown in enumerate(unknowns)}
    matrix = np.zeros((len(open_equations), len(unknowns)))
    constants = np.zeros(len(open_equations))
    for row, equation in enumerate(open_equations):
        for unknown, coefficient in equation.terms.items():
            matrix[row, index[unknown]] = coefficient
        constants[row] = -equation.constant

    solution, _, rank, _ = np.linalg.lstsq(matrix, constants, rcond=None)
    if rank < len(unknowns):
        raise SimulationError(
            f"underdetermined: {len(unknowns)} unknowns but only {rank} independent "
            f"equations. Unknowns: {describe(unknowns)}"
        )
    residual = float(np.abs(matrix @ solution - constants).max())
    if residual > _RESIDUAL_TOLERANCE:
        raise SimulationError(
            f"inconsistent: no values satisfy all {len(open_equations)} equations "
            f"(worst mismatch {residual:.3g}). Unknowns: {describe(unknowns)}"
        )
    return {unknown: float(solution[i]) + 0.0 for unknown, i in index.items()}


def _check_consistency(closed: list[Equation]) -> None:
    """An equation with no unknowns left is either satisfied or a flat
    contradiction between rules."""
    for equation in closed:
        if abs(equation.constant) > _RESIDUAL_TOLERANCE:
            raise SimulationError(
                f"contradictory equation {equation.source!r}: with everything else known "
                f"it reduces to {equation.constant:.6g} == 0"
            )


def _changes_for(properties: dict[str, dict], values: dict[Unknown, float]) -> list[Change]:
    """Old/new pairs for a set of solved values against a baseline
    properties dict -- pure, no DB write. Shared by every step of a
    preview (which never writes) and by the final commit (which does)."""
    by_mote: dict[str, dict[str, float]] = {}
    for (mote_id, key), value in values.items():
        by_mote.setdefault(mote_id, {})[key] = value

    changes = []
    for mote_id, new_properties in sorted(by_mote.items()):
        existing = properties.get(mote_id, {})
        for key, value in sorted(new_properties.items()):
            old = existing.get(key)
            changes.append(
                Change(mote_id, key, old if isinstance(old, (int, float)) else None, value)
            )
    return changes


def _write_back(
    conn: sqlite3.Connection, properties: dict[str, dict], values: dict[Unknown, float]
) -> list[Change]:
    changes = _changes_for(properties, values)
    by_mote: dict[str, dict[str, float]] = {}
    for (mote_id, key), value in values.items():
        by_mote.setdefault(mote_id, {})[key] = value
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

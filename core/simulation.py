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

Static only: there is no notion of a previous timestep yet. The tick
counter is recorded for provenance so time-dependent behaviour can build
on it later.
"""
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone

import numpy as np

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
from core.motes import list_motes, update_mote
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


@dataclass(frozen=True)
class SimulationResult:
    run_id: str
    tick: int
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
            f"(tick {self.tick})."
        )
        if self.unsolved:
            text += f" {len(self.unsolved)} declared unknown(s) had no equation."
        return text


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
    (after logging the failed run) if the rules can't be satisfied."""
    tick = _next_tick(conn)
    view = _build_view(conn)
    pairs = _rule_instances(conn, view)

    try:
        solved: dict[Unknown, float] = {}
        equations = _resolve(view, pairs, solved)
        solved.update(_matrix_solve(equations, solved))
    except SimulationError as e:
        _log_run(conn, tick, "error", str(e), [])
        logger.warning("simulation tick %s failed: %s", tick, e)
        raise

    changes = _write_back(conn, view, solved)
    run_id = _log_run(conn, tick, "ok", None, changes)
    logger.info("simulation tick %s solved %s values", tick, len(solved))
    return SimulationResult(
        run_id=run_id,
        tick=tick,
        values=solved,
        changes=changes,
        equation_count=len(equations),
        unsolved=_unsolved(view, solved),
    )


def _build_view(conn: sqlite3.Connection) -> _GardenView:
    properties = {m.id: m.properties for m in list_motes(conn)}
    out_edges: dict[tuple[str, str], list[str]] = {}
    in_edges: dict[tuple[str, str], list[str]] = {}
    unknowns: dict[str, set[str]] = {}

    for rel in list_relationships(conn):
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
    conn: sqlite3.Connection, view: _GardenView
) -> list[tuple[rules_repo.Rule, str]]:
    """Every (rule, instance) pair the run will evaluate."""
    pairs = []
    for rule in rules_repo.list_rules(conn):
        for mote_id in view.in_edges.get((rule.species_id, species_repo.INSTANCE_OF), []):
            pairs.append((rule, mote_id))
    return pairs


def _context(view: _GardenView, mote_id: str, solved: dict[Unknown, float]) -> Context:
    return Context(
        mote_id=mote_id,
        properties=lambda mid: view.properties.get(mid, {}),
        unknown_keys=lambda mid: view.unknowns.get(mid, set()),
        targets=lambda mid, type_: view.out_edges.get((mid, type_), []),
        sources=lambda mid, type_: view.in_edges.get((mid, type_), []),
        solved=solved,
    )


def _resolve(
    view: _GardenView,
    pairs: list[tuple[rules_repo.Rule, str]],
    solved: dict[Unknown, float],
) -> list[Equation]:
    """Substitution loop. Returns the equations as evaluated against the
    final set of substituted values, ready for the matrix solve."""
    equations: list[Equation] = []
    deferred: list[tuple[rules_repo.Rule, str, Exception]] = []

    while True:
        equations, deferred = _evaluate_all(view, pairs, solved)
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
) -> tuple[list[Equation], list[tuple[rules_repo.Rule, str, Exception]]]:
    """Nonlinear failures are deferred rather than fatal -- the offending
    unknown may still be determined by another equation, which would make
    this one evaluable on a later pass."""
    equations, deferred = [], []
    for rule, mote_id in pairs:
        try:
            equations.append(evaluate_equation(rule.equation, _context(view, mote_id, solved)))
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


def _write_back(
    conn: sqlite3.Connection, view: _GardenView, values: dict[Unknown, float]
) -> list[Change]:
    by_mote: dict[str, dict[str, float]] = {}
    for (mote_id, key), value in values.items():
        by_mote.setdefault(mote_id, {})[key] = value

    changes = []
    for mote_id, new_properties in sorted(by_mote.items()):
        existing = view.properties.get(mote_id, {})
        for key, value in sorted(new_properties.items()):
            old = existing.get(key)
            changes.append(
                Change(mote_id, key, old if isinstance(old, (int, float)) else None, value)
            )
        update_mote(conn, mote_id, new_properties)
    return changes


def _unsolved(view: _GardenView, solved: dict[Unknown, float]) -> list[Unknown]:
    declared = {(mote_id, key) for mote_id, keys in view.unknowns.items() for key in keys}
    return sorted(declared - set(solved))


def _next_tick(conn: sqlite3.Connection) -> int:
    """Derived rather than stored -- the log already knows how many runs
    this garden has seen."""
    cursor = conn.cursor()
    cursor.execute("SELECT COALESCE(MAX(tick), 0) + 1 AS next FROM simulation_runs")
    return int(cursor.fetchone()["next"])


def _log_run(
    conn: sqlite3.Connection,
    tick: int,
    status: str,
    detail: str | None,
    changes: list[Change],
) -> str:
    run_id = generate_uuid()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO simulation_runs (id, tick, ran_at, status, detail) VALUES (?, ?, ?, ?, ?)",
        (run_id, tick, datetime.now(timezone.utc).isoformat(), status, detail),
    )
    cursor.executemany(
        "INSERT INTO simulation_changes (run_id, mote_id, key, old_value, new_value) "
        "VALUES (?, ?, ?, ?, ?)",
        [
            (run_id, c.mote_id, c.key, _text(c.old_value), _text(c.new_value))
            for c in changes
        ],
    )
    conn.commit()
    return run_id


def _text(value: float | None) -> str | None:
    return None if value is None else repr(value)

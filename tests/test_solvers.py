import pytest

import core.solver_registry  # noqa: F401 -- registers "linear"/"newton"
from core.formula import Equation
from core.linear_solver import LinearSolver
from core.newton_solver import NewtonSolver
from core.solvers import (
    ConvergenceError,
    DeferredEquation,
    Remainder,
    SolverError,
    SolverNotFound,
    list_solvers,
    resolve_solver,
)

# core/solvers.py takes plain dataclasses/callables rather than a
# connection or a real rule/mote graph -- same reasoning as
# tests/test_formula.py's Context, kept DB-free and trivially testable.

X = ("m", "x")
Y = ("m", "y")


def deferred(reevaluate, references, rule_name="rule", mote_id="m", source="", detail=""):
    return DeferredEquation(
        rule_name=rule_name,
        mote_id=mote_id,
        source=source,
        detail=detail,
        references=frozenset(references),
        reevaluate=reevaluate,
    )


# ---- registry ----

def test_registry_has_both_built_ins():
    assert set(list_solvers()) >= {"linear", "newton"}
    assert resolve_solver("linear") is LinearSolver
    assert resolve_solver("newton") is NewtonSolver


def test_unknown_solver_name_lists_available():
    with pytest.raises(SolverNotFound, match="bogus"):
        resolve_solver("bogus")


# ---- LinearSolver ----

def test_linear_solver_solves_a_simultaneous_system():
    # x + y == 3, x - y == 1 -> x=2, y=1
    remainder = Remainder([
        Equation({X: 1.0, Y: 1.0}, -3.0),
        Equation({X: 1.0, Y: -1.0}, -1.0),
    ])
    result = LinearSolver({}).solve(remainder, {}, {X, Y}, lambda u: 1.0)
    assert result[X] == pytest.approx(2.0)
    assert result[Y] == pytest.approx(1.0)


def test_linear_solver_underdetermined():
    remainder = Remainder([Equation({X: 1.0, Y: 1.0}, -3.0)])
    with pytest.raises(SolverError, match="underdetermined"):
        LinearSolver({}).solve(remainder, {}, {X, Y}, lambda u: 1.0)


def test_linear_solver_inconsistent():
    remainder = Remainder([
        Equation({X: 1.0}, -1.0),
        Equation({X: 1.0}, -2.0),
    ])
    with pytest.raises(SolverError, match="inconsistent"):
        LinearSolver({}).solve(remainder, {}, {X}, lambda u: 1.0)


def test_linear_solver_contradictory_closed_equation():
    remainder = Remainder([Equation({}, 5.0)])
    with pytest.raises(SolverError, match="contradictory"):
        LinearSolver({}).solve(remainder, {}, set(), lambda u: 1.0)


def test_linear_solver_refuses_deferred_equations():
    d = deferred(lambda guess: Equation({}, 0.0), {X}, rule_name="area", detail="nonlinear")
    remainder = Remainder([], [d])
    with pytest.raises(SolverError, match="area.*could not be resolved"):
        LinearSolver({}).solve(remainder, {}, {X}, lambda u: 1.0)


def test_linear_solver_points_at_the_newton_backend():
    # The refusal is the one place that knows both that a rule was turned
    # down for being nonlinear and that another backend would take it, so
    # it says so rather than leaving the reader to find that out from the
    # source.
    d = deferred(lambda guess: Equation({}, 0.0), {X}, rule_name="area", detail="nonlinear")
    with pytest.raises(SolverError, match="newton"):
        LinearSolver({}).solve(Remainder([], [d]), {}, {X}, lambda u: 1.0)


# ---- NewtonSolver ----

def test_newton_solver_converges_on_a_nonlinear_equation():
    # x*x - 4 == 0, starting on the positive side -> x = 2
    d = deferred(lambda guess: Equation({}, guess[X] ** 2 - 4.0), {X})
    remainder = Remainder([], [d])
    result = NewtonSolver({}).solve(remainder, {}, {X}, lambda u: 3.0)
    assert result[X] == pytest.approx(2.0, abs=1e-6)


def test_newton_solver_combines_linear_and_nonlinear_equations():
    # y - 2x == 0 (linear), x*x - 4 == 0 (nonlinear) -> x=2, y=4
    d = deferred(lambda guess: Equation({}, guess[X] ** 2 - 4.0), {X})
    remainder = Remainder([Equation({Y: 1.0, X: -2.0}, 0.0)], [d])
    result = NewtonSolver({}).solve(remainder, {}, {X, Y}, lambda u: 3.0)
    assert result[X] == pytest.approx(2.0, abs=1e-6)
    assert result[Y] == pytest.approx(4.0, abs=1e-6)


def test_newton_solver_respects_already_known_values():
    # x*y - 6 == 0, y already known == 3 -> x = 2
    d = deferred(lambda guess: Equation({}, guess[X] * guess[Y] - 6.0), {X, Y})
    remainder = Remainder([], [d])
    result = NewtonSolver({}).solve(remainder, {Y: 3.0}, {X, Y}, lambda u: 3.0)
    assert result == {X: pytest.approx(2.0, abs=1e-6)}


def test_newton_solver_raises_on_no_root():
    # x*x + 4 == 0 has no real root -- must not converge
    d = deferred(lambda guess: Equation({}, guess[X] ** 2 + 4.0), {X})
    remainder = Remainder([], [d])
    with pytest.raises(ConvergenceError):
        NewtonSolver({"max_iterations": 5}).solve(remainder, {}, {X}, lambda u: 1.0)


def test_newton_solver_underdetermined_before_iterating():
    d = deferred(lambda guess: Equation({}, guess[X] + guess[Y]), {X, Y})
    remainder = Remainder([], [d])
    with pytest.raises(SolverError, match="underdetermined"):
        NewtonSolver({}).solve(remainder, {}, {X, Y}, lambda u: 1.0)


def test_newton_solver_no_unknowns_is_a_no_op():
    assert NewtonSolver({}).solve(Remainder([]), {X: 1.0}, {X}, lambda u: 1.0) == {}

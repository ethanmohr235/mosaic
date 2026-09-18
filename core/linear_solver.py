# core/linear_solver.py
"""The default solver backend: today's exact, non-iterative behavior.
Fails outright if anything genuinely nonlinear survived the substitution
loop -- see core/newton_solver.py for a backend that can pick that up
instead. Moved from core/simulation.py's old _matrix_solve/
_check_consistency verbatim; only the exception type changed (SolverError
rather than SimulationError, normalized back to SimulationError by
core/simulation.py). The deferred-equation message has since gained a
pointer to `newton`: this backend is the only place that knows both that
a rule was refused for being nonlinear and that another backend would
take it, so saying so here saves the reader going to the source to find
out an escape hatch exists.
"""
import numpy as np

from core.formula import Equation, Unknown, describe
from core.solvers import Remainder, SingularSystemError, SolverBackend, SolverError

_RESIDUAL_TOLERANCE = 1e-6
# Relative slack on top of the absolute floor above, sized to the largest
# magnitude an equation's own arithmetic touched (Equation.scale / a row's
# LinearExpr.scale) rather than to the equation's value in isolation. A
# rule mixing far-apart base units (kW against MeV, say) routinely
# produces intermediate values around 1e15; float64 carries ~16
# significant digits, so two equal ways of reaching that same value can
# differ by several times DBL_EPSILON once real arithmetic (not just a
# single rounding) is involved. 1e-9 is generous next to that noise floor
# while still catching a genuine contradiction, which disagrees by a
# fraction of the actual quantity, not a few parts in 1e15.
_RELATIVE_TOLERANCE = 1e-9


def _tolerance(scale: float) -> float:
    return max(_RESIDUAL_TOLERANCE, abs(scale) * _RELATIVE_TOLERANCE)


class LinearSolver(SolverBackend):
    def solve(self, remainder, known, candidates, initial_guess):
        if remainder.deferred:
            first = remainder.deferred[0]
            raise SolverError(
                f"rule {first.rule_name!r} on mote {first.mote_id[:8]} could not be "
                f"resolved: {first.detail}. This is the 'linear' solver, which only "
                f"handles equations that are linear in their unknowns -- either "
                f"restate the rule so it is (an attenuation law `I == I0 * exp(-k*x)` "
                f"becomes `k * x == -ln(I / I0)`), or re-run with solver='newton', "
                f"which solves it as written from an initial guess"
            )
        return _matrix_solve(remainder.open_equations)


def _matrix_solve(equations: list[Equation]) -> dict[Unknown, float]:
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
        raise SingularSystemError(
            f"underdetermined: {len(unknowns)} unknowns but only {rank} independent "
            f"equations. Unknowns: {describe(unknowns)}"
        )
    residual = float(np.abs(matrix @ solution - constants).max())
    scale = max((e.scale for e in open_equations), default=0.0)
    if residual > _tolerance(scale):
        raise SolverError(
            f"inconsistent: no values satisfy all {len(open_equations)} equations "
            f"(worst mismatch {residual:.3g}). Unknowns: {describe(unknowns)}"
        )
    return {unknown: float(solution[i]) + 0.0 for unknown, i in index.items()}


def _check_consistency(closed: list[Equation]) -> None:
    """An equation with no unknowns left is either satisfied or a flat
    contradiction between rules."""
    for equation in closed:
        if abs(equation.constant) > _tolerance(equation.scale):
            raise SolverError(
                f"contradictory equation {equation.source!r}: with everything else known "
                f"it reduces to {equation.constant:.6g} == 0"
            )

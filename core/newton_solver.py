# core/newton_solver.py
"""A solver backend for equations that are genuinely nonlinear in their
unknowns (x*x, sqrt(x), a property() model whose inputs include an
unknown) -- not just nonlinear over already-known values, which the
substitution loop in core/simulation.py already resolves for free before
either backend ever runs. Finds a root of the combined residual vector by
Newton-Raphson with a numeric (finite-difference) Jacobian and simple
backtracking, since scipy isn't a dependency here -- np.linalg.lstsq
handles a rank-deficient Jacobian the same way core/linear_solver.py's
_matrix_solve already relies on for the linear case.
"""
from collections.abc import Callable

import numpy as np

from core.formula import Unknown, describe
from core.solvers import ConvergenceError, Remainder, SingularSystemError, SolverBackend, SolverError

_DEFAULT_MAX_ITERATIONS = 50
_DEFAULT_TOLERANCE = 1e-9
_DEFAULT_EPSILON = 1e-6
_MAX_BACKTRACKS = 10


class NewtonSolver(SolverBackend):
    """Configuration: max_iterations (int, default 50), tolerance (float,
    default 1e-9, on the max absolute residual), epsilon (float, default
    1e-6, the finite-difference step used to build the Jacobian)."""

    def solve(self, remainder, known, candidates, initial_guess):
        unknowns = sorted(candidates - known.keys())
        if not unknowns:
            return {}

        residuals = _residual_functions(remainder, unknowns)
        if len(residuals) < len(unknowns):
            raise SolverError(
                f"underdetermined: {len(unknowns)} unknowns but only {len(residuals)} "
                f"equations. Unknowns: {describe(unknowns)}"
            )

        max_iterations = self.configuration.get("max_iterations", _DEFAULT_MAX_ITERATIONS)
        tolerance = self.configuration.get("tolerance", _DEFAULT_TOLERANCE)
        epsilon = self.configuration.get("epsilon", _DEFAULT_EPSILON)

        x = np.array([initial_guess(u) for u in unknowns], dtype=float)
        residual = _evaluate(residuals, known, unknowns, x)
        norm = float(np.abs(residual).max()) if len(residual) else 0.0

        for iteration in range(max_iterations):
            if norm < tolerance:
                return dict(zip(unknowns, x))

            jacobian = _jacobian(residuals, known, unknowns, x, residual, epsilon)
            step, _, rank, _ = np.linalg.lstsq(jacobian, -residual, rcond=None)

            improved = False
            scale = 1.0
            for _ in range(_MAX_BACKTRACKS):
                candidate_x = x + scale * step
                candidate_residual = _evaluate(residuals, known, unknowns, candidate_x)
                candidate_norm = float(np.abs(candidate_residual).max())
                if candidate_norm < norm:
                    x, residual, norm = candidate_x, candidate_residual, candidate_norm
                    improved = True
                    break
                scale /= 2.0

            if not improved:
                if rank < len(unknowns):
                    raise SingularSystemError(
                        f"singular system: the equations don't independently constrain "
                        f"every unknown. Unknowns: {describe(unknowns)}"
                    )
                raise ConvergenceError(
                    f"stalled after {iteration + 1} iteration(s) (residual {norm:.3g}). "
                    f"Unknowns: {describe(unknowns)}"
                )

        if norm < tolerance:
            return dict(zip(unknowns, x))
        raise ConvergenceError(
            f"did not converge within {max_iterations} iterations "
            f"(residual {norm:.3g}). Unknowns: {describe(unknowns)}"
        )


def _residual_functions(
    remainder: Remainder, unknowns: list[Unknown]
) -> list[Callable[[dict[Unknown, float]], float]]:
    """One callable per equation, each taking a *complete* guess dict and
    returning the scalar residual `lhs - rhs` at that point."""
    functions: list[Callable[[dict[Unknown, float]], float]] = []
    for equation in remainder.open_equations:
        functions.append(
            lambda guess, e=equation: e.constant + sum(c * guess[u] for u, c in e.terms.items())
        )
    for deferred in remainder.deferred:
        def evaluate(guess, d=deferred):
            result = d.reevaluate(guess)
            if result.terms:
                raise SolverError(
                    f"rule {d.rule_name!r} on mote {d.mote_id[:8]} still references an "
                    f"unknown outside {{{describe(unknowns)}}}: {describe(result.terms)}"
                )
            return result.constant
        functions.append(evaluate)
    return functions


def _evaluate(
    residuals: list[Callable[[dict[Unknown, float]], float]],
    known: dict[Unknown, float],
    unknowns: list[Unknown],
    x: np.ndarray,
) -> np.ndarray:
    guess = dict(known)
    guess.update(zip(unknowns, x))
    return np.array([f(guess) for f in residuals])


def _jacobian(
    residuals: list[Callable[[dict[Unknown, float]], float]],
    known: dict[Unknown, float],
    unknowns: list[Unknown],
    x: np.ndarray,
    base_residual: np.ndarray,
    epsilon: float,
) -> np.ndarray:
    jacobian = np.zeros((len(residuals), len(unknowns)))
    for col in range(len(unknowns)):
        perturbed = x.copy()
        perturbed[col] += epsilon
        jacobian[:, col] = (_evaluate(residuals, known, unknowns, perturbed) - base_residual) / epsilon
    return jacobian

# core/solver_registry.py
"""Registers every built-in solver backend -- the single explicit
registration point, mirroring core/model_resolver.py's role for property
providers. Anything that resolves a solver imports this module first, so
registration always happens before resolution; backends are never
resolved via dynamic import from a caller-supplied string beyond the
plain name lookup in core/solvers.py's registry.
"""
from core.linear_solver import LinearSolver
from core.newton_solver import NewtonSolver
from core.solvers import SolverBackend, register_solver


class AutoSolver(SolverBackend):
    """The default: `linear` when what is left is linear, `newton` when it
    is not -- so a garden that needs iteration solves everywhere it is
    read (a document, a plot, `solve`) without anyone naming a solver.
    Linear is exact and tried first; newton only ever sees the remainder
    the substitution loop could not finish. `used` says which ran."""

    used: str | None = None

    def solve(self, remainder, known, candidates, initial_guess):
        backend = NewtonSolver if remainder.deferred else LinearSolver
        self.used = "newton" if remainder.deferred else "linear"
        return backend(self.configuration).solve(remainder, known, candidates, initial_guess)


register_solver("auto", AutoSolver)
register_solver("linear", LinearSolver)
register_solver("newton", NewtonSolver)

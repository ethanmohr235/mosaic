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
from core.solvers import register_solver

register_solver("linear", LinearSolver)
register_solver("newton", NewtonSolver)

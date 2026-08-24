# core/solvers.py
"""The solver-backend contract: the compatibility layer between a
Remainder of equations the substitution loop couldn't fully resolve
(core/simulation.py's `_reduce`) and an actual numerical strategy for
finishing the job. Mirrors core/providers.py's PropertyProvider contract
-- a named, registered, per-invocation-selectable backend -- but scoped
to solving, not physical properties, so it stays free of any DB or
core/simulation.py import.

Solver choice is necessarily a whole-run decision rather than a per-mote
one (unlike `uses_model`): a linear-algebra solve can't mix backends
mid-equation-system, so there's no per-mote relationship to resolve here,
just a plain name passed by the caller.
"""
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field

from core.formula import Equation, FormulaError, Unknown


class SolverError(FormulaError):
    """Base for every solver-backend error. Subclasses FormulaError so
    these flow through core/simulation.py's existing error handling
    alongside FormulaError/NonlinearError, with the same normalization to
    SimulationError there."""


class SolverNotFound(SolverError):
    """No solver backend is registered under the requested name."""


class ConvergenceError(SolverError):
    """An iterative solver ran out of iterations without converging."""


class SingularSystemError(SolverError):
    """The system (linear, or the final Jacobian of a nonlinear solve) is
    rank-deficient -- underdetermined, not merely slow to converge."""


@dataclass(frozen=True)
class DeferredEquation:
    """One (rule, mote) pair whose equation is genuinely nonlinear in its
    unknowns -- NonlinearError survived the substitution loop's repeated
    retries. `reevaluate` re-evaluates the original equation given a
    *complete* guess for every unknown it references; with nothing left
    unsubstituted, NonlinearError structurally cannot fire (see
    core/formula.py's LinearExpr arithmetic), so the result's `.constant`
    is exactly the scalar residual `lhs - rhs` at that guess point."""
    rule_name: str
    mote_id: str
    source: str
    detail: str
    references: frozenset[Unknown]
    reevaluate: Callable[[dict[Unknown, float]], Equation]


@dataclass(frozen=True)
class Remainder:
    """What core/simulation.py's substitution loop couldn't resolve on
    its own, handed to the selected solver backend's final stage."""
    open_equations: list[Equation]
    deferred: list[DeferredEquation] = field(default_factory=list)


class SolverBackend(ABC):
    """Base class for every solver backend. A backend is unaware of the
    broader mote graph -- it only knows its own configuration and how to
    finish solving a Remainder."""

    def __init__(self, configuration: dict):
        self.configuration = configuration

    @abstractmethod
    def solve(
        self,
        remainder: Remainder,
        known: dict[Unknown, float],
        candidates: set[Unknown],
        initial_guess: Callable[[Unknown], float],
    ) -> dict[Unknown, float]:
        """Solve for every unknown in `candidates` not already in `known`,
        using `remainder`'s equations. Returns only the newly-solved
        values (not merged with `known`). `initial_guess` gives a
        starting value for an iterative backend to seed an unknown it
        has no other information about."""


_SOLVERS: dict[str, type[SolverBackend]] = {}


def register_solver(name: str, cls: type[SolverBackend]) -> None:
    _SOLVERS[name] = cls


def resolve_solver(name: str) -> type[SolverBackend]:
    try:
        return _SOLVERS[name]
    except KeyError:
        available = ", ".join(sorted(_SOLVERS)) or "(none registered)"
        raise SolverNotFound(
            f"no solver registered as {name!r}. Available solvers: {available}"
        ) from None


def list_solvers() -> list[str]:
    return sorted(_SOLVERS)

# core/incidence.py
"""Which equations touch which values, and the questions that answers.

This is the bipartite incidence relation between variables and equations
-- the structure a constraint system has instead of a dependency DAG. An
equation does not have inputs and outputs; it relates several values, and
which of them it can determine depends on which are free (a mote's
declared freedoms). `V == I * R` produces V for one garden and I for
another, from the same text.

**The index is derived, never stored.** It is rebuilt from the rules on
every query, because a persisted copy would be a second account of the
same equations and would drift from them. core/dimensions.py made the
same call in reusing core/formula.py's whitelist rather than copying it.

Nothing here parses or walks an expression: this module holds only the
graph math, and the touch sets it works on are produced by
core/simulation.py using the real evaluator. That split is deliberate and
is what makes "one interpretation of the model" structural rather than a
promise -- there is no second parser here to fall out of step with the
first. See tests/test_incidence.py's drift test.
"""
from collections.abc import Callable
from dataclasses import dataclass, field

from core.formula import Unknown

# Why a prerequisite is or isn't satisfied.
STORED = "stored"      # the mote holds a value for it
DERIVED = "derived"    # free, and some equation can determine it
MISSING = "missing"    # no value, and nothing can determine it


@dataclass(frozen=True)
class EquationTouch:
    """One (rule, instance) equation and every value it references.

    `determines` and `reads` partition those values by whether they are
    free on the mote that owns them -- which is a fact about the garden's
    declared freedoms, not about the equation. The same equation text
    lands differently in two gardens, and that is correct."""
    rule_mote_id: str
    rule_name: str
    anchor_mote_id: str
    equation: str
    determines: frozenset[Unknown] = frozenset()
    reads: frozenset[Unknown] = frozenset()

    @property
    def touches(self) -> frozenset[Unknown]:
        return self.determines | self.reads


@dataclass(frozen=True)
class Effect:
    """One value that could move if some other value changed, and the
    equations that carry the change to it."""
    variable: Unknown
    distance: int
    through: tuple[EquationTouch, ...]


@dataclass(frozen=True)
class Need:
    """One prerequisite for determining a value."""
    variable: Unknown
    status: str


@dataclass(frozen=True)
class Requirement:
    """What determining a value depends on, transitively."""
    variable: Unknown
    free: bool
    producers: tuple[EquationTouch, ...]
    needs: tuple[Need, ...]

    @property
    def determinable(self) -> bool:
        """Whether anything in the garden constrains this value at all.
        False is the single most common modelling mistake: a property
        nobody declared free, or declared free and never constrained."""
        return self.free and bool(self.producers)

    @property
    def missing(self) -> tuple[Need, ...]:
        return tuple(n for n in self.needs if n.status == MISSING)


@dataclass(frozen=True)
class Index:
    """Every equation in the garden, indexed by the values it touches."""
    touches: tuple[EquationTouch, ...] = ()
    freedoms: frozenset[Unknown] = frozenset()
    has_value: Callable[[Unknown], bool] = field(default=lambda _variable: False)

    def is_free(self, variable: Unknown) -> bool:
        return variable in self.freedoms

    def producers(self, variable: Unknown) -> tuple[EquationTouch, ...]:
        """Equations that could determine this value. Empty for a value
        no mote declared free -- nothing can determine what isn't
        allowed to move."""
        return tuple(t for t in self.touches if variable in t.determines)

    def consumers(self, variable: Unknown) -> tuple[EquationTouch, ...]:
        """Equations that read this value as a known quantity."""
        return tuple(t for t in self.touches if variable in t.reads)

    def touching(self, variable: Unknown) -> tuple[EquationTouch, ...]:
        return tuple(t for t in self.touches if variable in t.touches)

    def impact(self, variable: Unknown, max_distance: int | None = None) -> tuple[Effect, ...]:
        """Every value that could move if this one changed, nearest first.

        A breadth-first walk of the bipartite graph: a change perturbs
        every equation touching the value, each perturbed equation can
        move any value it is free to determine, and those in turn perturb
        further equations. This is not a forward walk of a dependency
        DAG -- in a coupled block every value moves together, and the
        walk reflects that rather than imposing an order the solve never
        had.

        A value nothing is free to determine can be a cause but never an
        effect, so it never appears here.

        This is a **sound over-approximation**: everything that could be
        affected is listed, but not everything listed necessarily moves.
        The analysis is structural, not numerical -- it knows an equation
        relates several values, not which of them a particular solve will
        end up pinning. A pump's specific volume, fixed by its own inlet
        state, still appears when the turbine work it shares an equation
        with changes. "Could move" is meant literally; tightening it
        would mean predicting the solve, which is what running the solve
        is for.
        """
        visited = {variable}
        frontier = {variable}
        effects: list[Effect] = []
        distance = 0

        while frontier and (max_distance is None or distance < max_distance):
            distance += 1
            reached: dict[Unknown, list[EquationTouch]] = {}
            for touch in self.touches:
                if not touch.touches & frontier:
                    continue
                for moved in touch.determines - visited:
                    reached.setdefault(moved, []).append(touch)
            if not reached:
                break
            for moved in sorted(reached):
                effects.append(Effect(moved, distance, tuple(reached[moved])))
            visited |= set(reached)
            frontier = set(reached)

        return tuple(effects)

    def requires(self, variable: Unknown) -> Requirement:
        """What must be known before this value can be determined.

        Transitive: a prerequisite that is itself derived contributes its
        own prerequisites. The closure is flat rather than a tree because
        a coupled block has no tree -- the same reason `why` stops at a
        solver block instead of expanding it.
        """
        producers = self.producers(variable)
        needs: dict[Unknown, str] = {}
        visited = {variable}
        frontier = {variable}

        while frontier:
            nxt: set[Unknown] = set()
            for touch in self.touches:
                if not touch.determines & frontier:
                    continue
                for candidate in touch.touches - visited:
                    status = self._status(candidate)
                    needs[candidate] = status
                    if status == DERIVED:
                        nxt.add(candidate)
            visited |= nxt
            frontier = nxt

        return Requirement(
            variable=variable,
            free=self.is_free(variable),
            producers=producers,
            needs=tuple(Need(v, needs[v]) for v in sorted(needs)),
        )

    def _status(self, variable: Unknown) -> str:
        """Freedom is checked before any stored value, because a free
        property may still hold the number a previous run left on it.
        That value is about to be re-solved, so reporting it as a stored
        input would describe the garden rather than the model."""
        if self.is_free(variable):
            return DERIVED if self.producers(variable) else MISSING
        return STORED if self.has_value(variable) else MISSING

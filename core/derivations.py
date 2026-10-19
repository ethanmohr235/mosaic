# core/derivations.py
"""Explaining a value: which equations determined it, from which inputs.

`why` and `\\work` explain the garden **as it stands**, from a live solve
(core/simulation.py's `solve_journal`), not from anything stored. A
record kept from "the last solve" can go stale the moment a given is
edited, and a number that explains itself with last week's inputs is the
exact failure Mosaic exists to avoid; a solve is cheap, so it is simply
done again. Nothing here writes.

An `Explainer` is one such solve, made the first time something is asked
of it and shared by every question after -- a document with forty `\\work`
blocks solves once. Explaining is a traversal, not a reconstruction: each
value names the block that produced it, each block names its inputs, and
an input that the solve derived is itself a value to explain. The
recursion terminates on anything the solve did not derive -- a stored
property, a property-model evaluation, a prev() read of the garden --
which is exactly the set of things that need no explanation.

Values here are in each dimension's base SI unit, the units the solver
worked in. See core/provenance.py for why they are not converted back.
"""
import sqlite3
from dataclasses import dataclass, field

from core import simulation
from core.formula import Unknown
from core.provenance import DERIVED_READ, Derivation, Journal

# How deep `explain` follows derived inputs before saying so rather than
# recursing further. Generous -- this is a guard against a pathological
# model, not a display limit.
DEFAULT_DEPTH = 32

NO_SOLVE_NOTE = "no equation in this garden determines a value"
UNDETERMINED_NOTE = "not determined by a solve -- set directly, or unchanged"


@dataclass(frozen=True)
class EquationRecord:
    """One equation that took part in a block."""
    rule_mote_id: str
    rule_name: str
    anchor_mote_id: str
    equation: str


@dataclass(frozen=True)
class ExplainedInput:
    """One known value a block consumed. `derivation` is filled in when
    the solve determined it too, which is what makes `why` recursive."""
    kind: str
    mote_id: str
    key: str
    value: float
    derivation: "Explanation | None" = None


@dataclass(frozen=True)
class Explanation:
    """Why one value is what it is.

    `equations` and `inputs` are empty when nothing derived the value --
    `note` then says why, and that is a complete answer rather than a
    failure. `also_determined` names the other values found in the same
    block, which is the part a tree-shaped explanation cannot express: a
    solver block's values were found together and no smaller attribution
    of them exists.
    """
    mote_id: str
    key: str
    value: float | None
    method: str | None = None
    detail: str | None = None
    equations: tuple[EquationRecord, ...] = ()
    inputs: tuple[ExplainedInput, ...] = ()
    also_determined: tuple[Unknown, ...] = ()
    note: str | None = None

    @property
    def derived(self) -> bool:
        return self.method is not None


@dataclass(frozen=True)
class Index:
    """One solve's derivations, found by the value each determined. When
    two blocks determined the same value the later one wins -- it is the
    one that stands."""
    by_value: dict[Unknown, Derivation]
    error: str | None = None

    @property
    def empty(self) -> bool:
        return not self.by_value


def index(journal: Journal, error: str | None = None) -> Index:
    found: dict[Unknown, Derivation] = {}
    for derivation in journal.derivations:
        for value in derivation.values:
            found[value] = derivation
    return Index(found, error)


@dataclass
class Explainer:
    """The garden's explanations, asked of one live solve made on first
    use. `overrides` are knobs (core/knobs.py) over `scenario`'s."""
    conn: sqlite3.Connection
    scenario: str | None = None
    solver: str = "linear"
    overrides: dict | None = None
    _live: simulation.LiveGarden | None = field(default=None, init=False, repr=False)
    _index: Index | None = field(default=None, init=False, repr=False)

    @property
    def live(self) -> simulation.LiveGarden:
        """The garden as this solve would write it -- what a document's
        values read, so `\\val` and `\\work` come from the one solve."""
        if self._live is None:
            self._live = simulation.live_garden(
                self.conn, self.scenario, self.solver, self.overrides)
        return self._live

    @property
    def index(self) -> Index:
        if self._index is None:
            self._index = index(self.live.journal, self.live.error)
        return self._index

    def explain(self, mote_id: str, key: str, depth: int = DEFAULT_DEPTH) -> Explanation:
        """Why `mote_id.key` holds the value it does."""
        found = self.index
        if found.empty:
            note = f"the garden does not solve: {found.error}" if found.error else NO_SOLVE_NOTE
            return Explanation(mote_id, key, None, note=note)
        return _explain(found, mote_id, key, depth, frozenset())

    def solved(self) -> list[Unknown]:
        """Every value the solve determined, sorted."""
        return sorted(self.index.by_value)


def _explain(
    found: Index, mote_id: str, key: str, depth: int, visited: frozenset[Unknown]
) -> Explanation:
    block = found.by_value.get((mote_id, key))
    if block is None:
        return Explanation(mote_id, key, None, note=UNDETERMINED_NOTE)

    value = block.values[(mote_id, key)]
    if (mote_id, key) in visited:
        return Explanation(mote_id, key, value, method=block.method,
                           note="already explained above")
    if depth <= 0:
        return Explanation(mote_id, key, value, method=block.method,
                           note="beyond the requested depth")

    seen = visited | {(mote_id, key)}
    return Explanation(
        mote_id=mote_id,
        key=key,
        value=value,
        method=block.method,
        detail=block.detail,
        equations=_equations(block),
        inputs=_inputs(found, block, depth - 1, seen),
        also_determined=tuple(sorted(v for v in block.values if v != (mote_id, key))),
    )


def _equations(block: Derivation) -> tuple[EquationRecord, ...]:
    records = (EquationRecord(o.rule_mote_id, o.rule_name, o.anchor_mote_id, o.equation)
               for o in block.equations)
    return tuple(sorted(records, key=lambda r: (r.rule_name, r.anchor_mote_id)))


def _inputs(
    found: Index, block: Derivation, depth: int, visited: frozenset[Unknown]
) -> tuple[ExplainedInput, ...]:
    reads = sorted(block.inputs, key=lambda r: (r.kind, r.mote_id, r.key))
    return tuple(
        ExplainedInput(r.kind, r.mote_id, r.key, r.value,
                       _input_derivation(found, r.kind, r.mote_id, r.key, depth, visited))
        for r in reads
    )


def _input_derivation(
    found: Index, kind: str, mote_id: str, key: str, depth: int, visited: frozenset[Unknown]
) -> Explanation | None:
    """Only a value the solve itself derived is worth following. A prev()
    read in a static solve is the value the garden held going in, which
    is as much a given as any stored property."""
    if kind == DERIVED_READ:
        return _explain(found, mote_id, key, depth, visited)
    return None

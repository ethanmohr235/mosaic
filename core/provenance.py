# core/provenance.py
"""Recording what a solve actually did, so a value can be explained.

Pure and DB-free, like core/formula.py's Context and
core/model_resolver.py: it takes plain values and callables rather than a
connection, so core/simulation.py can hand it to a solve and
core/derivations.py can persist the result without this module knowing
that either exists.

core/simulation.py's substitution loop is already a derivation generator
that throws its work away. Every pass does exactly one explicable thing:
*this equation, on this mote, with these known inputs, determined this
one value.* What it cannot explain is the remainder handed to a solver
backend -- when five equations jointly determine five unknowns there is
no tree, and the honest record is that they were found together.

Both are recorded in one shape: a **block** of equations producing a
block of values. A substitution step is simply a block of size one. That
is why there is no separate representation for the two, and why an
explanation can stop at a solver block without pretending to expand it.

Recorded values are in each dimension's **base SI unit** -- what the
solver actually saw -- not the unit a mote declares. An explanation shows
the arithmetic that happened; converting each input back to its own
mote's declared unit would print an equation whose numbers no longer
multiply out. core/simulation.py converts on write-back instead, which is
where the garden's own representation is restored.
"""
from collections.abc import Callable
from dataclasses import dataclass, field

from core.formula import Unknown

# How a block's values were determined.
SUBSTITUTION = "substitution"
SOLVER = "solver"

# Where an input value came from. A `derived` read is one this same run
# solved earlier, and is the edge an explanation recurses along; the rest
# terminate it.
PROPERTY_READ = "property"
DERIVED_READ = "derived"
PREVIOUS_READ = "previous"
MODEL_READ = "model"
# A property an equation asked for that no mote holds. Only ever recorded
# by a permissive Reads (see below) -- during a real solve this is an
# error, not an observation.
MISSING_READ = "missing"

# Reads whose value comes out of a mote's stored properties, and so
# arrives in whatever unit that mote declares.
_NEEDS_NORMALIZING = frozenset({PROPERTY_READ, PREVIOUS_READ})


@dataclass(frozen=True)
class Origin:
    """Which rule, applied to which mote, produced one equation. The rule
    mote's id is kept alongside its name so an explanation can link back
    to the rule itself rather than only naming it."""
    rule_mote_id: str
    rule_name: str
    anchor_mote_id: str
    equation: str


@dataclass(frozen=True)
class InputRead:
    """One already-known value an equation read while being evaluated."""
    kind: str
    mote_id: str
    key: str
    value: float


@dataclass(frozen=True)
class Derivation:
    """One block: the equations that were solved together, the values
    they jointly determined, and the known values that went in.

    `values` holds the determined values themselves rather than only
    their addresses, so a derivation is self-contained: an explanation
    reads the number the solve produced, in the base units it produced it
    in, without having to reconcile against the change log, which records
    the same numbers converted back into each mote's declared unit."""
    block: int
    method: str
    values: dict[Unknown, float]
    equations: tuple[Origin, ...]
    inputs: tuple[InputRead, ...]
    detail: str | None = None


class Reads:
    """Collects the values one equation evaluation actually read.

    Reading is captured by handing core/formula.py's `_lookup` a dict
    that records what comes out of it, rather than by changing the
    evaluator. That constraint is deliberate: the evaluator's node
    whitelist is the security boundary and stays a flat dispatch with
    nothing else woven through it. core/simulation.py's `_RecordingKnown`
    already established the technique for a different purpose.

    `normalize` converts a stored value into its dimension's base unit --
    the same conversion core/formula.py applies immediately after the
    read, so that what is recorded is what the equation actually used. It
    must not raise: a bad unit name fails the run through the real path,
    and provenance is not the place to discover it.
    """

    def __init__(
        self,
        normalize: Callable[[str, str, float], float] | None = None,
        permissive: bool = False,
    ):
        self._normalize = normalize or (lambda _mote_id, _key, value: value)
        self._permissive = permissive
        self._reads: dict[tuple[str, str, str], InputRead] = {}

    def note(self, kind: str, mote_id: str, key: str, value: float) -> None:
        """Record one read. The first value wins -- an equation that reads
        the same property twice read the same number both times, and
        listing it twice would only pad the explanation."""
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return
        if kind in _NEEDS_NORMALIZING:
            value = self._normalize(mote_id, key, float(value))
        self._reads.setdefault(
            (kind, mote_id, key), InputRead(kind, mote_id, key, float(value))
        )

    def properties(self, mote_id: str, values: dict) -> dict:
        """Wrap a mote's properties so ordinary reads are recorded."""
        if self._permissive:
            return _PermissiveDict(values, PROPERTY_READ, mote_id, self)
        return _RecordingDict(values, PROPERTY_READ, mote_id, self)

    def previous(self, mote_id: str, values: dict) -> dict:
        """Wrap a mote's entering-the-step properties, read by prev()."""
        if self._permissive:
            return _PermissiveDict(values, PREVIOUS_READ, mote_id, self)
        return _RecordingDict(values, PREVIOUS_READ, mote_id, self)

    def solved(self, mote_id: str, values: dict[Unknown, float]) -> dict[Unknown, float]:
        """Wrap this run's already-solved values. Keys here are whole
        Unknowns rather than bare property names, so this needs its own
        wrapper rather than _RecordingDict's.

        Values already wrapped are returned as they are rather than
        copied: core/simulation.py's probe builds its own RecordingSolved
        in order to read `touched` off it afterwards, and re-wrapping
        would hand the evaluator a copy whose records the probe never
        sees."""
        if isinstance(values, RecordingSolved):
            return values.recording_into(self)
        return RecordingSolved(values, self)

    def model(self, mote_id: str, operation: str, value: float) -> None:
        """Record one property-model evaluation. The operation name takes
        the property key's place: `enthalpy_pt` is what was asked for, and
        there is no property on any mote that holds the answer."""
        self.note(MODEL_READ, mote_id, operation, value)

    def collected(self) -> tuple[InputRead, ...]:
        return tuple(
            sorted(self._reads.values(), key=lambda r: (r.kind, r.mote_id, r.key))
        )


class _RecordingDict(dict):
    """A properties dict that records every value read out of it.

    core/formula.py's `_lookup` tests membership and then indexes, so
    recording `__getitem__` captures exactly the reads that reached a
    formula -- a missing key raises before it can be recorded, which is
    correct, since nothing was read."""

    def __init__(self, base: dict, kind: str, mote_id: str, reads: Reads):
        super().__init__(base)
        self._kind = kind
        self._mote_id = mote_id
        self._reads = reads

    def __getitem__(self, key):
        value = super().__getitem__(key)
        self._reads.note(self._kind, self._mote_id, str(key), value)
        return value


class _PermissiveDict(_RecordingDict):
    """A properties dict that answers for keys it does not have.

    Used only when analysing the rules rather than running them (see
    core/simulation.py's `_touch`). A missing property is exactly what a
    question like "what does this still need?" is asking about -- but
    core/formula.py's `_lookup` raises on it, so the read never happens
    and the gap is invisible to anything watching reads. Claiming to hold
    every key lets the walk finish and records the absence instead.

    This must never be used while solving: there, a missing property is
    a real error, and answering 1.0 would produce a confident wrong
    number rather than a failure.
    """

    def __contains__(self, key) -> bool:
        return True

    def __getitem__(self, key):
        if dict.__contains__(self, key):
            return super().__getitem__(key)
        self._reads.note(MISSING_READ, self._mote_id, str(key), 1.0)
        return 1.0


class RecordingSolved(dict):
    """This run's solved values, recording what an equation asked of them.

    Two different questions are recorded, because core/formula.py's
    `_lookup` asks two: membership ("is this unknown already solved?")
    lands in `touched`, and indexing ("what is its value?") is a genuine
    input read. The first is what core/simulation.py's probe uses to
    discover which unknowns an equation references at all; the second is
    provenance. One class answers both, because they are the same
    interception point and two classes would drift.

    Copied rather than referenced: the substitution loop mutates the real
    dict between passes, and a recording view must reflect the pass it
    was built for.
    """

    def __init__(self, base: dict[Unknown, float], reads: Reads | None = None):
        super().__init__(base)
        self._reads = reads
        self.touched: set[Unknown] = set()

    def recording_into(self, reads: Reads) -> "RecordingSolved":
        """Start reporting value reads to `reads`, keeping whatever has
        already been touched."""
        self._reads = reads
        return self

    def __contains__(self, key) -> bool:
        self.touched.add(key)
        return super().__contains__(key)

    def __getitem__(self, key):
        value = super().__getitem__(key)
        if self._reads is not None:
            mote_id, property_key = key
            self._reads.note(DERIVED_READ, mote_id, property_key, value)
        return value


@dataclass
class Journal:
    """Every derivation one solve produced, in the order it produced them.

    Mutable and passed in rather than returned, so `_reduce` can append as
    it goes without changing what it returns to a caller that doesn't want
    provenance at all. Block numbers are assigned here so they are unique
    within a journal and mean nothing outside it.
    """
    derivations: list[Derivation] = field(default_factory=list)
    _next_block: int = 0

    def record(
        self,
        method: str,
        values: dict[Unknown, float],
        equations: tuple[Origin, ...],
        inputs: tuple[InputRead, ...],
        detail: str | None = None,
    ) -> Derivation:
        derivation = Derivation(
            block=self._next_block,
            method=method,
            values=dict(values),
            equations=equations,
            inputs=inputs,
            detail=detail,
        )
        self._next_block += 1
        self.derivations.append(derivation)
        return derivation

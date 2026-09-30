# core/derivations.py
"""Persisting and reading back what the last solve did, and explaining a
value.

A garden keeps provenance for exactly one solve: the last one that wrote
its answer back (core/simulation.py's run_simulation()). That is the
solve whose values are on the motes, so it is the only one `why` needs
to explain. The write side replaces the whole set at once and takes a
cursor rather than a connection, so the caller commits it together with
the write-back it describes.

The read side answers `why`. Explaining is a traversal, not a
reconstruction: each value names the block that produced it, each block
names its inputs, and an input that the solve derived is itself a value
to explain. The recursion terminates on anything the solve did not
derive -- a stored property, a property-model evaluation, a prev() read
of the garden -- which is exactly the set of things that need no
explanation.

Values here are in each dimension's base SI unit, the units the solver
worked in. See core/provenance.py for why they are not converted back.
"""
import sqlite3
from dataclasses import dataclass

from core.formula import Unknown
from core.provenance import DERIVED_READ, Derivation, Journal

# How deep `explain` follows derived inputs before saying so rather than
# recursing further. Generous -- this is a guard against a pathological
# model, not a display limit.
DEFAULT_DEPTH = 32

_TABLES = (
    "simulation_derivations",
    "simulation_derivation_equations",
    "simulation_derivation_inputs",
)

NO_SOLVE_NOTE = "no solve has been recorded for this garden yet"
UNDETERMINED_NOTE = "not determined by the last solve -- set directly, or unchanged since"


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


def _text(value: float) -> str:
    """Stored as text, like mote_properties.value, so a value round-trips
    exactly rather than through SQLite's REAL affinity."""
    return repr(float(value))


def _number(text: str) -> float:
    return float(text)


def replace_journal(cursor: sqlite3.Cursor, journal: Journal) -> None:
    """Replace the garden's derivations with one solve's. Takes a cursor,
    not a connection, and does not commit -- the caller owns the
    transaction."""
    for table in _TABLES:
        cursor.execute(f"DELETE FROM {table}")
    for derivation in journal.derivations:
        _log_derivation(cursor, derivation)


def _log_derivation(cursor: sqlite3.Cursor, derivation: Derivation) -> None:
    cursor.executemany(
        "INSERT INTO simulation_derivations "
        "(block, mote_id, key, value, method, detail) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
            (derivation.block, mote_id, key,
             _text(value), derivation.method, derivation.detail)
            for (mote_id, key), value in derivation.values.items()
        ],
    )
    cursor.executemany(
        "INSERT INTO simulation_derivation_equations "
        "(block, rule_mote_id, rule_name, anchor_mote_id, equation) "
        "VALUES (?, ?, ?, ?, ?)",
        [
            (derivation.block, o.rule_mote_id, o.rule_name, o.anchor_mote_id, o.equation)
            for o in derivation.equations
        ],
    )
    cursor.executemany(
        "INSERT INTO simulation_derivation_inputs "
        "(block, kind, mote_id, key, value) "
        "VALUES (?, ?, ?, ?, ?)",
        [
            (derivation.block, r.kind, r.mote_id, r.key, _text(r.value))
            for r in derivation.inputs
        ],
    )


def _has_solve(conn: sqlite3.Connection) -> bool:
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM simulation_derivations LIMIT 1")
    return cursor.fetchone() is not None


def explain(
    conn: sqlite3.Connection,
    mote_id: str,
    key: str,
    depth: int = DEFAULT_DEPTH,
) -> Explanation:
    """Why `mote_id.key` holds the value it does, as of the last solve
    that wrote to the garden -- the one whose values are on the motes."""
    if not _has_solve(conn):
        return Explanation(mote_id, key, None, note=NO_SOLVE_NOTE)
    return _explain(conn, mote_id, key, depth, visited=frozenset())


def _explain(
    conn: sqlite3.Connection,
    mote_id: str,
    key: str,
    depth: int,
    visited: frozenset[Unknown],
) -> Explanation:
    row = _derivation_row(conn, mote_id, key)
    if row is None:
        return Explanation(mote_id, key, None, note=UNDETERMINED_NOTE)

    found = (mote_id, key)
    if found in visited:
        return Explanation(
            mote_id, key, _number(row["value"]),
            method=row["method"], note="already explained above",
        )
    if depth <= 0:
        return Explanation(
            mote_id, key, _number(row["value"]),
            method=row["method"], note="beyond the requested depth",
        )

    block = row["block"]
    return Explanation(
        mote_id=mote_id,
        key=key,
        value=_number(row["value"]),
        method=row["method"],
        detail=row["detail"],
        equations=_block_equations(conn, block),
        inputs=_block_inputs(conn, block, depth - 1, visited | {found}),
        also_determined=_block_siblings(conn, block, mote_id, key),
    )


def _derivation_row(
    conn: sqlite3.Connection, mote_id: str, key: str
) -> sqlite3.Row | None:
    """The block that determined this value, if the last solve did."""
    cursor = conn.cursor()
    cursor.execute(
        "SELECT block, value, method, detail FROM simulation_derivations "
        "WHERE mote_id = ? AND key = ? ORDER BY block DESC LIMIT 1",
        (mote_id, key),
    )
    return cursor.fetchone()


def _block_equations(conn: sqlite3.Connection, block: int) -> tuple[EquationRecord, ...]:
    cursor = conn.cursor()
    cursor.execute(
        "SELECT rule_mote_id, rule_name, anchor_mote_id, equation "
        "FROM simulation_derivation_equations "
        "WHERE block = ? ORDER BY rule_name, anchor_mote_id",
        (block,),
    )
    return tuple(
        EquationRecord(
            row["rule_mote_id"], row["rule_name"], row["anchor_mote_id"], row["equation"]
        )
        for row in cursor.fetchall()
    )


def _block_siblings(
    conn: sqlite3.Connection, block: int, mote_id: str, key: str
) -> tuple[Unknown, ...]:
    """The other values this block determined -- empty for a substitution
    step, and the whole point for a solver block."""
    cursor = conn.cursor()
    cursor.execute(
        "SELECT mote_id, key FROM simulation_derivations "
        "WHERE block = ? ORDER BY mote_id, key",
        (block,),
    )
    return tuple(
        (row["mote_id"], row["key"])
        for row in cursor.fetchall()
        if (row["mote_id"], row["key"]) != (mote_id, key)
    )


def _block_inputs(
    conn: sqlite3.Connection,
    block: int,
    depth: int,
    visited: frozenset[Unknown],
) -> tuple[ExplainedInput, ...]:
    cursor = conn.cursor()
    cursor.execute(
        "SELECT kind, mote_id, key, value FROM simulation_derivation_inputs "
        "WHERE block = ? ORDER BY kind, mote_id, key",
        (block,),
    )
    return tuple(
        ExplainedInput(
            row["kind"], row["mote_id"], row["key"], _number(row["value"]),
            _input_derivation(conn, row["kind"], row["mote_id"], row["key"], depth, visited),
        )
        for row in cursor.fetchall()
    )


def _input_derivation(
    conn: sqlite3.Connection,
    kind: str,
    mote_id: str,
    key: str,
    depth: int,
    visited: frozenset[Unknown],
) -> Explanation | None:
    """Only a value the solve itself derived is worth following. A prev()
    read in a static solve is the value the garden held going in, which
    is as much a given as any stored property."""
    if kind == DERIVED_READ:
        return _explain(conn, mote_id, key, depth, visited)
    return None

# core/derivations.py
"""Persisting and reading back what a solve did, and explaining a value.

The write side takes a cursor rather than a connection so a run's
derivations land in the same transaction as its changes (see
core/simulation.py's `_log_run`) -- the two describe one event and must
never be separable.

The read side answers `why`. Explaining is a traversal, not a
reconstruction: each value names the block that produced it, each block
names its inputs, and an input that this run derived is itself a value to
explain. The recursion terminates on anything the run did not derive --
a stored property, a property-model evaluation, the state entering the
first step -- which is exactly the set of things that need no
explanation.

Values here are in each dimension's base SI unit, the units the solver
worked in. See core/provenance.py for why they are not converted back.
"""
import sqlite3
from dataclasses import dataclass

from core.formula import Unknown
from core.provenance import (
    DERIVED_READ,
    PREVIOUS_READ,
    Derivation,
    Journal,
)

# How deep `explain` follows derived inputs before saying so rather than
# recursing further. Generous -- this is a guard against a pathological
# model, not a display limit.
DEFAULT_DEPTH = 32


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
    this run determined it too, which is what makes `why` recursive."""
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
    step: int | None = None
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
    """Same representation core/simulation.py's change log uses, so the
    two halves of a run's record read identically."""
    return repr(float(value))


def _number(text: str) -> float:
    return float(text)


def log_journal(
    cursor: sqlite3.Cursor, run_id: str, step: int, journal: Journal
) -> None:
    """Write one step's derivations. Takes a cursor, not a connection, and
    does not commit -- the caller owns the transaction."""
    for derivation in journal.derivations:
        _log_derivation(cursor, run_id, step, derivation)


def _log_derivation(
    cursor: sqlite3.Cursor, run_id: str, step: int, derivation: Derivation
) -> None:
    cursor.executemany(
        "INSERT INTO simulation_derivations "
        "(run_id, step, block, mote_id, key, value, method, detail) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (run_id, step, derivation.block, mote_id, key,
             _text(value), derivation.method, derivation.detail)
            for (mote_id, key), value in derivation.values.items()
        ],
    )
    cursor.executemany(
        "INSERT INTO simulation_derivation_equations "
        "(run_id, step, block, rule_mote_id, rule_name, anchor_mote_id, equation) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (run_id, step, derivation.block, o.rule_mote_id, o.rule_name,
             o.anchor_mote_id, o.equation)
            for o in derivation.equations
        ],
    )
    cursor.executemany(
        "INSERT INTO simulation_derivation_inputs "
        "(run_id, step, block, kind, mote_id, key, value) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (run_id, step, derivation.block, r.kind, r.mote_id, r.key, _text(r.value))
            for r in derivation.inputs
        ],
    )


def latest_run_id(conn: sqlite3.Connection) -> str | None:
    """The most recent logged run. `why` with no run named explains the
    garden as it currently stands, which is that run."""
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id FROM simulation_runs WHERE status = 'ok' "
        "ORDER BY run_number DESC LIMIT 1"
    )
    row = cursor.fetchone()
    return row["id"] if row is not None else None


def explain(
    conn: sqlite3.Connection,
    mote_id: str,
    key: str,
    run_id: str | None = None,
    step: int | None = None,
    depth: int = DEFAULT_DEPTH,
) -> Explanation:
    """Why `mote_id.key` holds the value it does, as of one run.

    With no run named, the garden's most recent successful run is used --
    the one whose values are currently on the motes. With no step named,
    the last step of that run that determined this value, since a
    time-series run may have determined it repeatedly."""
    if run_id is None:
        run_id = latest_run_id(conn)
    if run_id is None:
        return Explanation(mote_id, key, None, note="this garden has no logged runs")
    return _explain(conn, run_id, mote_id, key, step, depth, visited=frozenset())


def _explain(
    conn: sqlite3.Connection,
    run_id: str,
    mote_id: str,
    key: str,
    step: int | None,
    depth: int,
    visited: frozenset[tuple[str, str, int]],
) -> Explanation:
    row = _derivation_row(conn, run_id, mote_id, key, step)
    if row is None:
        return Explanation(mote_id, key, None, step=step, note=_undetermined_note(step))

    found = (mote_id, key, row["step"])
    if found in visited:
        return Explanation(
            mote_id, key, _number(row["value"]), step=row["step"],
            method=row["method"], note="already explained above",
        )
    if depth <= 0:
        return Explanation(
            mote_id, key, _number(row["value"]), step=row["step"],
            method=row["method"], note="beyond the requested depth",
        )

    block, block_step = row["block"], row["step"]
    return Explanation(
        mote_id=mote_id,
        key=key,
        value=_number(row["value"]),
        step=block_step,
        method=row["method"],
        detail=row["detail"],
        equations=_block_equations(conn, run_id, block_step, block),
        inputs=_block_inputs(
            conn, run_id, block_step, block, depth - 1, visited | {found}
        ),
        also_determined=_block_siblings(conn, run_id, block_step, block, mote_id, key),
    )


def _undetermined_note(step: int | None) -> str:
    if step is not None:
        return f"not determined at step {step} of this run"
    return "not determined by this run -- set directly, or unchanged since"


def _derivation_row(
    conn: sqlite3.Connection, run_id: str, mote_id: str, key: str, step: int | None
) -> sqlite3.Row | None:
    """The block that determined this value. Without a step, the last one
    to do so -- in a time-series run every step re-determines it, and the
    latest is the one whose value the garden ended up with."""
    cursor = conn.cursor()
    query = (
        "SELECT step, block, value, method, detail FROM simulation_derivations "
        "WHERE run_id = ? AND mote_id = ? AND key = ?"
    )
    params: tuple = (run_id, mote_id, key)
    if step is not None:
        query += " AND step = ?"
        params += (step,)
    cursor.execute(query + " ORDER BY step DESC, block DESC LIMIT 1", params)
    return cursor.fetchone()


def _block_equations(
    conn: sqlite3.Connection, run_id: str, step: int, block: int
) -> tuple[EquationRecord, ...]:
    cursor = conn.cursor()
    cursor.execute(
        "SELECT rule_mote_id, rule_name, anchor_mote_id, equation "
        "FROM simulation_derivation_equations "
        "WHERE run_id = ? AND step = ? AND block = ? ORDER BY rule_name, anchor_mote_id",
        (run_id, step, block),
    )
    return tuple(
        EquationRecord(
            row["rule_mote_id"], row["rule_name"], row["anchor_mote_id"], row["equation"]
        )
        for row in cursor.fetchall()
    )


def _block_siblings(
    conn: sqlite3.Connection, run_id: str, step: int, block: int, mote_id: str, key: str
) -> tuple[Unknown, ...]:
    """The other values this block determined -- empty for a substitution
    step, and the whole point for a solver block."""
    cursor = conn.cursor()
    cursor.execute(
        "SELECT mote_id, key FROM simulation_derivations "
        "WHERE run_id = ? AND step = ? AND block = ? ORDER BY mote_id, key",
        (run_id, step, block),
    )
    return tuple(
        (row["mote_id"], row["key"])
        for row in cursor.fetchall()
        if (row["mote_id"], row["key"]) != (mote_id, key)
    )


def _block_inputs(
    conn: sqlite3.Connection,
    run_id: str,
    step: int,
    block: int,
    depth: int,
    visited: frozenset[tuple[str, str, int]],
) -> tuple[ExplainedInput, ...]:
    cursor = conn.cursor()
    cursor.execute(
        "SELECT kind, mote_id, key, value FROM simulation_derivation_inputs "
        "WHERE run_id = ? AND step = ? AND block = ? ORDER BY kind, mote_id, key",
        (run_id, step, block),
    )
    return tuple(
        ExplainedInput(
            row["kind"], row["mote_id"], row["key"], _number(row["value"]),
            _input_derivation(
                conn, run_id, step, row["kind"], row["mote_id"], row["key"], depth, visited
            ),
        )
        for row in cursor.fetchall()
    )


def _input_derivation(
    conn: sqlite3.Connection,
    run_id: str,
    step: int,
    kind: str,
    mote_id: str,
    key: str,
    depth: int,
    visited: frozenset[tuple[str, str, int]],
) -> Explanation | None:
    """Only a value this run derived is worth following.

    A `previous` read is followed into the step before, because that is
    literally where it came from: prev(x) is the value x had entering
    this step, which the last step determined. At step 0 there is no
    earlier step, and the honest answer is that it came from the garden.
    """
    if kind == DERIVED_READ:
        return _explain(conn, run_id, mote_id, key, step, depth, visited)
    if kind == PREVIOUS_READ and step > 0:
        return _explain(conn, run_id, mote_id, key, step - 1, depth, visited)
    return None

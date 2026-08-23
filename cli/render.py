# cli/render.py
"""Rendering shared by both command-line surfaces.

`cli/main.py` (argparse, one command per invocation) and `cli/repl.py`
(interactive) are two front doors onto the same functions, and each has
grown its own copy of the small printers. An explanation tree is too big
to copy: it is recursive, and two copies would drift into two different
accounts of the same run.

These return lines rather than printing them, so the caller decides where
output goes and a test can assert on the text without capturing stdout.
"""
import sqlite3

from core.derivations import Explanation
from core.incidence import DERIVED, MISSING, STORED
from core.motes import get_mote

BASE_UNIT_NOTE = (
    "Values are in each dimension's base SI unit -- what the solver worked in."
)


def label(conn: sqlite3.Connection, mote_id: str) -> str:
    """A mote's name if it has one, else a short id -- the same choice
    core/simulation.py makes for its warnings, for the same reason: an
    explanation naming R1 is worth far more than one naming a UUID."""
    mote = get_mote(conn, mote_id)
    name = mote.properties.get("name") if mote is not None else None
    return str(name) if isinstance(name, str) and name.strip() else mote_id[:8]


def explanation_lines(
    conn: sqlite3.Connection, explanation: Explanation, indent: int = 0
) -> list[str]:
    """One derivation and everything it rests on, depth-first.

    A solver block prints its jointly-determined siblings before its
    equations, because "these four values were found together" is the
    part a reader is most likely to be surprised by, and the part a
    tree-shaped explanation would quietly omit.
    """
    pad = "  " * indent
    value = "?" if explanation.value is None else f"{explanation.value:g}"
    method = f"  [{explanation.method}]" if explanation.method else ""
    lines = [f"{pad}{label(conn, explanation.mote_id)}.{explanation.key} = {value}{method}"]

    if explanation.note:
        lines.append(f"{pad}  {explanation.note}")
    if explanation.detail:
        lines.append(f"{pad}  {explanation.detail}")
    if explanation.also_determined:
        together = ", ".join(
            f"{label(conn, mote_id)}.{key}" for mote_id, key in explanation.also_determined
        )
        lines.append(f"{pad}  found together with: {together}")
    for equation in explanation.equations:
        anchor = label(conn, equation.anchor_mote_id)
        lines.append(f"{pad}  <- {equation.rule_name} on {anchor}: {equation.equation}")
    for item in explanation.inputs:
        lines.append(
            f"{pad}  {item.kind} {label(conn, item.mote_id)}.{item.key} = {item.value:g}"
        )
        if item.derivation is not None:
            lines.extend(explanation_lines(conn, item.derivation, indent + 2))
    return lines


# Missing prerequisites first: they are the only part of a requirement
# listing that anyone has to do something about.
_URGENCY = {MISSING: 0, DERIVED: 1, STORED: 2}


def variable(conn: sqlite3.Connection, target: tuple[str, str]) -> str:
    """`R1.resistance` -- one value's address, named the way a user wrote it."""
    mote_id, key = target
    return f"{label(conn, mote_id)}.{key}"


def _equation_line(conn: sqlite3.Connection, touch, indent: str = "  ") -> str:
    return f"{indent}{touch.rule_name} on {label(conn, touch.anchor_mote_id)}"


def equation_lines(conn: sqlite3.Connection, touches, empty: str) -> list[str]:
    """A list of equations, deduplicated by (rule, instance) and sorted.

    The same rule reaches a value through several instances often enough
    -- three KCL equations on three nodes -- that leaving duplicates in
    would bury the answer."""
    if not touches:
        return [f"  {empty}"]
    seen = {_equation_line(conn, t) for t in touches}
    return sorted(seen)


def impact_lines(conn: sqlite3.Connection, target: tuple[str, str], effects) -> list[str]:
    """What could move if one value changed, grouped by how far the
    change has to travel.

    Distance is hops through the equation graph, not a causal ordering:
    everything in a coupled block moves at once. It is still worth
    showing, because the near values are the ones a reader is most likely
    to have meant.
    """
    if not effects:
        return [
            f"Nothing depends on {variable(conn, target)}.",
            "  No equation reads it, or nothing it reaches is free to move.",
        ]

    count = len(effects)
    lines = [
        f"Changing {variable(conn, target)} could move "
        f"{count} value{'' if count == 1 else 's'}:"
    ]
    for distance in sorted({e.distance for e in effects}):
        step = "1 step" if distance == 1 else f"{distance} steps"
        lines.append(f"  {step} away")
        for effect in (e for e in effects if e.distance == distance):
            through = ", ".join(sorted({
                f"{t.rule_name} on {label(conn, t.anchor_mote_id)}" for t in effect.through
            }))
            lines.append(f"    {variable(conn, effect.variable)}  via {through}")
    return lines


def requirement_lines(conn: sqlite3.Connection, requirement) -> list[str]:
    """What determining a value depends on, and what is stopping it.

    The two failure headlines come first because they are the two most
    common modelling mistakes, and both are silent otherwise: a property
    nobody declared free, and one declared free that no equation
    constrains."""
    name = variable(conn, requirement.variable)
    if not requirement.free:
        return [
            f"Nothing can determine {name}.",
            "  It is not among its mote's declared freedoms, so the solver "
            "may not change it.",
        ]
    if not requirement.producers:
        return [
            f"Nothing can determine {name}.",
            "  It is declared free, but no equation constrains it.",
        ]

    lines = [f"To determine {name}:", "", "  determined by:"]
    lines.extend(f"  {line}" for line in equation_lines(conn, requirement.producers, ""))

    missing = len(requirement.missing)
    heading = "  needs:" if not missing else f"  needs ({missing} missing):"
    lines.extend(["", heading])
    if not requirement.needs:
        lines.append("    nothing -- it is fully constrained on its own")
    # core/incidence.py sorts by mote id, which is UUID order and reads as
    # random. Sort for a human instead: what is missing first, since that
    # is the only part of this list anyone has to act on.
    for need in sorted(
        requirement.needs,
        key=lambda n: (_URGENCY.get(n.status, 9), variable(conn, n.variable)),
    ):
        lines.append(f"    {need.status:<8} {variable(conn, need.variable)}")
    return lines


def scenario_lines(conn: sqlite3.Connection, scenario) -> list[str]:
    """What one scenario changes, grouped by the mote it changes.

    Shows the value each property would take *and* what it currently
    holds, because the interesting thing about a scenario is almost
    always the difference rather than the absolute value."""
    lines = [f"{scenario.name} varies {len(scenario.target_ids)} mote(s):"]
    for override in scenario.overrides:
        current = get_mote(conn, override.target_id)
        existing = current.properties if current is not None else {}
        lines.append(f"  {label(conn, override.target_id)}")
        for key in sorted(override.properties):
            was = existing.get(key, "(unset)")
            lines.append(f"    {key}: {was} -> {override.properties[key]}")
    return lines


def change_lines(changes) -> list[str]:
    """One `old -> new` line per solved property."""
    return [
        f"  [{change.mote_id[:8]}] {change.key}: "
        f"{'None' if change.old_value is None else format(change.old_value, 'g')} -> "
        f"{change.new_value:g}"
        for change in changes
    ]


def run_summary_line(conn: sqlite3.Connection, run) -> str:
    """One logged run, as a single line.

    A run made under a scenario names it, because that is the only thing
    distinguishing two runs that solved the same rules to different
    answers -- and comparing those is what scenarios are for."""
    applied = "applied" if run.committed else "saved"
    line = f"[{run.run_id}] run=#{run.run_number} {run.ran_at} {run.status} ({applied})"
    if run.scenario_id is not None:
        line += f" scenario={label(conn, run.scenario_id)}"
    if run.status == "ok":
        line += f" ({run.change_count} change{'' if run.change_count == 1 else 's'})"
    else:
        line += f": {run.detail}"
    return line

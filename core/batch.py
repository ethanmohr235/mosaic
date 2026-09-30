# core/batch.py
"""One edit applied to many motes, as one undo step.

The table is where large edits happen -- forty motes given an owner in one
gesture -- and this is what that gesture is made of. Three rules hold for
every function here, and they are the whole design:

**One primitive write per mote.** Nothing reaches into `core/db.py`; every
change goes through `core/motes.py` one mote at a time, so a two-hundred
mote edit is two hundred ordinary oplog rows that `core/sync.py` replicates
like any other. There is no bulk path in the store for a surface to have
invented, and there should not be one.

**One undo step.** Each function returns a single `undo.combine`d Command,
because somebody who bulk-edited a selection expects one Ctrl+Z to bring
all of it back, not two hundred presses. The Command is *returned* rather
than pushed: the undo stack belongs to whichever surface is open (see
`core/undo.py`), and core has no business knowing which.

**A missing id is skipped, not fatal.** A selection outlives the rows it
was made on -- another window may have deleted one since -- so writing the
other 199 is the right answer, and raising is not.

This lived in `monolith/api.py`, where it was three bridge methods. It is
in `core/` because a bulk write is not presentation: the console's table,
the CLI and an MCP tool all have to mean the same thing by "set `status`
on these forty", including how the value they were handed is read.
"""
import logging
import sqlite3

from core import command_bar
from core import graph as graph_repo
from core import motes as motes_repo
from core import undo
from core import units

logger = logging.getLogger(__name__)


def existing(conn: sqlite3.Connection, mote_ids) -> list[str]:
    """The ids that are still there, in the order given. See the module
    docstring: a stale selection is ordinary, not an error."""
    ids = [i for i in (mote_ids or []) if isinstance(i, str)]
    return [i for i in ids if motes_repo.get_mote(conn, i) is not None]


def set_property(
    conn: sqlite3.Connection, mote_ids, key: str, raw_value
) -> tuple[int, undo.Command | None]:
    """One key, on exactly the ids given, with the value read the way the
    command bar reads `R1.x = 5 ohm`.

    Which is the point of reusing `command_bar.parse_value`: typing `90`
    into a duration column stores a number rather than a string
    `core/calendar.py` would ignore, and typing `500 ohm` annotates the
    mote's `units` map instead of storing the word. A second, unit-blind
    reading of the same keystrokes is how one surface comes to disagree
    with another about what was typed."""
    if not key:
        raise ValueError("A property needs a key.")
    value, unit_name = command_bar.parse_value(raw_value)
    commands = []
    for mote_id in existing(conn, mote_ids):
        old = motes_repo.get_mote(conn, mote_id).properties
        new = {key: value}
        if unit_name:
            unit_map = units.parse_units_property(old.get(units.UNITS_KEY))
            unit_map[key] = unit_name
            new[units.UNITS_KEY] = unit_map
        before = {k: old[k] for k in new if k in old}
        motes_repo.update_mote(conn, mote_id, new)
        commands.append(undo.for_update_mote(
            conn, mote_id, before, new, label=f"set {key}",
        ))
    return len(commands), _one_step(f"set {key}", commands)


def clear_property(
    conn: sqlite3.Connection, mote_ids, key: str
) -> tuple[int, undo.Command | None]:
    """Deletes the key rather than writing an empty string -- "is empty"
    has to mean the same thing after a clear as it does before the key was
    ever set.

    The mote's `units` entry for the key is deliberately left alone: a
    cleared value is a value not yet filled in, and what it will be
    measured in has not changed."""
    commands = []
    for mote_id in existing(conn, mote_ids):
        old = motes_repo.get_mote(conn, mote_id).properties
        if key not in old:
            continue
        if motes_repo.delete_property(conn, mote_id, key):
            commands.append(undo.for_delete_property(
                conn, mote_id, key, old[key], label=f"clear {key}",
            ))
    return len(commands), _one_step(f"clear {key}", commands)


def delete_motes(conn: sqlite3.Connection, mote_ids) -> tuple[int, undo.Command | None]:
    """The one destructive one, and the reason the whole module returns an
    undo Command rather than asking for confirmation: every other action
    here is reversible by editing the cell back, and a dialog is the wrong
    shape for a selection that might be one row or two hundred. One
    Ctrl+Z brings the batch back instead.

    Relationships are read *before* each delete, never after -- they
    cascade with the mote (`core/db.py`'s ON DELETE CASCADE), so this is
    the last moment they exist to be recorded."""
    commands = []
    for mote_id in existing(conn, mote_ids):
        mote = motes_repo.get_mote(conn, mote_id)
        relationships = [
            {"id": r.id, "fromId": r.from_id, "toId": r.to_id, "type": r.type}
            for r in graph_repo.relationships_touching(conn, mote_id)
        ]
        if motes_repo.delete_mote(conn, mote_id):
            commands.append(undo.for_delete_mote(
                conn, mote_id, mote.properties, relationships, label="delete a mote",
            ))
    return len(commands), _one_step("delete a mote", commands, plural="delete {n} motes")


def _one_step(
    label: str, commands: list[undo.Command], plural: str | None = None
) -> undo.Command | None:
    """The batch as one undo step, or None when it wrote nothing -- a
    caller must not push an empty step, or Ctrl+Z spends a press doing
    nothing at all."""
    if not commands:
        return None
    if len(commands) == 1:
        return commands[0]
    count = len(commands)
    return undo.combine(
        plural.format(n=count) if plural else f"{label} on {count} motes", commands
    )

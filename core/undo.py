# core/undo.py
"""A per-Api, in-memory undo/redo stack shared by every desktop app.

Not built on core/oplog.py: the oplog's delete/update rows only record the
*id* being touched (e.g. delete_mote logs {"id": id}), not the value it had
a moment before, so there is nothing to invert after the fact. Instead a
Command captures its own "before" snapshot at the moment the mutation runs
-- the same idea monolith/api.py's delete_motes() used to hand back as a
bespoke undo token before it was retrofitted onto this module, for
cascade-deleted relationships -- generalized to every mutation and given
a redo half too.

Session-only by design: the stack lives on the Api instance and is gone
when the app closes. It never touches core/motes.py, core/relationships.py,
or the oplog/sync path, so sync replay and the MCP server are unaffected.
"""
import logging
import sqlite3
import time
from dataclasses import dataclass
from typing import Callable

from core import annotations as annotations_repo
from core import motes as motes_repo
from core import relationships as rel_repo

logger = logging.getLogger(__name__)


@dataclass
class Command:
    label: str
    undo: Callable[[], None]
    redo: Callable[[], None]


class UndoManager:
    def __init__(self, limit: int = 200):
        self._undo_stack: list[Command] = []
        self._redo_stack: list[Command] = []
        self._limit = limit
        self._coalesce_key = None
        self._coalesce_until = 0.0

    def push(self, command: Command, coalesce_key=None, coalesce_window: float = 1.5) -> None:
        """Record a command just performed. Clears the redo stack, same as
        any editor: redoing a stale future after a fresh edit would silently
        resurrect a change the user just undid something else to make room
        for.

        coalesce_key, when given, lets a burst of pushes (e.g. keystrokes
        editing the same property) collapse into the one undo step a user
        actually wants: if the top of the stack was pushed with the same
        key within coalesce_window seconds, this call replaces its redo
        action but keeps its original undo action -- so undoing still lands
        on the value from before the burst started, not one keystroke back."""
        now = time.monotonic()
        if (
            coalesce_key is not None
            and self._undo_stack
            and self._coalesce_key == coalesce_key
            and now < self._coalesce_until
        ):
            top = self._undo_stack[-1]
            top.redo = command.redo
            top.label = command.label
        else:
            self._undo_stack.append(command)
            if len(self._undo_stack) > self._limit:
                self._undo_stack.pop(0)

        self._coalesce_key = coalesce_key
        self._coalesce_until = now + coalesce_window if coalesce_key is not None else 0.0
        self._redo_stack.clear()

    def undo(self) -> str | None:
        if not self._undo_stack:
            return None
        command = self._undo_stack.pop()
        command.undo()
        self._redo_stack.append(command)
        self._coalesce_key = None
        return command.label

    def redo(self) -> str | None:
        if not self._redo_stack:
            return None
        command = self._redo_stack.pop()
        command.redo()
        self._undo_stack.append(command)
        self._coalesce_key = None
        return command.label

    def can_undo(self) -> bool:
        return bool(self._undo_stack)

    def can_redo(self) -> bool:
        return bool(self._redo_stack)


# ---- Command factories --------------------------------------------------
#
# One per mutating primitive in core/motes.py and core/relationships.py.
# Each captures whatever "before" state its undo half needs at construction
# time -- the caller must build these *before* the properties/relationships
# they read are gone (see for_delete_mote's docstring).

def for_create_mote(conn, mote_id: str, properties: dict, label: str) -> Command:
    return Command(
        label=label,
        undo=lambda: motes_repo.delete_mote(conn, mote_id),
        redo=lambda: motes_repo.create_mote(conn, properties, id=mote_id),
    )


def for_delete_mote(conn, mote_id: str, properties: dict, relationships: list[dict], label: str) -> Command:
    """relationships: [{"id", "fromId", "toId", "type"}, ...] touching this
    mote, read *before* the delete -- they cascade with it (core/db.py's ON
    DELETE CASCADE), so afterward there is nothing left to read."""
    def _undo():
        motes_repo.create_mote(conn, properties, id=mote_id)
        for r in relationships:
            try:
                rel_repo.create_relationship(conn, r["fromId"], r["toId"], r["type"], id=r["id"])
            except sqlite3.IntegrityError:
                # The mote at the other end was itself deleted after this
                # one -- restoring the rest of the batch is better than
                # refusing the whole undo over one dangling arrow.
                logger.warning("could not restore relationship %r", r.get("id"))

    def _redo():
        motes_repo.delete_mote(conn, mote_id)

    return Command(label=label, undo=_undo, redo=_redo)


def for_update_mote(conn, mote_id: str, old_props: dict, new_props: dict, label: str) -> Command:
    """old_props/new_props map each changed key to its value before/after
    -- a key absent from old_props means it didn't exist yet, so undo
    deletes it rather than setting it back to some placeholder."""
    changed = list(new_props)

    def _undo():
        to_restore = {k: old_props[k] for k in changed if k in old_props}
        if to_restore:
            motes_repo.update_mote(conn, mote_id, to_restore)
        for k in changed:
            if k not in old_props:
                motes_repo.delete_property(conn, mote_id, k)

    def _redo():
        motes_repo.update_mote(conn, mote_id, {k: new_props[k] for k in changed})

    return Command(label=label, undo=_undo, redo=_redo)


def for_delete_property(conn, mote_id: str, key: str, old_value, label: str) -> Command:
    return Command(
        label=label,
        undo=lambda: motes_repo.update_mote(conn, mote_id, {key: old_value}),
        redo=lambda: motes_repo.delete_property(conn, mote_id, key),
    )


def for_create_relationship(conn, rel_id: str, from_id: str, to_id: str, type_: str, label: str) -> Command:
    return Command(
        label=label,
        undo=lambda: rel_repo.delete_relationship(conn, rel_id),
        redo=lambda: rel_repo.create_relationship(conn, from_id, to_id, type_, id=rel_id),
    )


def for_delete_relationship(conn, rel_id: str, from_id: str, to_id: str, type_: str, label: str) -> Command:
    return Command(
        label=label,
        undo=lambda: rel_repo.create_relationship(conn, from_id, to_id, type_, id=rel_id),
        redo=lambda: rel_repo.delete_relationship(conn, rel_id),
    )


def for_update_relationship(conn, rel_id: str, old_fields: dict, new_fields: dict, label: str) -> Command:
    """old_fields/new_fields hold whichever of from_id/to_id/type_ changed."""
    return Command(
        label=label,
        undo=lambda: rel_repo.update_relationship(conn, rel_id, **old_fields),
        redo=lambda: rel_repo.update_relationship(conn, rel_id, **new_fields),
    )


def for_create_annotation(conn, snapshot: dict, label: str) -> Command:
    """snapshot: annotations_repo.to_dict() of the annotation right after
    creation -- its id plus every field the redo half needs to recreate it
    exactly (color/width/height may hold frontend defaults not passed to
    create_sticky_note itself)."""
    return Command(
        label=label,
        undo=lambda: annotations_repo.delete_annotation(conn, snapshot["id"]),
        redo=lambda: annotations_repo._recreate(conn, snapshot),
    )


def for_delete_annotation(conn, snapshot: dict, label: str) -> Command:
    """snapshot: annotations_repo.to_dict() read *before* the delete."""
    return Command(
        label=label,
        undo=lambda: annotations_repo._recreate(conn, snapshot),
        redo=lambda: annotations_repo.delete_annotation(conn, snapshot["id"]),
    )


def for_update_annotation(conn, annotation_id: str, old_fields: dict, new_fields: dict, label: str) -> Command:
    """old_fields/new_fields hold whichever of text/color/width/height/etc
    changed, same shape as for_update_relationship."""
    return Command(
        label=label,
        undo=lambda: annotations_repo.update_annotation(conn, annotation_id, **old_fields),
        redo=lambda: annotations_repo.update_annotation(conn, annotation_id, **new_fields),
    )


def for_move_annotation(conn, annotation_id: str, old_xy: tuple, new_xy: tuple, label: str) -> Command:
    """Kept distinct from for_update_annotation so a drag's undo label
    reads "move a note" rather than "edit a note", and so its push() can
    use its own coalesce_key -- a drag fires many mousemove-driven moves
    that should collapse into the one undo step the gesture represents."""
    return Command(
        label=label,
        undo=lambda: annotations_repo.move_annotation(conn, annotation_id, *old_xy),
        redo=lambda: annotations_repo.move_annotation(conn, annotation_id, *new_xy),
    )


def combine(label: str, commands: list[Command]) -> Command:
    """One Command out of several, so a bulk action (delete 200 selected
    rows, restyle a whole column) is one undo step rather than 200 -- a
    user who bulk-deleted a selection expects one Ctrl+Z to bring all of
    it back, not to spend 200 presses undoing it one row at a time.

    Undoes in reverse order, redoes in forward order, so a batch that
    depends on ordering (e.g. a mote created before a relationship that
    points at it) unwinds and rewinds the same way it was built."""
    def _undo():
        for command in reversed(commands):
            command.undo()

    def _redo():
        for command in commands:
            command.redo()

    return Command(label=label, undo=_undo, redo=_redo)

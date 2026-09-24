# core/annotations.py
"""Commentary layered over a garden's graph -- sticky notes today, other
markup types (highlight halos, callout arrows, freehand ink) later.

Deliberately not built on core/motes.py: an annotation is a reader's aside
about the graph, not graph data, so it must never appear in
list_motes()/neighborhood()/species queries. For the same reason this
module never calls core/oplog.py -- annotations are local-only, and stay
that way until sync support is deliberately added (see docs/architecture.md).
"""
import json
import sqlite3
from dataclasses import asdict, dataclass, field

from core import oplog
from core.db import generate_uuid

_ANCHOR_MODES = {"world", "mote"}

# Columns editable through update_annotation(); text/x/y/color/width/height/z
# and, for a future annotation type, data. anchor_mode and mote_id change
# together and are handled separately since one implies the other.
_UPDATABLE_FIELDS = {"text", "x", "y", "color", "width", "height", "z", "data"}


@dataclass
class Annotation:
    id: str
    type: str
    anchor_mode: str
    mote_id: str | None
    x: float
    y: float
    text: str
    color: str | None
    width: float | None
    height: float | None
    z: int
    data: dict = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""


def to_dict(annotation: Annotation) -> dict:
    """A full snapshot suitable for _recreate() -- e.g. what core/undo.py
    captures before a delete, to hand back to its redo half."""
    return asdict(annotation)


def _row_to_annotation(row: sqlite3.Row) -> Annotation:
    try:
        data = json.loads(row["data"]) if row["data"] else {}
    except json.JSONDecodeError:
        data = {}
    return Annotation(
        id=row["id"],
        type=row["type"],
        anchor_mode=row["anchor_mode"],
        mote_id=row["mote_id"],
        x=row["x"],
        y=row["y"],
        text=row["text"],
        color=row["color"],
        width=row["width"],
        height=row["height"],
        z=row["z"],
        data=data,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def list_annotations(conn: sqlite3.Connection) -> list[Annotation]:
    cur = conn.cursor()
    cur.execute("SELECT * FROM annotations ORDER BY z, created_at")
    return [_row_to_annotation(row) for row in cur.fetchall()]


def get_annotation(conn: sqlite3.Connection, annotation_id: str) -> Annotation | None:
    cur = conn.cursor()
    cur.execute("SELECT * FROM annotations WHERE id=?", (annotation_id,))
    row = cur.fetchone()
    return _row_to_annotation(row) if row is not None else None


def create_sticky_note(
    conn: sqlite3.Connection,
    *,
    anchor_mode: str,
    x: float,
    y: float,
    text: str = "",
    mote_id: str | None = None,
    color: str | None = None,
    width: float | None = None,
    height: float | None = None,
    id: str | None = None,
) -> str:
    if anchor_mode not in _ANCHOR_MODES:
        raise ValueError(f"Invalid anchor_mode: {anchor_mode!r} (must be 'world' or 'mote')")
    if anchor_mode == "mote" and not mote_id:
        raise ValueError("anchor_mode='mote' requires mote_id")

    annotation_id = id or generate_uuid()
    ts = oplog.now_iso()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO annotations
           (id, type, anchor_mode, mote_id, x, y, text, color, width, height, z, data, created_at, updated_at)
           VALUES (?, 'sticky_note', ?, ?, ?, ?, ?, ?, ?, ?, 0, '{}', ?, ?)""",
        (annotation_id, anchor_mode, mote_id, x, y, text, color, width, height, ts, ts),
    )
    conn.commit()
    return annotation_id


def _recreate(conn: sqlite3.Connection, snapshot: dict) -> str:
    """Reinsert an annotation exactly as a prior get_annotation() snapshot
    described it -- the redo half of undoing a delete. Kept separate from
    create_sticky_note() since it restores every field (including type,
    z and data) rather than only what a sticky note's own creation needs."""
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO annotations
           (id, type, anchor_mode, mote_id, x, y, text, color, width, height, z, data, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            snapshot["id"], snapshot["type"], snapshot["anchor_mode"], snapshot["mote_id"],
            snapshot["x"], snapshot["y"], snapshot["text"], snapshot["color"],
            snapshot["width"], snapshot["height"], snapshot["z"], json.dumps(snapshot["data"]),
            snapshot["created_at"], snapshot["updated_at"],
        ),
    )
    conn.commit()
    return snapshot["id"]


def update_annotation(conn: sqlite3.Connection, annotation_id: str, **fields) -> None:
    unknown = set(fields) - _UPDATABLE_FIELDS
    if unknown:
        raise ValueError(f"Unknown annotation field(s): {sorted(unknown)}")
    if not fields:
        raise ValueError("update_annotation requires at least one field to update")
    if "data" in fields:
        fields["data"] = json.dumps(fields["data"])

    ts = oplog.now_iso()
    set_clause = ", ".join(f"{col} = ?" for col in fields) + ", updated_at = ?"
    cur = conn.cursor()
    cur.execute(
        f"UPDATE annotations SET {set_clause} WHERE id=?",
        (*fields.values(), ts, annotation_id),
    )
    conn.commit()


def move_annotation(conn: sqlite3.Connection, annotation_id: str, x: float, y: float) -> None:
    update_annotation(conn, annotation_id, x=x, y=y)


def delete_annotation(conn: sqlite3.Connection, annotation_id: str) -> bool:
    cur = conn.cursor()
    cur.execute("DELETE FROM annotations WHERE id=?", (annotation_id,))
    conn.commit()
    return cur.rowcount > 0

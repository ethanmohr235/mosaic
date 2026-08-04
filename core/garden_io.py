# core/garden_io.py
import dataclasses
import sqlite3

from core.motes import create_mote, get_mote, list_motes
from core.relationships import create_relationship, list_relationships

EXPORT_FORMAT = "monoss-garden-export-v1"


def export_garden(conn: sqlite3.Connection) -> dict:
    return {
        "format": EXPORT_FORMAT,
        "motes": [dataclasses.asdict(m) for m in list_motes(conn)],
        "relationships": [dataclasses.asdict(r) for r in list_relationships(conn)],
    }


def import_garden(conn: sqlite3.Connection, data: dict) -> tuple[int, int]:
    if data.get("format") != EXPORT_FORMAT:
        raise ValueError(f"Unrecognized export format: {data.get('format')!r}")

    motes = data.get("motes", [])
    relationships = data.get("relationships", [])

    conflicts = [m["id"] for m in motes if get_mote(conn, m["id"]) is not None]
    if conflicts:
        raise ValueError(
            f"Cannot import: {len(conflicts)} mote id(s) already exist in this "
            f"garden: {', '.join(conflicts[:5])}{'…' if len(conflicts) > 5 else ''}"
        )

    for m in motes:
        create_mote(conn, m["properties"], id=m["id"])
    for r in relationships:
        create_relationship(conn, r["from_id"], r["to_id"], r["type"], id=r["id"])

    return len(motes), len(relationships)

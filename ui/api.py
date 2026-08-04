# ui/api.py
import dataclasses
import json

from core.db import get_connection, initialize_database
from core import gardens as gardens_repo
from core import graph as graph_repo
from core import motes as motes_repo
from core import relationships as rel_repo


def _coerce(raw: str):
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def _coerce_properties(raw_properties: dict[str, str]) -> dict:
    return {k: _coerce(v) for k, v in raw_properties.items()}


class Api:
    """JS-facing bridge over the core data layer. Holds the live garden
    connection as instance state -- not module-global, same pattern as
    cli/repl.py's locals."""

    def __init__(self, garden: str | None = None):
        self._current_garden = gardens_repo.resolve_garden(garden)
        self._conn = get_connection(self._current_garden)
        initialize_database(self._conn)

    def shutdown(self) -> None:
        self._conn.close()

    # ---- garden management ----

    def current_garden(self) -> str:
        return self._current_garden

    def list_gardens(self) -> list[str]:
        return gardens_repo.list_gardens()

    def create_garden(self, name: str) -> None:
        gardens_repo.create_garden(name)

    def switch_garden(self, name: str) -> dict:
        resolved = gardens_repo.resolve_garden(name)
        self._conn.close()
        self._conn = get_connection(resolved)
        initialize_database(self._conn)
        self._current_garden = resolved
        return {"garden": resolved}

    # ---- motes ----

    def get_mote(self, mote_id: str) -> dict | None:
        mote = motes_repo.get_mote(self._conn, mote_id)
        return dataclasses.asdict(mote) if mote is not None else None

    def list_motes(self) -> list[dict]:
        return [dataclasses.asdict(m) for m in motes_repo.list_motes(self._conn)]

    def create_mote(self, raw_properties: dict[str, str]) -> dict:
        mote_id = motes_repo.create_mote(self._conn, _coerce_properties(raw_properties))
        return dataclasses.asdict(motes_repo.get_mote(self._conn, mote_id))

    def update_mote(self, mote_id: str, raw_properties: dict[str, str]) -> dict:
        motes_repo.update_mote(self._conn, mote_id, _coerce_properties(raw_properties))
        return dataclasses.asdict(motes_repo.get_mote(self._conn, mote_id))

    def delete_property(self, mote_id: str, key: str) -> bool:
        return motes_repo.delete_property(self._conn, mote_id, key)

    def delete_mote(self, mote_id: str) -> bool:
        return motes_repo.delete_mote(self._conn, mote_id)

    # ---- relationships ----

    def mote_relationships(self, mote_id: str) -> list[dict]:
        rels = graph_repo.relationships_touching(self._conn, mote_id)
        return [dataclasses.asdict(r) for r in rels]

    def create_relationship(self, from_id: str, to_id: str, type_: str) -> dict:
        rel_id = rel_repo.create_relationship(self._conn, from_id, to_id, type_)
        return dataclasses.asdict(rel_repo.get_relationship(self._conn, rel_id))

    def delete_relationship(self, rel_id: str) -> bool:
        return rel_repo.delete_relationship(self._conn, rel_id)

    # ---- graph/focus ----

    def focus(self, mote_id: str | None = None, hops: int = 2) -> dict:
        """Single entrypoint for startup, recenter, and post-create/delete
        refresh. Resolves: given id (if it still exists) -> most-recent ->
        empty state. Returns the resolved focus_id so the caller can
        self-heal stored state."""
        if mote_id is not None and motes_repo.get_mote(self._conn, mote_id) is None:
            mote_id = None
        if mote_id is None:
            mote_id = graph_repo.most_recent_mote_id(self._conn)
        if mote_id is None:
            return {"focus_id": None, "motes": [], "relationships": []}

        motes, rels = graph_repo.neighborhood(self._conn, mote_id, hops)
        return {
            "focus_id": mote_id,
            "motes": [dataclasses.asdict(m) for m in motes],
            "relationships": [dataclasses.asdict(r) for r in rels],
        }

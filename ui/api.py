# ui/api.py
import dataclasses
import functools
import json
import threading
from pathlib import Path

import webview

from core.db import get_connection, initialize_database
from core import gardens as gardens_repo
from core import garden_io
from core import graph as graph_repo
from core import motes as motes_repo
from core import relationships as rel_repo
from core import species as species_repo


def _coerce(raw: str):
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def _coerce_properties(raw_properties: dict[str, str]) -> dict:
    return {k: _coerce(v) for k, v in raw_properties.items()}


def _synchronized(method):
    """pywebview invokes exposed methods from a worker thread, not
    necessarily the same one each time. Api holds one long-lived sqlite3
    connection (opened with check_same_thread=False), so calls onto it
    must be serialized here rather than left to sqlite3 to police."""
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapper


class Api:
    """JS-facing bridge over the core data layer. Holds the live garden
    connection as instance state -- not module-global, same pattern as
    cli/repl.py's locals."""

    def __init__(self, garden: str | None = None):
        self._lock = threading.Lock()
        self._current_garden = gardens_repo.resolve_garden(garden)
        self._conn = get_connection(self._current_garden)
        initialize_database(self._conn)
        # Set by ui/app.py right after the window is created (the window
        # needs this Api instance to exist first, as its js_api) -- used
        # for native file dialogs in export/import.
        self._window: webview.Window | None = None

    @_synchronized
    def shutdown(self) -> None:
        self._conn.close()

    # ---- garden management ----

    @_synchronized
    def current_garden(self) -> str:
        return self._current_garden

    @_synchronized
    def list_gardens(self) -> list[str]:
        return gardens_repo.list_gardens()

    @_synchronized
    def create_garden(self, name: str) -> None:
        gardens_repo.create_garden(name)

    @_synchronized
    def switch_garden(self, name: str) -> dict:
        resolved = gardens_repo.resolve_garden(name)
        self._conn.close()
        self._conn = get_connection(resolved)
        initialize_database(self._conn)
        self._current_garden = resolved
        return {"garden": resolved}

    @_synchronized
    def export_garden(self) -> dict:
        path = self._window.create_file_dialog(
            webview.FileDialog.SAVE, save_filename=f"{self._current_garden}.json"
        )
        if not path:
            return {"cancelled": True}
        target = Path(path if isinstance(path, str) else path[0])
        target.write_text(json.dumps(garden_io.export_garden(self._conn), indent=2))
        return {"path": str(target)}

    @_synchronized
    def import_garden_into_new(self, new_garden_name: str) -> dict:
        paths = self._window.create_file_dialog(webview.FileDialog.OPEN)
        if not paths:
            return {"cancelled": True}
        data = json.loads(Path(paths[0]).read_text())

        gardens_repo.create_garden(new_garden_name)
        conn = get_connection(new_garden_name)
        try:
            initialize_database(conn)
            motes, rels = garden_io.import_garden(conn, data)
        finally:
            conn.close()
        return {"garden": new_garden_name, "motes": motes, "relationships": rels}

    # ---- motes ----

    @_synchronized
    def get_mote(self, mote_id: str) -> dict | None:
        mote = motes_repo.get_mote(self._conn, mote_id)
        return dataclasses.asdict(mote) if mote is not None else None

    @_synchronized
    def list_motes(self) -> list[dict]:
        return [dataclasses.asdict(m) for m in motes_repo.list_motes(self._conn)]

    @_synchronized
    def create_mote(self, raw_properties: dict[str, str]) -> dict:
        mote_id = motes_repo.create_mote(self._conn, _coerce_properties(raw_properties))
        return dataclasses.asdict(motes_repo.get_mote(self._conn, mote_id))

    @_synchronized
    def update_mote(self, mote_id: str, raw_properties: dict[str, str]) -> dict:
        motes_repo.update_mote(self._conn, mote_id, _coerce_properties(raw_properties))
        return dataclasses.asdict(motes_repo.get_mote(self._conn, mote_id))

    @_synchronized
    def delete_property(self, mote_id: str, key: str) -> bool:
        return motes_repo.delete_property(self._conn, mote_id, key)

    @_synchronized
    def delete_mote(self, mote_id: str) -> bool:
        return motes_repo.delete_mote(self._conn, mote_id)

    # ---- relationships ----

    @_synchronized
    def mote_relationships(self, mote_id: str) -> list[dict]:
        rels = graph_repo.relationships_touching(self._conn, mote_id)
        return [dataclasses.asdict(r) for r in rels]

    @_synchronized
    def create_relationship(self, from_id: str, to_id: str, type_: str) -> dict:
        rel_id = rel_repo.create_relationship(self._conn, from_id, to_id, type_)
        return dataclasses.asdict(rel_repo.get_relationship(self._conn, rel_id))

    @_synchronized
    def update_relationship(
        self, rel_id: str, from_id: str | None = None, to_id: str | None = None, type_: str | None = None
    ) -> dict:
        rel_repo.update_relationship(self._conn, rel_id, from_id, to_id, type_)
        return dataclasses.asdict(rel_repo.get_relationship(self._conn, rel_id))

    @_synchronized
    def delete_relationship(self, rel_id: str) -> bool:
        return rel_repo.delete_relationship(self._conn, rel_id)

    # ---- species ----

    @_synchronized
    def set_species(self, mote_id: str, species_id: str) -> dict:
        rel_id = species_repo.set_species(self._conn, mote_id, species_id)
        return dataclasses.asdict(rel_repo.get_relationship(self._conn, rel_id))

    @_synchronized
    def species_of(self, mote_id: str) -> list[dict]:
        return [dataclasses.asdict(m) for m in species_repo.species_of(self._conn, mote_id)]

    @_synchronized
    def instances_of(self, species_id: str) -> list[dict]:
        return [dataclasses.asdict(m) for m in species_repo.instances_of(self._conn, species_id)]

    @_synchronized
    def list_species(self) -> list[dict]:
        return [dataclasses.asdict(m) for m in species_repo.list_species(self._conn)]

    # ---- graph/focus ----

    @_synchronized
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
            return {"focus_id": None, "motes": [], "relationships": [], "species_ids": []}

        motes, rels = graph_repo.neighborhood(self._conn, mote_id, hops)
        species_ids = [
            m.id for m in motes
            if any(r.type == species_repo.INSTANCE_OF and r.to_id == m.id for r in rels)
        ]
        return {
            "focus_id": mote_id,
            "motes": [dataclasses.asdict(m) for m in motes],
            "relationships": [dataclasses.asdict(r) for r in rels],
            "species_ids": species_ids,
        }

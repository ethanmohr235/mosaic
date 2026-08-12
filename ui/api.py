# ui/api.py
import dataclasses
import functools
import json
import os
import platform
import subprocess
import threading
import webbrowser
from pathlib import Path

import webview

from core.db import get_connection, initialize_database
from core import gardens as gardens_repo
from core import garden_io
from core import google_calendar as gcal_repo
from core import graph as graph_repo
from core import links as links_repo
from core import motes as motes_repo
from core import relationships as rel_repo
from core import simulation as simulation_repo
from core import species as species_repo


def _coerce(raw: str):
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def _coerce_properties(raw_properties: dict[str, str]) -> dict:
    return {k: _coerce(v) for k, v in raw_properties.items()}


def _popen_detached(args: list[str]) -> None:
    """Launches args without waiting on it (see _open_with_os_default for
    why), but still reaps the child on a background thread so it doesn't
    sit as a zombie for as long as MonOSS keeps running -- this app is
    long-lived and every Open click spawns one of these."""
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    threading.Thread(target=proc.wait, daemon=True).start()


def _open_with_os_default(path: str) -> None:
    """Launches a local file with whatever the OS has registered as the
    default app for its type -- e.g. a .py file opens in VSCode, not as
    text in a browser tab, which is what webbrowser.open() would do.

    Fire-and-forget: open_link() is @_synchronized, so blocking here
    until the opener command exits would hold that lock -- and freeze
    every other Api call -- for as long as the opener (or the app it
    launches) takes to return, which observably is not always fast."""
    system = platform.system()
    if system == "Darwin":
        _popen_detached(["open", path])
    elif system == "Windows":
        os.startfile(path)
    else:
        # gio (GLib/GVFS) detects content-type the same way GTK file
        # managers do (extension-aware glob matching), which is what a
        # user expects "Open" to match. xdg-open shells out to a cruder
        # MIME-sniffing heuristic that can disagree -- observed here as a
        # .py file coming back as the legacy "text/x-script.python"
        # alias, which has no registered default, instead of
        # "text/x-python", which does, silently falling back to a web
        # browser instead of erroring. Only falls back to xdg-open if
        # gio itself isn't installed.
        try:
            _popen_detached(["gio", "open", path])
        except FileNotFoundError:
            _popen_detached(["xdg-open", path])


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
    def delete_garden(self, name: str) -> dict:
        # Checked in this order deliberately: if it's the only garden left,
        # "switch away first" would be misleading advice -- there's nowhere
        # to switch to.
        if len(gardens_repo.list_gardens()) <= 1:
            raise ValueError("Can't delete the last garden.")
        if name == self._current_garden:
            raise ValueError("Can't delete the garden you're currently in -- switch to another one first.")
        gardens_repo.delete_garden(name)
        return {"deleted": name}

    @_synchronized
    def restore_garden(self, name: str) -> dict:
        gardens_repo.restore_garden(name)
        return {"restored": name}

    @_synchronized
    def export_garden(self, garden_name: str | None = None) -> dict:
        """Exports the current garden by default, or any named garden --
        via a short-lived connection, so exporting one from the gardens
        list doesn't disturb the currently open one."""
        target_name = garden_name or self._current_garden
        path = self._window.create_file_dialog(
            webview.FileDialog.SAVE, save_filename=f"{target_name}.json"
        )
        if not path:
            return {"cancelled": True}
        target = Path(path if isinstance(path, str) else path[0])

        if target_name == self._current_garden:
            data = garden_io.export_garden(self._conn)
        else:
            conn = get_connection(target_name)
            try:
                data = garden_io.export_garden(conn)
            finally:
                conn.close()
        target.write_text(json.dumps(data, indent=2))
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
    def create_mote(self, raw_properties: dict[str, str], id: str | None = None) -> dict:
        mote_id = motes_repo.create_mote(self._conn, _coerce_properties(raw_properties), id=id)
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

    # ---- links ----

    @_synchronized
    def open_link(self, target: str) -> dict:
        kind, value = links_repo.classify_link(target)
        if kind == "url":
            webbrowser.open(value)
        else:
            _open_with_os_default(value)
        return {"opened": target}

    # ---- relationships ----

    @_synchronized
    def mote_relationships(self, mote_id: str) -> list[dict]:
        rels = graph_repo.relationships_touching(self._conn, mote_id)
        return [dataclasses.asdict(r) for r in rels]

    @_synchronized
    def create_relationship(self, from_id: str, to_id: str, type_: str, id: str | None = None) -> dict:
        rel_id = rel_repo.create_relationship(self._conn, from_id, to_id, type_, id=id)
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

    @_synchronized
    def set_template(self, mote_id: str, species_id: str) -> dict:
        rel_id = species_repo.set_template(self._conn, mote_id, species_id)
        return dataclasses.asdict(rel_repo.get_relationship(self._conn, rel_id))

    @_synchronized
    def template_for(self, species_id: str) -> dict | None:
        template = species_repo.template_for(self._conn, species_id)
        return dataclasses.asdict(template) if template is not None else None

    @_synchronized
    def create_instance(self, species_id: str, raw_overrides: dict | None = None) -> dict:
        mote_id = species_repo.create_instance(
            self._conn, species_id, _coerce_properties(raw_overrides or {})
        )
        return dataclasses.asdict(motes_repo.get_mote(self._conn, mote_id))

    # ---- simulation ----

    @_synchronized
    def run_simulation(self) -> dict:
        """A garden whose rules don't solve is an ordinary outcome to
        report, not a crash -- so SimulationError comes back as ok:false
        rather than propagating across the JS bridge as an exception."""
        try:
            result = simulation_repo.run_simulation(self._conn)
        except simulation_repo.SimulationError as e:
            return {"ok": False, "message": str(e)}
        return {
            "ok": True,
            "tick": result.tick,
            "written": len(result.changes),
            "message": result.summary,
        }

    # ---- google calendar ----

    def export_to_google_calendar(self) -> dict:
        """Deliberately NOT @_synchronized: this does OAuth (may block on
        the user completing a consent screen in their browser) and
        network I/O, and holding the shared connection lock for that long
        would freeze every other Api call for as long as it takes -- the
        same mistake already made and fixed once for local-file opening
        (see _open_with_os_default). Only the brief DB-touching calls
        (list_motes, update_mote) are synchronized, individually."""
        garden_at_start = self._current_garden
        motes = gcal_repo.dated_motes(self.list_motes())
        credentials = gcal_repo.get_credentials()

        created = updated = skipped = 0
        for mote in motes:
            try:
                payload = gcal_repo.event_payload(mote)
            except ValueError:
                skipped += 1
                continue

            existing_id = mote["properties"].get("gcal_event_id")
            event_id = gcal_repo.push_event(credentials, payload, existing_id)

            if self._current_garden != garden_at_start:
                # The garden changed underneath this loop (e.g. the user
                # switched gardens mid-export) -- don't write this id
                # into whatever garden is open now.
                continue
            if existing_id:
                updated += 1
            else:
                created += 1
            if event_id != existing_id:
                self.update_mote(mote["id"], {"gcal_event_id": event_id})

        return {"created": created, "updated": updated, "skipped": skipped, "total": len(motes)}

    # ---- graph/focus ----

    @_synchronized
    def focus(self, mote_id: str | None = None, hops: int | None = 2) -> dict:
        """Single entrypoint for startup, recenter, and post-create/delete
        refresh. Resolves: given id (if it still exists) -> most-recent ->
        empty state. Returns the resolved focus_id so the caller can
        self-heal stored state.

        `hops=None` means the whole garden rather than a neighborhood --
        the only way to see motes that have no relationships yet."""
        if mote_id is not None and motes_repo.get_mote(self._conn, mote_id) is None:
            mote_id = None
        if mote_id is None:
            mote_id = graph_repo.most_recent_mote_id(self._conn)
        if mote_id is None:
            return {"focus_id": None, "motes": [], "relationships": [], "species_ids": []}

        if hops is None:
            motes, rels = graph_repo.whole_garden(self._conn)
        else:
            motes, rels = graph_repo.neighborhood(self._conn, mote_id, hops)
        # template_of counts as well as instance_of, matching
        # species.list_species -- otherwise a species you've set up with a
        # template but not yet instantiated wouldn't render as one, and the
        # UI would offer no way to create its first instance.
        structural = (species_repo.INSTANCE_OF, species_repo.TEMPLATE_OF)
        species_ids = [
            m.id for m in motes
            if any(r.type in structural and r.to_id == m.id for r in rels)
        ]
        return {
            "focus_id": mote_id,
            "motes": [dataclasses.asdict(m) for m in motes],
            "relationships": [dataclasses.asdict(r) for r in rels],
            "species_ids": species_ids,
        }

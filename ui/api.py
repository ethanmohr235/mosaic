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
from core import folders as folders_repo
from core import graph as graph_repo
from core import library as library_repo
from core import links as links_repo
from core import motes as motes_repo
from core import portals as portals_repo
from core import relationships as rel_repo
from core import reserved as reserved_repo
from core import roles as roles_repo
from core import simulation as simulation_repo
from core import species as species_repo


def _coerce(raw):
    # Text-field input arrives as a string and gets interpreted as JSON
    # where possible (so typing "21" produces an int). Copy/paste
    # (app.js::copySelection/pasteAt) instead sends a mote's already-typed
    # properties straight back through create_mote -- those aren't raw
    # text to interpret, they're already the right type (an int, a bool,
    # a list, ...), and json.loads() raises TypeError on anything that
    # isn't str/bytes/bytearray. Only strings need coercing; anything
    # else is passed through as-is.
    if not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def _coerce_properties(raw_properties: dict) -> dict:
    return {k: _coerce(v) for k, v in raw_properties.items()}


def _classify(motes: list, rels: list) -> dict:
    """What each returned mote *is* -- shared by focus() (the currently-
    open garden) and library_snapshot() (a read of _library) so the two
    don't drift.

    One `roles` map rather than the species_ids/folder_ids arrays this
    used to return: those were two of six roles core recognizes
    (core/roles.py), and the frontend had no way to hear about the other
    four, which is why a portal mote's detail panel said "Mote" while
    the canvas drew it with a portal ring. Adding a role is now a change
    to the table, not to this payload's shape."""
    return {"roles": roles_repo.roles_in(motes, rels)}


def _time_series_payload(result: "simulation_repo.TimeSeriesResult") -> dict:
    """Shapes a simulation_repo.TimeSeriesResult for the JS bridge. The
    steps themselves go through core's own step_payloads(), shared with
    the MCP adapter; only the run-level fields around them are this
    bridge's business."""
    return {
        "steps": simulation_repo.step_payloads(result.steps),
        "failed_at": result.failed_at,
        "error": result.error,
        "run_id": result.run_id,
    }


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
    cli/repl.py's locals.

    The bridge mirrors the repository surface, the same way
    mcp_server/server.py does, rather than being trimmed to exactly
    today's frontend callers: a method with no caller yet (list_species,
    set_template) is a bridge that's complete, not one that's rotting.

    What earns a test in tests/test_api.py is behavior this layer adds --
    property coercion, garden switching, cross-garden connections, export
    orchestration. A method that only forwards to a repository and
    asdict()s the result is already covered by that repository's own
    tests, and testing it again here would only assert that Python can
    call a function."""

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
        # Reserved gardens (the reusable-motes library) are filtered by
        # core, not here, so the switcher, the portal picker, `garden
        # list` and the MCP tool all show the same set.
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
        # to switch to. "Last garden" counts workspaces, not garden files:
        # being left with only the reusable-motes library is being left
        # with nowhere to work.
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
    def get_default_garden(self) -> str | None:
        return gardens_repo.get_default_garden()

    @_synchronized
    def set_default_garden(self, name: str) -> None:
        gardens_repo.set_default_garden(name)

    @_synchronized
    def clear_default_garden(self) -> None:
        gardens_repo.clear_default_garden()

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

    # ---- portals ----

    @_synchronized
    def find_portal_target(self, garden: str, query: str) -> list[dict]:
        """Search another garden's motes for a portal's target, via a
        short-lived connection -- same pattern export_garden(garden_name)
        uses so browsing candidates doesn't disturb the currently open
        garden. Raises if `garden` doesn't exist rather than silently
        creating it (get_connection()/initialize_database() would
        otherwise happily create an empty garden file from a bad name)."""
        if not gardens_repo.garden_exists(garden):
            raise LookupError(f"No such garden: {garden!r}")
        conn = get_connection(garden)
        try:
            initialize_database(conn)
            return [dataclasses.asdict(m) for m in motes_repo.search_motes(conn, query)]
        finally:
            conn.close()

    @_synchronized
    def create_portal(
        self, target_garden: str, target_mote_id: str, raw_properties: dict | None = None
    ) -> dict:
        mote_id = portals_repo.create_portal(
            self._conn, target_garden, target_mote_id, _coerce_properties(raw_properties or {})
        )
        return dataclasses.asdict(motes_repo.get_mote(self._conn, mote_id))

    @_synchronized
    def jump_to_portal(self, mote_id: str) -> dict:
        """Resolve mote_id's portal target and switch the live connection
        to it in one call -- bundles resolve+switch (the same close/
        reopen switch_garden() does) so the frontend doesn't orchestrate
        a multi-round-trip sequence. Errors (deleted target garden/mote)
        propagate up through call()'s existing toast path."""
        mote = motes_repo.get_mote(self._conn, mote_id)
        if mote is None:
            raise LookupError(f"No such mote: {mote_id!r}")
        garden, target_mote = portals_repo.resolve_portal(mote.properties)
        self._conn.close()
        self._conn = get_connection(garden)
        initialize_database(self._conn)
        self._current_garden = garden
        return {"garden": garden, "mote_id": target_mote.id}

    # ---- library ----

    @_synchronized
    def open_library(self) -> dict:
        """Switch into _library as a real, viewable/editable garden --
        lazily creating it if nothing's been saved to it yet, same
        reasoning as core/library.py's own lazy-creation-on-first-save
        (get_connection + initialize_database is enough; SQLite creates
        the file, so this deliberately bypasses self.create_garden(),
        which rejects the literal name _library for *user-initiated*
        creation via "+ New garden…"). Bundles create-if-needed with the
        same close/reopen dance switch_garden()/jump_to_portal() already
        do, so the frontend's action is one call."""
        self._conn.close()
        self._conn = get_connection(library_repo.LIBRARY_GARDEN)
        initialize_database(self._conn)
        self._current_garden = library_repo.LIBRARY_GARDEN
        return {"garden": self._current_garden}

    @_synchronized
    def save_to_library(self, mote_id: str) -> dict:
        """Snapshot mote_id's properties into a new library entry.
        Returns the new entry, fetched back via a short-lived connection
        to the library garden -- same pattern library_snapshot() uses to
        read it without disturbing self._conn."""
        entry_id = library_repo.save_to_library(self._conn, mote_id)
        conn = get_connection(library_repo.LIBRARY_GARDEN)
        try:
            initialize_database(conn)
            return dataclasses.asdict(motes_repo.get_mote(conn, entry_id))
        finally:
            conn.close()

    @_synchronized
    def library_snapshot(self) -> dict:
        """Whole-graph read of _library (motes + relationships +
        species/folder classification) for the "Reuse from library…"
        picker's client-side filtering -- fetched once per modal-open
        instead of a per-keystroke round trip. Cross-garden idiom
        matches find_portal_target()/save_to_library()."""
        conn = get_connection(library_repo.LIBRARY_GARDEN)
        try:
            initialize_database(conn)
            motes, rels = graph_repo.whole_garden(conn)
            return {
                "motes": [dataclasses.asdict(m) for m in motes],
                "relationships": [dataclasses.asdict(r) for r in rels],
                **_classify(motes, rels),
            }
        finally:
            conn.close()

    @_synchronized
    def reuse_from_library(self, library_mote_id: str, raw_overrides: dict | None = None) -> dict:
        mote_id = library_repo.reuse_from_library(
            self._conn, library_mote_id, _coerce_properties(raw_overrides or {})
        )
        return dataclasses.asdict(motes_repo.get_mote(self._conn, mote_id))

    @_synchronized
    def delete_library_entry(self, library_mote_id: str) -> bool:
        conn = get_connection(library_repo.LIBRARY_GARDEN)
        try:
            initialize_database(conn)
            return motes_repo.delete_mote(conn, library_mote_id)
        finally:
            conn.close()

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

    # ---- folders ----

    @_synchronized
    def set_folder(self, mote_id: str, folder_id: str) -> dict:
        rel_id = folders_repo.set_folder(self._conn, mote_id, folder_id)
        return dataclasses.asdict(rel_repo.get_relationship(self._conn, rel_id))

    @_synchronized
    def folders_of(self, mote_id: str) -> list[dict]:
        return [dataclasses.asdict(m) for m in folders_repo.folders_of(self._conn, mote_id)]

    @_synchronized
    def folder_members(self, folder_id: str) -> list[dict]:
        return [dataclasses.asdict(m) for m in folders_repo.folder_members(self._conn, folder_id)]

    # ---- simulation ----

    @_synchronized
    def preview_simulation(self, step_count: int) -> dict:
        """Runs step_count steps entirely in memory -- nothing is written
        to this garden. The frontend scrubs through the returned steps
        client-side; committing a chosen step is a separate call."""
        result = simulation_repo.run_time_series(self._conn, step_count, commit=False)
        return _time_series_payload(result)

    @_synchronized
    def commit_simulation(self, step_count: int) -> dict:
        """Re-runs step_count steps (deterministic given the same garden
        and rules) and this time writes the final cumulative result,
        logging one entry the same way a single run_simulation() always
        has. Never trusts step values echoed back from a prior preview
        call -- the only numbers that ever reach the database are ones
        this recomputes itself."""
        result = simulation_repo.run_time_series(self._conn, step_count, commit=True)
        return _time_series_payload(result)

    @_synchronized
    def save_simulation(self, step_count: int) -> dict:
        """Like commit_simulation, but never writes the graph -- logs
        every computed tick for later review (e.g. comparing a few
        hypothetical parameter sets) without any of them becoming the
        garden's canonical state. Always re-runs the full step_count
        (unlike commit, which only re-runs up to whichever tick was
        scrubbed to), since there's no "committed position" for a save."""
        result = simulation_repo.run_time_series(self._conn, step_count, commit=False, save=True)
        return _time_series_payload(result)

    @_synchronized
    def list_runs(self, limit: int | None = None) -> list[dict]:
        """Every logged run (run_simulation or a committed run_time_series),
        most recent first -- backs the runs panel's list."""
        return [dataclasses.asdict(r) for r in simulation_repo.list_runs(self._conn, limit=limit)]

    @_synchronized
    def get_run(self, run_id: str) -> dict | None:
        run = simulation_repo.get_run(self._conn, run_id)
        return dataclasses.asdict(run) if run is not None else None

    @_synchronized
    def run_changes(self, run_id: str) -> list[dict]:
        """A logged run's net old->new changes -- backs the runs panel's
        list view. For the tick-by-tick breakdown, see run_ticks."""
        return [dataclasses.asdict(c) for c in simulation_repo.run_changes(self._conn, run_id)]

    @_synchronized
    def run_ticks(self, run_id: str) -> dict:
        """A run's own internal ticks, shaped like _time_series_payload's
        steps so the frontend's existing scrub-bar rendering needs
        nothing new to "enter" a run -- unrelated to any other run in
        the garden's history."""
        return {"steps": simulation_repo.step_payloads(simulation_repo.run_ticks(self._conn, run_id))}

    @_synchronized
    def mote_history(self, mote_id: str) -> dict[str, list[float]]:
        """Every value a mote's properties have taken across the
        garden's committed run history -- backs the inspector's
        sparklines."""
        return simulation_repo.mote_history(self._conn, mote_id)

    # ---- google calendar ----

    def export_to_google_calendar(self) -> dict:
        """Deliberately NOT @_synchronized: this does OAuth (may block on
        the user completing a consent screen in their browser) and
        network I/O, and holding the shared connection lock for that long
        would freeze every other Api call for as long as it takes -- the
        same mistake already made and fixed once for local-file opening
        (see _open_with_os_default). Only the brief DB-touching calls
        (list_motes, update_mote) are synchronized, individually.

        The export itself is core's (gcal_repo.export_events); what this
        supplies is the two things only the app knows -- how to write an
        event id back through the lock, and whether the user has switched
        gardens out from under a network call in flight."""
        garden_at_start = self._current_garden
        all_motes = self.list_motes()
        # Lock held only for this brief read, same discipline as every
        # other DB touch in this deliberately-not-@_synchronized method.
        with self._lock:
            template_ids = species_repo.template_mote_ids(self._conn)

        summary = gcal_repo.export_events(
            gcal_repo.exportable_motes(all_motes, template_ids),
            gcal_repo.get_credentials(),
            record_event_id=lambda mote_id, event_id: self.update_mote(
                mote_id, {"gcal_event_id": event_id}
            ),
            still_exporting=lambda: self._current_garden == garden_at_start,
        )
        return dataclasses.asdict(summary)

    # ---- reserved names ----

    @_synchronized
    def reserved_names(self) -> dict:
        return {
            "properties": sorted(reserved_repo.RESERVED_PROPERTIES),
            "relationshipTypes": sorted(reserved_repo.RESERVED_RELATIONSHIP_TYPES),
        }

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
            return {"focus_id": None, "motes": [], "relationships": [], "roles": {}}

        if hops is None:
            motes, rels = graph_repo.whole_garden(self._conn)
        else:
            motes, rels = graph_repo.neighborhood(self._conn, mote_id, hops)
        return {
            "focus_id": mote_id,
            "motes": [dataclasses.asdict(m) for m in motes],
            "relationships": [dataclasses.asdict(r) for r in rels],
            **_classify(motes, rels),
        }

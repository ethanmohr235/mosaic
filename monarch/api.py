# monarch/api.py
import dataclasses
import json
import logging
import os
import platform
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

import webview

from api_common import coerce_properties as _coerce_properties
from api_common import synchronized as _synchronized
from core.db import get_connection, initialize_database
from core import derivations as derivations_repo
from core import gardens as gardens_repo
from core import garden_io
from core import folders as folders_repo
from core import graph as graph_repo
from core import library as library_repo
from core import links as links_repo
from core import motes as motes_repo
from core import portals as portals_repo
from core import relationships as rel_repo
from core import reserved as reserved_repo
from core import kinds as kinds_repo
from core import scenarios as scenarios_repo
from core import simulation as simulation_repo
from core import sync as sync_repo
from core import solvers as solvers_repo
from core import species as species_repo


def _classify(motes: list, rels: list) -> dict:
    """What each returned mote *is* -- shared by focus() (the currently-
    open garden) and library_snapshot() (a read of _library) so the two
    don't drift.

    One `kinds` map rather than the species_ids/folder_ids arrays this
    used to return: those were two of six kinds core recognizes
    (core/kinds.py), and the frontend had no way to hear about the other
    four, which is why a portal mote's detail panel said "Mote" while
    the canvas drew it with a portal ring. Adding a kind is now a change
    to the table, not to this payload's shape."""
    return {"kinds": kinds_repo.kinds_in(motes, rels)}


def _time_series_payload(result: "simulation_repo.TimeSeriesResult") -> dict:
    """Shapes a simulation_repo.TimeSeriesResult for the JS bridge. The
    steps themselves go through core's own step_payloads(), shared with
    the MCP adapter; only the run-level fields around them are this
    bridge's business. (StepResult.values/unsolved are keyed by
    (mote_id, key) tuples, which don't survive JSON -- that is why
    step_payloads exists and why unsolved counts stay prose-only inside
    each step's summary.)

    `constraint_violations` is flat and already labelled with its step
    (see TimeSeriesResult), because the sim bar shows one advisory line
    for the run rather than a per-tick badge -- a limit broken at any
    tick is worth seeing whichever tick you happen to be scrubbed to."""
    return {
        "steps": simulation_repo.step_payloads(result.steps),
        "failed_at": result.failed_at,
        "error": result.error,
        "run_id": result.run_id,
        "dimension_warnings": result.dimension_warnings,
        "constraint_violations": result.constraint_violations,
    }


def _explanation_payload(explanation) -> dict:
    """Shapes a core/derivations.py Explanation for the JS bridge.

    Recursive, mirroring the structure itself: an input this run derived
    carries its own explanation, and one it did not carries None, which
    is where the panel stops expanding. `also_determined` is flattened
    from tuples into dicts, since tuples reach JS as arrays and a value's
    address is easier to read as named fields."""
    if explanation is None:
        return None
    return {
        "mote_id": explanation.mote_id,
        "key": explanation.key,
        "value": explanation.value,
        "step": explanation.step,
        "method": explanation.method,
        "detail": explanation.detail,
        "note": explanation.note,
        "equations": [
            {
                "rule_mote_id": e.rule_mote_id,
                "rule_name": e.rule_name,
                "anchor_mote_id": e.anchor_mote_id,
                "equation": e.equation,
            }
            for e in explanation.equations
        ],
        "also_determined": [
            {"mote_id": mote_id, "key": key}
            for mote_id, key in explanation.also_determined
        ],
        "inputs": [
            {
                "kind": i.kind,
                "mote_id": i.mote_id,
                "key": i.key,
                "value": i.value,
                "derivation": _explanation_payload(i.derivation),
            }
            for i in explanation.inputs
        ],
    }


# Mora, the .modoc note editor (docs/modoc.md), launched as its own
# process rather than a second window on this one: it holds its own
# garden connection and outlives or predeceases this window freely.
MORA_SCRIPT = Path(__file__).resolve().parent.parent / "run_mora.py"

# Monastery, the calendar window, on exactly the same terms.
MONASTERY_SCRIPT = Path(__file__).resolve().parent.parent / "run_monastery.py"

# Momentum, the task board and timer, on the same terms again.
MOMENTUM_SCRIPT = Path(__file__).resolve().parent.parent / "run_momentum.py"


def _popen_detached(args: list[str], *, quiet: bool = True) -> None:
    """Launches args without waiting on it (see _open_with_os_default for
    why), but still reaps the child on a background thread so it doesn't
    sit as a zombie for as long as MonOSS keeps running -- this app is
    long-lived and every Open click spawns one of these.

    `quiet` discards the child's output, which is right for an OS opener
    (`gio open` chatters about nothing we can act on) and wrong for our
    own apps: a discarded traceback is how a crashed Mora came to look
    like a mysteriously wrong window instead of an error."""
    streams = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL} if quiet else {}
    proc = subprocess.Popen(args, **streams)
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


logger = logging.getLogger(__name__)


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
        # Set by monarch/app.py right after the window is created (the window
        # needs this Api instance to exist first, as its js_api) -- used
        # for native file dialogs in export/import.
        self._window: webview.Window | None = None
        self._best_effort_sync()

    def _best_effort_sync(self) -> None:
        """Push and pull this garden's changes if it's been pointed at a
        sync folder (`sync init`, core/sync.py) -- called on open, on
        garden switch, and on close, so "edit on one device, open the
        app on another later" converges with no action from the user.
        Never allowed to block opening or closing the app: a sync
        folder that's a moment-unmounted cloud drive just means this
        pass is skipped, not a failure to launch."""
        if gardens_repo.get_sync_dir(self._current_garden) is None:
            return
        if not gardens_repo.get_auto_sync_enabled(self._current_garden):
            return
        try:
            sync_repo.run(self._conn, self._current_garden)
        except Exception:
            logger.exception("garden sync failed for %r", self._current_garden)

    @_synchronized
    def shutdown(self) -> None:
        self._best_effort_sync()
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
        self._best_effort_sync()
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
    def export_garden_to_file(self, garden_name: str | None = None) -> dict:
        """Exports the current garden by default, or any named garden --
        via a short-lived connection, so exporting one from the gardens
        list doesn't disturb the currently open one.

        Named for what it does that core's export_garden() doesn't: ask
        the user for a path and write the file. The two used to share a
        name while returning completely different things (a document vs.
        a path or {"cancelled": True})."""
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

    # ---- sync ----

    @_synchronized
    def sync_status(self) -> dict:
        status = sync_repo.status(self._conn, self._current_garden)
        status["auto_sync"] = gardens_repo.get_auto_sync_enabled(self._current_garden)
        return status

    @_synchronized
    def choose_sync_folder(self) -> dict:
        """Native folder picker -- same cancel-handling shape as
        export_garden_to_file's SAVE dialog."""
        path = self._window.create_file_dialog(webview.FileDialog.FOLDER)
        if not path:
            return {"cancelled": True}
        return {"path": path if isinstance(path, str) else path[0]}

    @_synchronized
    def enable_sync(self, folder: str) -> dict:
        # clone=True is always safe here: the only case sync.init() needs
        # it for is "garden doesn't exist locally yet," which can't be
        # true for a garden the GUI already has open.
        return sync_repo.init(self._current_garden, folder, clone=True)

    @_synchronized
    def disable_sync(self) -> None:
        gardens_repo.clear_sync_dir(self._current_garden)

    @_synchronized
    def set_auto_sync(self, enabled: bool) -> None:
        gardens_repo.set_auto_sync_enabled(self._current_garden, enabled)

    @_synchronized
    def sync_now(self) -> dict:
        """Unlike _best_effort_sync(), a manual click doesn't swallow
        errors -- surfaced to the user as a toast via call()'s existing
        rejection handling."""
        return sync_repo.run(self._conn, self._current_garden)

    @_synchronized
    def refresh(self) -> dict:
        """Manual refresh button: sync (if configured) then let the
        caller re-fetch the current view fresh from self._conn. Like
        sync_now() and unlike _best_effort_sync(), a real sync error is
        not swallowed. Unconfigured sync is not an error -- refresh is
        also just "reload from disk" for a garden with no sync set up."""
        if gardens_repo.get_sync_dir(self._current_garden) is None:
            return {"synced": False}
        result = sync_repo.run(self._conn, self._current_garden)
        return {"synced": True, **result}

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

    @_synchronized
    def restore_library_entry(self, raw_properties: dict, id: str) -> dict:
        """Undo counterpart to delete_library_entry() -- recreates a
        just-removed entry with its original id and properties, the same
        create-with-explicit-id pattern deleteSelection()'s own mote-undo
        already uses via create_mote(). Properties arrive already typed
        (the deleted entry's own dict, round-tripped through the undo
        toast's closure) rather than raw form text, same as
        copySelection/pasteAt -- _coerce_properties leaves non-strings
        alone."""
        conn = get_connection(library_repo.LIBRARY_GARDEN)
        try:
            initialize_database(conn)
            mote_id = motes_repo.create_mote(conn, _coerce_properties(raw_properties), id=id)
            return dataclasses.asdict(motes_repo.get_mote(conn, mote_id))
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

    @_synchronized
    def open_mora(self, mote_id: str | None = None) -> dict:
        """Open Mora on this garden, optionally straight to one mote's
        note. Fire-and-forget, through the same detached launcher the
        Open-link action uses, so nothing here blocks on a window that
        may outlive this one.

        `sys.executable` rather than a bare `python`: the app is running
        in a specific virtualenv (docs/ui-setup.md), and the editor has
        to come up in that same one.

        The two processes then hold separate connections to the same
        garden file. Neither observes the other's writes until it
        refetches -- the same rule the MCP server already lives under
        (docs/architecture.md) -- but nothing is lost either way, since
        both write one property at a time.

        `quiet=False` so a Mora that fails to start says so on the
        terminal this app was launched from, rather than failing
        silently. That mattered once already."""
        command = [sys.executable, str(MORA_SCRIPT), "--garden", self._current_garden]
        if mote_id:
            command += ["--note", mote_id]
        _popen_detached(command, quiet=False)
        return {"garden": self._current_garden, "note": mote_id}

    @_synchronized
    def open_monastery(self, mote_id: str | None = None) -> dict:
        """Open Monastery, the calendar, on this garden -- optionally with
        one mote already selected. Everything open_mora says applies here
        unchanged: fire-and-forget through the detached launcher, under
        `sys.executable` so it comes up in this virtualenv, holding its
        own connection to the same garden file."""
        command = [sys.executable, str(MONASTERY_SCRIPT), "--garden", self._current_garden]
        if mote_id:
            command += ["--focus", mote_id]
        _popen_detached(command, quiet=False)
        return {"garden": self._current_garden, "focus": mote_id}

    @_synchronized
    def open_momentum(self, mote_id: str | None = None) -> dict:
        """Open Momentum, the task board and timer, on this garden --
        optionally with one mote already selected. Everything open_mora
        says applies here unchanged: fire-and-forget through the detached
        launcher, under `sys.executable` so it comes up in this
        virtualenv, holding its own connection to the same garden file."""
        command = [sys.executable, str(MOMENTUM_SCRIPT), "--garden", self._current_garden]
        if mote_id:
            command += ["--focus", mote_id]
        _popen_detached(command, quiet=False)
        return {"garden": self._current_garden, "focus": mote_id}

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
    def list_solvers(self) -> list[str]:
        """Every registered solver backend name (see core/solvers.py),
        for the Tools menu's solver picker -- no DB access, but kept
        @_synchronized like every other Api method for consistency."""
        return solvers_repo.list_solvers()

    @_synchronized
    def preview_simulation(
        self, step_count: int, solver: str = "linear",
        solver_configuration: dict | None = None, scenario: str | None = None,
    ) -> dict:
        """Runs step_count steps entirely in memory -- nothing is written
        to this garden. The frontend scrubs through the returned steps
        client-side; committing a chosen step is a separate call. `solver`
        picks the backend for whatever the substitution loop can't
        resolve alone -- see core/solvers.py -- and defaults to today's
        exact, non-iterative behavior.

        `scenario` names a mote gathering overrides (see list_scenarios):
        the garden is previewed as that scenario would have it. A preview
        writes nothing either way -- the difference is which question is
        being asked, not whether the answer is kept."""
        result = simulation_repo.run_time_series(
            self._conn, step_count, commit=False, solver=solver,
            solver_configuration=solver_configuration, scenario=scenario,
        )
        return _time_series_payload(result)

    @_synchronized
    def commit_simulation(
        self, step_count: int, solver: str = "linear", solver_configuration: dict | None = None
    ) -> dict:
        """Re-runs step_count steps (deterministic given the same garden
        and rules) and this time writes the final cumulative result,
        logging one entry the same way a single run_simulation() always
        has. Never trusts step values echoed back from a prior preview
        call -- the only numbers that ever reach the database are ones
        this recomputes itself."""
        result = simulation_repo.run_time_series(
            self._conn, step_count, commit=True, solver=solver, solver_configuration=solver_configuration
        )
        return _time_series_payload(result)

    @_synchronized
    def save_simulation(
        self, step_count: int, solver: str = "linear",
        solver_configuration: dict | None = None, scenario: str | None = None,
    ) -> dict:
        """Like commit_simulation, but never writes the graph -- logs
        every computed tick for later review (e.g. comparing a few
        hypothetical parameter sets) without any of them becoming the
        garden's canonical state. Always re-runs the full step_count
        (unlike commit, which only re-runs up to whichever tick was
        scrubbed to), since there's no "committed position" for a save.

        This is the only way a scenario run can be kept: committing one
        is refused by the engine, since a scenario answers what *would*
        happen and that is not the garden's state."""
        result = simulation_repo.run_time_series(
            self._conn, step_count, commit=False, save=True,
            solver=solver, solver_configuration=solver_configuration, scenario=scenario,
        )
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

    # ---- explanation and dependencies ----

    @_synchronized
    def why(self, mote_id: str, key: str, run_id: str | None = None) -> dict:
        """How a solved value was determined: the equations that produced
        it, the known values they read, and -- recursively -- how each of
        those was itself determined (see core/derivations.py).

        Reads the run log, so it needs a run to have happened; a
        discarded preview leaves nothing to read. Values come back in
        base SI units, which is what the solver worked in, so the numbers
        beside an equation multiply out -- unlike the inspector, which
        shows each mote's own declared unit."""
        explanation = derivations_repo.explain(self._conn, mote_id, key, run_id=run_id)
        return _explanation_payload(explanation)

    @_synchronized
    def dependencies(self, mote_id: str, key: str) -> dict:
        """Everything the dependency index knows about one value, in one
        call -- what could move if it changed, what determining it needs,
        and which equations produce or read it.

        Fetched together because the panel shows them together, and the
        index is rebuilt per call anyway (see core/incidence.py): three
        round trips would each rebuild it."""
        index = simulation_repo.incidence_index(self._conn)
        target = (mote_id, key)
        requirement = index.requires(target)
        return {
            "free": index.is_free(target),
            "determinable": requirement.determinable,
            "impact": [
                {
                    "mote_id": effect.variable[0],
                    "key": effect.variable[1],
                    "distance": effect.distance,
                    "through": sorted({t.rule_name for t in effect.through}),
                }
                for effect in index.impact(target)
            ],
            "needs": [
                {"mote_id": n.variable[0], "key": n.variable[1], "status": n.status}
                for n in requirement.needs
            ],
            "producers": sorted({t.rule_name for t in requirement.producers}),
            "consumers": sorted({t.rule_name for t in index.consumers(target)}),
        }

    # ---- roles and scenarios ----

    @_synchronized
    def check_roles(self) -> dict:
        """How every assembly is wired to its declared parts, plus which
        rules are asserted about a single mote rather than a species (see
        core/simulation.py's check_roles)."""
        report = simulation_repo.check_roles(self._conn)
        return {
            "motes_checked": report.motes_checked,
            "problems": report.problems,
            "once_scoped": report.once_scoped,
        }

    @_synchronized
    def list_scenarios(self) -> list[dict]:
        """Named perturbations of the garden, for the simulation
        scenario picker. Each is a mote with `varies` relationships to
        override motes, which shadow one mote's properties each."""
        return [
            {
                "id": scenario.mote_id,
                "name": scenario.name,
                "override_count": len(scenario.overrides),
                "target_count": len(scenario.target_ids),
            }
            for scenario in scenarios_repo.list_scenarios(self._conn)
        ]

    @_synchronized
    def mote_history(self, mote_id: str) -> dict[str, list[float]]:
        """Every value a mote's properties have taken across the
        garden's committed run history -- backs the inspector's
        sparklines."""
        return simulation_repo.mote_history(self._conn, mote_id)

    # ---- reserved names ----

    @_synchronized
    def reserved_names(self) -> dict:
        """The same reference mcp_server.server.reserved_names() returns,
        rather than the names alone. The app used to get a bare list and
        so could only draw a "this is reserved" dot, while an LLM driving
        MonOSS over MCP got the meanings, the equation language and every
        provider's operations -- the surface with a person behind it was
        the less informed one."""
        return {
            "properties": sorted(reserved_repo.RESERVED_PROPERTIES),
            "relationshipTypes": sorted(reserved_repo.RESERVED_RELATIONSHIP_TYPES),
            "propertyMeanings": reserved_repo.PROPERTY_MEANINGS,
            "relationshipTypeMeanings": reserved_repo.RELATIONSHIP_TYPE_MEANINGS,
            "valueShapes": reserved_repo.VALUE_SHAPES,
            "propertyModelProviders": reserved_repo.PROPERTY_MODEL_PROVIDERS,
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
            return {"focus_id": None, "motes": [], "relationships": [], "kinds": {}}

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

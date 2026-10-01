# api_common.py
"""Shared pieces of the pywebview Api bridge every desktop app (monarch,
mora, monastery, monolith) exposes as its js_api. Before this, each
app's api.py carried its own byte-for-byte copy of `synchronized` and,
where properties are edited as text, `coerce`/`coerce_properties` --
see window_launcher.py for the other half of the boilerplate that used
to get re-copied per app.
"""
import functools
import json
import logging
import os
import platform
import re
import sqlite3
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

from core import annotations as annotations_repo
from core import gardens as gardens_repo
from core import inspect as inspect_repo
from core import links as links_repo
from core import motes as motes_repo
from core import relationships as rel_repo
from core import species as species_repo
from core import sync as sync_repo
from core import undo
from core import units as units_repo
from core.command_bar import run_command, suggest as suggest_command
from core.db import get_connection
from core.reserved import RESERVED_PROPERTIES, RESERVED_RELATIONSHIP_TYPES
from core.undo import UndoManager

logger = logging.getLogger(__name__)


def _push_undo(api, command, coalesce_key=None, coalesce_window: float = 1.5) -> None:
    """Record a command on `api.undo_manager`, if it has one. Every app
    that mixes in InspectorApi also mixes in UndoableApi now, but this
    stays defensive rather than assuming it: InspectorApi is the one
    surface shared across all six windows, and a future one that skips
    UndoableApi should still work, just without undo.

    coalesce_key/coalesce_window pass straight through to
    UndoManager.push() -- see its docstring. AnnotatableApi.move_annotation
    is the first caller to use this."""
    manager = getattr(api, "undo_manager", None)
    if manager is not None:
        manager.push(command, coalesce_key=coalesce_key, coalesce_window=coalesce_window)


def synchronized(method):
    """pywebview invokes exposed methods from a worker thread, and not
    the same one each time. Each Api holds one long-lived sqlite3
    connection (opened with check_same_thread=False), so calls onto it
    must be serialized here rather than left to sqlite3 to police.
    Assumes `self._lock`, a threading.Lock every Api.__init__ sets up."""
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapper


def coerce(raw):
    """A property value as the garden should store it: text-field input
    interpreted as JSON where possible, so a duration typed as "90" is
    stored as a number rather than a string core/calendar.py would
    ignore. Only strings need coercing -- copy/paste sends a mote's
    already-typed properties straight back through create_mote, already
    the right type, and json.loads() raises TypeError on anything that
    isn't str/bytes/bytearray."""
    if not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def coerce_properties(raw_properties: dict) -> dict:
    return {k: coerce(v) for k, v in raw_properties.items()}


def species_ids(conn: sqlite3.Connection) -> list[str]:
    """Every species in the garden, sorted -- the universe each window's
    colour palette claims its entries from.

    Every window colours a mote by its species, and they must agree, so
    the answer cannot be "the species I can currently see": Monarch under
    a hop limit sees a subset of what Monolith's table lists, and a
    palette claimed from a subset would give the same species two
    colours in two windows. The garden's whole species list is the one
    answer all six can ask for. Sorted here rather than in each
    frontend: the claiming order decides who gets which entry when two
    ids want the same one.

    Every window's main payload carries this rather than fetching it on
    the side -- it changes only when somebody makes a new species, and a
    bridge call per poll was enough to cost Monastery a redraw."""
    return inspect_repo.species_ids(conn)


# ---- launching a sibling app ----

REPO_ROOT = Path(__file__).resolve().parent

# app id -> (display name, its run_*.py, the flag that opens straight to
# one mote -- None where that script takes no such flag). The single
# source both the ⌘J switcher (below) and each app's own frontend read,
# so a seventh app is one entry here rather than a seventh copy-pasted
# open_<sibling>() per existing app.
APPS = {
    "monarch": ("Monarch", REPO_ROOT / "run_monarch.py", None),
    "mora": ("Mora", REPO_ROOT / "run_mora.py", "--note"),
    "monastery": ("Monastery", REPO_ROOT / "run_monastery.py", "--focus"),
    # The console (docs/console.md): the one window the others are
    # being ported into. Listed so an old window can open it; it takes
    # no focus flag -- selection lives in the console's own store.
    "console": ("Mosaic", REPO_ROOT / "run_console.py", None),
}


# The Gardens window (gardens_app/), deliberately not an entry in APPS
# above: those six are windows *onto* a garden, launched with --garden
# and optionally a mote to focus. This one is a window onto the list of
# gardens, so "open it on garden X" means "start with X selected", and
# the ⌘J switcher has no business offering it as a seventh place to read
# your motes.
GARDENS_SCRIPT = REPO_ROOT / "run_gardens.py"


def open_gardens(garden: str | None = None, *, sync: bool = False) -> dict:
    """Open the shared garden manager, as its own detached process.

    Every app exposes this as `open_gardens()` on its bridge, which is
    the whole point: creating, renaming, importing and deleting gardens
    used to exist only inside Monarch's own modal, so doing any of it
    from Motion meant opening Monarch first. One window, one
    implementation, six callers.

    Not a singleton: a second Gardens window is a duplicate list rather
    than anything dangerous, and pywebview can't raise and focus a
    window belonging to another process -- so a click that silently did
    nothing (because the first one is buried behind the app that opened
    it) would be the worse failure. Same trade window_launcher.choose_port
    already makes for second copies of the apps."""
    command = [sys.executable, str(GARDENS_SCRIPT)]
    if garden:
        command += ["--garden", garden]
    if sync:
        command.append("--sync")
    popen_detached(command, quiet=False)
    return {"gardens": True, "select": garden, "sync": sync}


def popen_detached(args: list[str], *, quiet: bool = True) -> None:
    """Launches args without waiting on it, but still reaps the child on a
    background thread so it doesn't sit as a zombie for as long as MonOSS
    keeps running -- this app is long-lived and every launch spawns one
    of these.

    `quiet` discards the child's output, which is right for an OS opener
    (`gio open` chatters about nothing we can act on) and wrong for our
    own apps: a discarded traceback is how a crashed sibling app came to
    look like a mysteriously wrong window instead of an error."""
    streams = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL} if quiet else {}
    proc = subprocess.Popen(args, **streams)
    threading.Thread(target=proc.wait, daemon=True).start()


def best_effort_sync(garden: str) -> None:
    """Every app's `_best_effort_sync()` calls this on open, garden
    switch, and close: it pushes/pulls `garden` on a background thread,
    using its own short-lived connection rather than the caller's, so a
    sync server that's slow or offline (compass turned off, Tailscale
    peer unreachable) never blocks opening, switching gardens, or
    closing a window -- whichever one of those called this has often
    already replaced or closed its own connection by the time the
    thread gets to run. Errors are logged, not raised: this path is
    opportunistic, unlike the visible refresh()/sync_now() button,
    which waits and surfaces a real failure to the user."""
    if gardens_repo.get_sync_dir(garden) is None:
        return

    def _run() -> None:
        conn = get_connection(garden)
        try:
            sync_repo.run(conn, garden)
        except Exception:
            logger.exception("garden sync failed for %r", garden)
        finally:
            conn.close()

    threading.Thread(target=_run, daemon=True).start()


def open_app(app_id: str, garden: str, mote_id: str | None = None) -> dict:
    """Launches another MonOSS app on this same garden, as its own
    detached process rather than a second window here: each app holds
    its own garden connection and outlives or predates this one freely.
    The two processes then hold separate connections to the same garden
    file -- neither observes the other's writes until it refetches, but
    nothing is lost either way, since both write one property at a time.

    `sys.executable` rather than a bare `python`: the app is running in
    a specific virtualenv (docs/ui-setup.md), and the sibling has to come
    up in that same one. `quiet=False` so a sibling that fails to start
    says so on the terminal this app was launched from, rather than
    failing silently."""
    _name, script, focus_flag = APPS[app_id]
    command = [sys.executable, str(script), "--garden", garden]
    if mote_id and focus_flag:
        command += [focus_flag, mote_id]
    popen_detached(command, quiet=False)
    return {"app": app_id, "garden": garden, "focus": mote_id}


def open_with_os_default(path: str) -> None:
    """Launches a local file with whatever the OS has registered as the
    default app for its type -- e.g. a .py file opens in VSCode, not as
    text in a browser tab, which is what webbrowser.open() would do.

    Fire-and-forget: the callers are @synchronized, so blocking here
    until the opener command exits would hold that lock -- and freeze
    every other Api call -- for as long as the opener (or the app it
    launches) takes to return, which observably is not always fast.

    Lived in monarch/api.py until the inspector became shared. A `url`
    property means the same thing in all six windows, so the one window
    that could follow one was an accident of where the panel happened to
    be written."""
    system = platform.system()
    if system == "Darwin":
        popen_detached(["open", path])
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
            popen_detached(["gio", "open", path])
        except FileNotFoundError:
            popen_detached(["xdg-open", path])


# ---- the inspector's bridge contract ----



class InspectorApi:
    """The mote-editing contract the floating inspector speaks, identical
    in all six windows.

    The inspector is one surface wherever you are (frontend_shared/
    inspector.js), so it has to find the same methods on every bridge it
    is dropped into. Before this, editing a mote's properties meant
    whatever that app's own detail panel had grown: Monarch could add a
    property and set a species, Motion could only overwrite the values
    that already existed, Mora could do neither. Mixed into each app's
    Api rather than reimplemented six times.

    Assumes `self._conn` and `self._lock`, which every Api already has.
    An app that needs different write semantics overrides one method and
    inherits the rest (Motion's old window, now the console's `mo tion`,
    redirected writes onto a scenario's override this way)."""

    # ---- reading ----

    def inspect_mote(self, mote_id: str) -> dict | None:
        """Everything the inspector draws for one mote, in one call.

        One call rather than the six it would otherwise take (mote,
        species, template, siblings, relationships, their labels)
        because this runs on every selection change in a graph you can
        arrow-key through, and each one is a pywebview bridge round trip
        serialized behind `self._lock`."""
        with self._lock:
            return self._inspector_payload(mote_id)

    def _inspector_payload(self, mote_id: str) -> dict | None:
        """The payload, without taking the lock -- every writing method
        below already holds it and returns the fresh state. Built by
        core/inspect.py, which the console's props pane reads too."""
        return inspect_repo.inspect(self._conn, mote_id)



    @synchronized
    def inspector_search(self, query: str, limit: int = 8) -> list[dict]:
        """Candidates for the relationship picker. Its own method rather
        than a reuse of each app's `search`, which returns that app's own
        row shape (Monolith's carries column values, Mora's carries note
        excerpts) and differs in whether it searches property values."""
        conn = self._conn
        found = motes_repo.search_motes(conn, query, limit=limit) if query.strip() \
            else motes_repo.list_motes(conn)[:limit]
        rows = []
        for mote in found[:limit]:
            species = species_repo.species_of(conn, mote.id)
            rows.append({
                "id": mote.id,
                "label": inspect_repo.label(mote),
                "speciesId": species[0].id if species else None,
                "speciesLabel": inspect_repo.label(species[0]) if species else None,
            })
        return rows

    # ---- writing ----

    # Each of these returns the whole refreshed payload rather than an ok
    # flag, so the inspector redraws from what the garden actually holds
    # instead of from what it assumed the write did. A rename that the
    # garden coerced, a relationship the other end already had -- the
    # panel shows the truth on the next frame either way.

    @synchronized
    def inspector_set_property(self, mote_id: str, key: str, raw_value) -> dict | None:
        value = coerce(raw_value)
        mote = motes_repo.get_mote(self._conn, mote_id)
        old_props = {key: mote.properties[key]} if mote and key in mote.properties else {}
        motes_repo.update_mote(self._conn, mote_id, {key: value})
        _push_undo(self, undo.for_update_mote(self._conn, mote_id, old_props, {key: value}, label=f"set {key}"))
        return self._inspector_payload(mote_id)

    @synchronized
    def inspector_delete_property(self, mote_id: str, key: str) -> dict | None:
        """Deleting a property that carries a unit also drops its entry
        in the mote's `units` map -- otherwise the map keeps naming a
        property that no longer exists, and a later property reusing
        that key would silently inherit a unit it never declared."""
        mote = motes_repo.get_mote(self._conn, mote_id)
        if mote is None:
            return self._inspector_payload(mote_id)
        commands = []
        units = units_repo.parse_units_property(mote.properties.get(units_repo.UNITS_KEY))
        if key in units:
            old_units = mote.properties.get(units_repo.UNITS_KEY)
            units = dict(units)
            del units[key]
            motes_repo.update_mote(self._conn, mote_id, {units_repo.UNITS_KEY: units})
            commands.append(undo.for_update_mote(
                self._conn, mote_id, {units_repo.UNITS_KEY: old_units}, {units_repo.UNITS_KEY: units},
                label=f"clear {key}",
            ))
        if key in mote.properties:
            old_value = mote.properties[key]
            if motes_repo.delete_property(self._conn, mote_id, key):
                commands.append(undo.for_delete_property(self._conn, mote_id, key, old_value, label=f"clear {key}"))
        if commands:
            _push_undo(self, undo.combine(f"clear {key}", commands))
        return self._inspector_payload(mote_id)

    @synchronized
    def inspector_rename_property(self, mote_id: str, old: str, new: str) -> dict | None:
        """Rename in place, keeping the value. Delete-then-add is what
        the frontend would otherwise have to do, and that loses the value
        if the second call fails.

        A property's entry in the mote's `units` map is keyed by the same
        name, so a rename that left it behind would silently strip the
        unit from the renamed property while leaving a stale entry for a
        key that no longer exists."""
        mote = motes_repo.get_mote(self._conn, mote_id)
        new = new.strip()
        if mote is None or old not in mote.properties or not new:
            return self._inspector_payload(mote_id)
        updates = {new: mote.properties[old]}
        units = units_repo.parse_units_property(mote.properties.get(units_repo.UNITS_KEY))
        old_updates_props = {new: mote.properties[new]} if new in mote.properties else {}
        if old in units:
            old_units = mote.properties.get(units_repo.UNITS_KEY)
            units = dict(units)
            units[new] = units.pop(old)
            updates[units_repo.UNITS_KEY] = units
            old_updates_props[units_repo.UNITS_KEY] = old_units
        motes_repo.update_mote(self._conn, mote_id, updates)
        motes_repo.delete_property(self._conn, mote_id, old)
        _push_undo(self, undo.combine(f"rename {old}", [
            undo.for_update_mote(self._conn, mote_id, old_updates_props, updates, label=f"rename {old}"),
            undo.for_delete_property(self._conn, mote_id, old, mote.properties[old], label=f"rename {old}"),
        ]))
        return self._inspector_payload(mote_id)

    @synchronized
    def inspector_relate(self, from_id: str, to_id: str, type_: str) -> dict | None:
        type_ = type_.strip() or "related_to"
        rel_id = rel_repo.create_relationship(self._conn, from_id, to_id, type_)
        _push_undo(self, undo.for_create_relationship(self._conn, rel_id, from_id, to_id, type_, label=f"link {type_}"))
        return self._inspector_payload(from_id)

    @synchronized
    def inspector_unrelate(self, mote_id: str, rel_id: str) -> dict | None:
        relationship = rel_repo.get_relationship(self._conn, rel_id)
        if relationship is not None and rel_repo.delete_relationship(self._conn, rel_id):
            _push_undo(self, undo.for_delete_relationship(
                self._conn, rel_id, relationship.from_id, relationship.to_id, relationship.type,
                label=f"unlink {relationship.type}",
            ))
        return self._inspector_payload(mote_id)

    @synchronized
    def inspector_set_species(self, mote_id: str, species_id: str) -> dict | None:
        """Set, not add. core.species.set_species deliberately allows a
        mote to be an instance of several species, but the inspector
        shows one species in one control -- so picking a new one here
        means replacing what that control was showing, rather than
        quietly accumulating memberships the control cannot display.
        Multi-species motes remain expressible over MCP and the CLI."""
        conn = self._conn
        commands = []
        for rel in rel_repo.list_relationships(conn, from_id=mote_id,
                                               type_=species_repo.INSTANCE_OF):
            if rel_repo.delete_relationship(conn, rel.id):
                commands.append(undo.for_delete_relationship(
                    conn, rel.id, rel.from_id, rel.to_id, rel.type, label="set species",
                ))
        if species_id:
            rel_id = species_repo.set_species(conn, mote_id, species_id)
            commands.append(undo.for_create_relationship(
                conn, rel_id, mote_id, species_id, species_repo.INSTANCE_OF, label="set species",
            ))
        if commands:
            _push_undo(self, undo.combine("set species", commands))
        return self._inspector_payload(mote_id)

    @synchronized
    def inspector_set_template(self, mote_id: str, species_id: str) -> dict | None:
        """Replace the species' template, rather than add a second one.

        core.species.template_for uses only the first template it finds
        and says so: a species with several is a modelling mistake it
        declines to merge. So a bare set_template here would leave the
        panel's "Replace the template" button doing nothing observable --
        the new relationship written, the old one still answering --
        which is the worst of the three possible behaviours."""
        conn = self._conn
        commands = []
        for rel in rel_repo.list_relationships(conn, type_=species_repo.TEMPLATE_OF):
            if rel.to_id == species_id and rel.from_id != mote_id:
                if rel_repo.delete_relationship(conn, rel.id):
                    commands.append(undo.for_delete_relationship(
                        conn, rel.id, rel.from_id, rel.to_id, rel.type, label="set template",
                    ))
        rel_id = species_repo.set_template(conn, mote_id, species_id)
        commands.append(undo.for_create_relationship(
            conn, rel_id, mote_id, species_id, species_repo.TEMPLATE_OF, label="set template",
        ))
        _push_undo(self, undo.combine("set template", commands))
        return self._inspector_payload(mote_id)

    @synchronized
    def inspector_open_link(self, target: str) -> dict:
        """Follow a `url` property: a web address in the browser, a local
        path in whatever the OS opens that file type with."""
        kind, value = links_repo.classify_link(target)
        if kind == "url":
            webbrowser.open(value)
        else:
            open_with_os_default(value)
        return {"opened": target}

    @synchronized
    def inspector_why(self, mote_id: str, key: str) -> dict | None:
        """Where a value came from. The old windows have no solver, so the
        answer is None and the Advanced section doesn't draw the control;
        the console answers it with `why ‹m.k›` (core/motion.py). Declared
        here so the frontend can call it unconditionally."""
        return None


class CommandBarApi:
    """The "/" command bar's bridge, identical in all six windows -- the
    same one-surface argument as InspectorApi just above, applied to
    typing instead of clicking. `frontend_shared/commandbar.js` speaks
    only this one method; the grammar itself (property/rule/freedom/
    relationship/species/delete/navigate) lives in core/command_bar.py, which is pure
    and takes a connection rather than an Api, so it is tested once there
    instead of once per window.

    Assumes `self._conn` and `self._lock`, like InspectorApi. Not mixed
    into gardens_app.api.Api: that window holds no garden connection, and
    a command bar that edits motes has nothing to reach from a window
    about gardens themselves."""

    @synchronized
    def command_run(self, text: str, confirm: bool = False) -> dict:
        result = run_command(self._conn, text, confirm=confirm)
        if result.command is not None:
            _push_undo(self, result.command)
        return {
            "ok": result.ok,
            "message": result.message,
            "moteId": result.mote_id,
            "created": result.created,
            "needsConfirm": result.needs_confirm,
            "choices": result.choices,
        }

    @synchronized
    def command_suggest(self, text: str, cursor: int | None = None) -> list[str]:
        return suggest_command(self._conn, text, cursor)


class UndoableApi:
    """The generic api.undo()/api.redo() every desktop app exposes,
    backed by one core.undo.UndoManager per Api instance -- session-only,
    same lifetime as the window. `_init_undo()` must run in `Api.__init__`;
    mutating methods elsewhere in that Api push a core.undo.Command after
    each change they make (see core/undo.py's factory functions).

    frontend_shared/undo.js is the JS half: it binds Ctrl/Cmd+Z and
    Ctrl/Cmd+Shift+Z to these two methods and toasts the label each
    returns."""

    def _init_undo(self) -> None:
        self.undo_manager = UndoManager()

    @synchronized
    def undo(self) -> dict:
        return {"undone": self.undo_manager.undo()}

    @synchronized
    def redo(self) -> dict:
        return {"redone": self.undo_manager.redo()}


class AnnotatableApi:
    """Annotation CRUD -- sticky notes, group boxes, pen strokes
    (core/annotations.py) -- identical in shape to
    InspectorApi -- Monarch is the only app mixing this in for now (see
    docs/architecture.md's Annotations section). Local-only: no method
    here ever writes to core/sync.py or the oplog.

    Assumes `self._conn` and `self._lock`, like InspectorApi. Each
    writing method returns the refreshed annotation (or None/bool for
    delete) and, where `self.undo_manager` exists, pushes a Command so
    Ctrl+Z covers annotations the same as it covers motes."""

    @synchronized
    def list_annotations(self) -> list[dict]:
        return [_annotation_payload(a) for a in annotations_repo.list_annotations(self._conn)]

    @synchronized
    def create_sticky_note(
        self, anchor_mode: str, x: float, y: float,
        mote_id: str | None = None, text: str = "",
    ) -> dict:
        annotation_id = annotations_repo.create_sticky_note(
            self._conn, anchor_mode=anchor_mode, x=x, y=y, text=text, mote_id=mote_id,
        )
        snapshot = annotations_repo.to_dict(annotations_repo.get_annotation(self._conn, annotation_id))
        _push_undo(self, undo.for_create_annotation(self._conn, snapshot, label="add a note"))
        return _annotation_payload(annotations_repo.get_annotation(self._conn, annotation_id))

    @synchronized
    def update_annotation_text(self, annotation_id: str, text: str) -> dict | None:
        annotation = annotations_repo.get_annotation(self._conn, annotation_id)
        if annotation is None:
            return None
        old_text = annotation.text
        annotations_repo.update_annotation(self._conn, annotation_id, text=text)
        label = "rename a group" if annotation.type == annotations_repo.GROUP else "edit a note"
        _push_undo(self, undo.for_update_annotation(
            self._conn, annotation_id, {"text": old_text}, {"text": text}, label=label,
        ))
        return _annotation_payload(annotations_repo.get_annotation(self._conn, annotation_id))

    @synchronized
    def move_annotation(self, annotation_id: str, x: float, y: float) -> dict | None:
        annotation = annotations_repo.get_annotation(self._conn, annotation_id)
        if annotation is None:
            return None
        old_xy = (annotation.x, annotation.y)
        annotations_repo.move_annotation(self._conn, annotation_id, x, y)
        _push_undo(
            self,
            undo.for_move_annotation(
                self._conn, annotation_id, old_xy, (x, y), label=f"move {_noun(annotation.type)}",
            ),
            coalesce_key=("annotation-move", annotation_id),
        )
        return _annotation_payload(annotations_repo.get_annotation(self._conn, annotation_id))

    @synchronized
    def delete_annotation(self, annotation_id: str) -> bool:
        annotation = annotations_repo.get_annotation(self._conn, annotation_id)
        if annotation is None:
            return False
        snapshot = annotations_repo.to_dict(annotation)
        deleted = annotations_repo.delete_annotation(self._conn, annotation_id)
        if deleted:
            _push_undo(self, undo.for_delete_annotation(
                self._conn, snapshot, label=f"delete {_noun(annotation.type)}",
            ))
        return deleted

    @synchronized
    def move_annotations(self, moves: list[dict]) -> list[dict]:
        """Several annotations moved by one gesture (a multi-selection
        drag, or a group box carrying the ink inside it): moves is
        [{id, x, y}, ...], and the whole batch is one undo step."""
        commands, moved, types = [], [], []
        for move in moves:
            annotation = annotations_repo.get_annotation(self._conn, move["id"])
            if annotation is None:
                continue
            old_xy = (annotation.x, annotation.y)
            annotations_repo.move_annotation(self._conn, annotation.id, move["x"], move["y"])
            commands.append(undo.for_move_annotation(
                self._conn, annotation.id, old_xy, (move["x"], move["y"]), label="move",
            ))
            types.append(annotation.type)
            moved.append(_annotation_payload(annotations_repo.get_annotation(self._conn, annotation.id)))
        if commands:
            _push_undo(self, undo.combine(f"move {_count_noun(types)}", commands))
        return moved

    @synchronized
    def delete_annotations(self, annotation_ids: list[str]) -> int:
        """Delete a selection of annotations as one undo step."""
        commands, types = [], []
        for annotation_id in annotation_ids:
            annotation = annotations_repo.get_annotation(self._conn, annotation_id)
            if annotation is None:
                continue
            snapshot = annotations_repo.to_dict(annotation)
            if annotations_repo.delete_annotation(self._conn, annotation_id):
                commands.append(undo.for_delete_annotation(self._conn, snapshot, label="delete"))
                types.append(annotation.type)
        if commands:
            _push_undo(self, undo.combine(f"delete {_count_noun(types)}", commands))
        return len(commands)

    @synchronized
    def resize_annotation(
        self, annotation_id: str, x: float, y: float, width: float, height: float,
    ) -> dict | None:
        """x/y come along because a note is stored by its centre and a
        resize keeps its top-left corner fixed, so the centre moves too."""
        annotation = annotations_repo.get_annotation(self._conn, annotation_id)
        if annotation is None:
            return None
        before = annotations_repo.to_dict(annotation)
        annotations_repo.update_annotation(
            self._conn, annotation_id, x=x, y=y, width=width, height=height,
        )
        after = annotations_repo.to_dict(annotations_repo.get_annotation(self._conn, annotation_id))
        _push_undo(self, undo.for_replace_annotation(
            self._conn, before, after, label=f"resize {_noun(annotation.type)}",
        ))
        return _annotation_payload(annotations_repo.get_annotation(self._conn, annotation_id))

    @synchronized
    def anchor_annotation(
        self, annotation_id: str, anchor_mode: str, mote_id: str | None, x: float, y: float,
    ) -> dict | None:
        """Attach a note to a mote or detach it back to the canvas. The
        frontend converts x/y (an offset from the mote vs. an absolute
        position) so the note doesn't jump."""
        annotation = annotations_repo.get_annotation(self._conn, annotation_id)
        if annotation is None:
            return None
        before = annotations_repo.to_dict(annotation)
        annotations_repo.set_anchor(
            self._conn, annotation_id, anchor_mode=anchor_mode, mote_id=mote_id, x=x, y=y,
        )
        after = annotations_repo.to_dict(annotations_repo.get_annotation(self._conn, annotation_id))
        label = "attach a note" if anchor_mode == "mote" else "detach a note"
        _push_undo(self, undo.for_replace_annotation(self._conn, before, after, label=label))
        return _annotation_payload(annotations_repo.get_annotation(self._conn, annotation_id))

    @synchronized
    def create_group(
        self, x: float, y: float, width: float, height: float,
        member_ids: list[str], color: str | None = None,
    ) -> dict:
        """A group box around member_ids. A mote belongs to one group at
        most, so any member already in another group leaves it -- in the
        same undo step as the create."""
        commands = self._release_members(member_ids, keep_group_id=None)
        group_id = annotations_repo.create_group(
            self._conn, x=x, y=y, width=width, height=height, members=member_ids, color=color,
        )
        snapshot = annotations_repo.to_dict(annotations_repo.get_annotation(self._conn, group_id))
        commands.append(undo.for_create_annotation(self._conn, snapshot, label="add a group"))
        _push_undo(self, undo.combine("add a group", commands))
        return _annotation_payload(annotations_repo.get_annotation(self._conn, group_id))

    @synchronized
    def set_group_members(self, changes: list[dict]) -> list[dict]:
        """changes is [{groupId, members}, ...] -- each group's new full
        member list, e.g. after motes were dragged out of one box and into
        another. Members named here leave any group not in `changes`, so
        the one-group-per-mote rule holds whatever the caller sent. One
        undo step for the lot."""
        commands, changed = [], []
        named = {change["groupId"] for change in changes}
        for change in changes:
            group = annotations_repo.get_annotation(self._conn, change["groupId"])
            if group is None or group.type != annotations_repo.GROUP:
                continue
            members = list(dict.fromkeys(change["members"]))
            commands.extend(self._release_members(members, keep_group_id=group.id, skip=named))
            commands.append(self._update_data(group, {**group.data, "members": members}))
            changed.append(_annotation_payload(annotations_repo.get_annotation(self._conn, group.id)))
        if commands:
            _push_undo(self, undo.combine("regroup motes", commands))
        return changed

    @synchronized
    def create_stroke(
        self, x: float, y: float, points: list[list[float]], width: float,
        color: str | None = None,
    ) -> dict:
        stroke_id = annotations_repo.create_stroke(
            self._conn, x=x, y=y, points=points, width=width, color=color,
        )
        snapshot = annotations_repo.to_dict(annotations_repo.get_annotation(self._conn, stroke_id))
        _push_undo(self, undo.for_create_annotation(self._conn, snapshot, label="draw"))
        return _annotation_payload(annotations_repo.get_annotation(self._conn, stroke_id))

    @synchronized
    def recolor_annotations(self, annotation_ids: list[str], color: str | None) -> list[dict]:
        """Recolor a selection as one undo step; None means the automatic
        color again."""
        commands, types, changed = [], [], []
        for annotation_id in annotation_ids:
            annotation = annotations_repo.get_annotation(self._conn, annotation_id)
            if annotation is None or annotation.color == color:
                continue
            annotations_repo.update_annotation(self._conn, annotation_id, color=color)
            commands.append(undo.for_update_annotation(
                self._conn, annotation_id, {"color": annotation.color}, {"color": color}, label="recolor",
            ))
            types.append(annotation.type)
            changed.append(_annotation_payload(annotations_repo.get_annotation(self._conn, annotation_id)))
        if commands:
            _push_undo(self, undo.combine(f"recolor {_count_noun(types)}", commands))
        return changed

    def _release_members(
        self, member_ids: list[str], keep_group_id: str | None, skip: set[str] = frozenset(),
    ) -> list:
        """Take member_ids out of every group but keep_group_id (and those
        in `skip`, which the caller is rewriting itself). Returns the undo
        commands for the edits it made."""
        wanted = set(member_ids)
        commands = []
        for group in annotations_repo.list_annotations(self._conn):
            if group.type != annotations_repo.GROUP or group.id == keep_group_id or group.id in skip:
                continue
            members = group.data.get("members", [])
            kept = [m for m in members if m not in wanted]
            if len(kept) != len(members):
                commands.append(self._update_data(group, {**group.data, "members": kept}))
        return commands

    def _update_data(self, annotation, data: dict):
        annotations_repo.update_annotation(self._conn, annotation.id, data=data)
        return undo.for_update_annotation(
            self._conn, annotation.id, {"data": annotation.data}, {"data": data}, label="regroup",
        )


def _annotation_payload(annotation) -> dict:
    """An Annotation as the frontend reads it: camelCase keys, matching
    every other payload in this module (see _inspector_payload)."""
    return {
        "id": annotation.id,
        "type": annotation.type,
        "anchorMode": annotation.anchor_mode,
        "moteId": annotation.mote_id,
        "x": annotation.x,
        "y": annotation.y,
        "text": annotation.text,
        "color": annotation.color,
        "width": annotation.width,
        "height": annotation.height,
        "z": annotation.z,
        "data": annotation.data,
        "createdAt": annotation.created_at,
        "updatedAt": annotation.updated_at,
    }


_ANNOTATION_NOUNS = {
    annotations_repo.STICKY_NOTE: ("a note", "notes"),
    annotations_repo.GROUP: ("a group", "groups"),
    annotations_repo.STROKE: ("a drawing", "drawings"),
}


def _noun(annotation_type: str) -> str:
    """How an undo label names one annotation of this type."""
    return _ANNOTATION_NOUNS.get(annotation_type, ("an annotation", "annotations"))[0]


def _count_noun(types: list[str]) -> str:
    """"a note", "3 groups", or "4 annotations" for a mixed batch."""
    if len(types) == 1:
        return _noun(types[0])
    kinds = set(types)
    plural = _ANNOTATION_NOUNS.get(types[0], ("", "annotations"))[1] if len(kinds) == 1 else "annotations"
    return f"{len(types)} {plural}"













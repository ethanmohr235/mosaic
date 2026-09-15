# monarch/api.py
import dataclasses
import logging
import sys
import threading
import webbrowser
from pathlib import Path

import webview

from api_common import CommandBarApi, InspectorApi
from api_common import coerce_properties as _coerce_properties
from api_common import open_app as _open_app
from api_common import open_gardens as _open_gardens
from api_common import open_with_os_default as _open_with_os_default
from api_common import popen_detached as _popen_detached
from api_common import species_ids as _species_ids
from api_common import synchronized as _synchronized
from core.db import get_connection, initialize_database
from core import gardens as gardens_repo
from core import graph as graph_repo
from core import links as links_repo
from core import motes as motes_repo
from core import query as query_repo
from core.query import Index, Query
from core import relationships as rel_repo
from core import reserved as reserved_repo
from core import kinds as kinds_repo
from core import sync as sync_repo
from core import species as species_repo


def _classify(motes: list, rels: list) -> dict:
    """What each returned mote *is*, for focus()'s payload.

    One `kinds` map rather than a hardcoded array per kind: it's a
    general index over whatever core/kinds.py recognizes, so adding a
    kind is a change to that table, not to this payload's shape."""
    return {"kinds": kinds_repo.kinds_in(motes, rels)}


# Mora, the .modoc note editor (docs/modoc.md), launched as its own
# process rather than a second window on this one: it holds its own
# garden connection and outlives or predeceases this window freely.
MORA_SCRIPT = Path(__file__).resolve().parent.parent / "run_mora.py"

# Monastery, the calendar window, on exactly the same terms.
MONASTERY_SCRIPT = Path(__file__).resolve().parent.parent / "run_monastery.py"

# Momentum, the task board and timer, on the same terms again.
MOMENTUM_SCRIPT = Path(__file__).resolve().parent.parent / "run_momentum.py"


logger = logging.getLogger(__name__)


class Api(InspectorApi, CommandBarApi):
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
        return gardens_repo.list_gardens()

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
    def open_gardens(self, sync: bool = False) -> dict:
        """Open the shared Gardens window (gardens_app/), which is where
        creating, renaming, deleting, importing, exporting and syncing a
        garden now live -- for every window, not just this one.

        This window used to carry all of that privately, as a modal only
        Monarch had, which is why "make a new garden" from Motion meant
        opening Monarch first. What stays here is the half that is
        genuinely about *this* window: which garden it is looking at
        (list_gardens/switch_garden above)."""
        return _open_gardens(self._current_garden, sync=sync)

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

    @_synchronized
    def open_app(self, app_id: str, mote_id: str | None = None) -> dict:
        """Launch any MonOSS app on this same garden from the ⌘J switcher
        -- the app-agnostic counterpart to open_mora/open_monastery/
        open_momentum above (which stay as they are, since Monarch's own
        Tools menu already calls each by name). Monarch is the one app
        that can already reach every sibling, so this is also how the
        switcher opens a second Monarch window on itself, same as any
        other app id -- see window_launcher.choose_port for why that's a
        reasonable thing to do."""
        return _open_app(app_id, self._current_garden, mote_id)

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

    # ---- saved view filter ----

    def _updated(self) -> dict[str, str]:
        """Mirrors monolith/api.py::_updated -- when each mote last
        changed, for the `updated` builtin a saved view's query can
        filter or sort on."""
        rows = self._conn.execute(
            "SELECT mote_id, MAX(updated_at) AS updated FROM mote_properties GROUP BY mote_id"
        ).fetchall()
        return {row["mote_id"]: row["updated"] for row in rows if row["updated"]}

    @_synchronized
    def view_filter_ids(self, view_id: str) -> list[str] | None:
        """Every mote id Monolith's saved view `view_id` matches,
        evaluated fresh over the whole garden -- what the control panel's
        saved-view filter keeps on screen, hiding everything else. A
        saved view is an ordinary mote carrying a `query` property
        (core/query.py); this is Monarch's only reach into that
        language, since the graph doesn't otherwise care what a saved
        view is. None if the mote is gone or no longer carries a query,
        so a stale picker selection clears itself instead of raising."""
        mote = motes_repo.get_mote(self._conn, view_id)
        if mote is None or not query_repo.has_query(mote.properties):
            return None
        motes, relationships = graph_repo.whole_garden(self._conn)
        index = Index.of(motes, relationships, self._updated())
        query = Query.from_json(mote.properties[query_repo.QUERY_KEY])
        return [m.id for m in query_repo.evaluate(index, motes, query)]

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
            return {"focus_id": None, "motes": [], "relationships": [], "kinds": {},
                    "speciesIds": _species_ids(self._conn)}

        if hops is None:
            motes, rels = graph_repo.whole_garden(self._conn)
        else:
            motes, rels = graph_repo.neighborhood(self._conn, mote_id, hops)
        return {
            "focus_id": mote_id,
            "motes": [dataclasses.asdict(m) for m in motes],
            "relationships": [dataclasses.asdict(r) for r in rels],
            # Asked of the garden, not of `motes`: under a hop limit this
            # answer is a neighborhood, and a palette claimed from a
            # neighborhood would colour a species differently here than
            # in the four other windows. See api_common.species_ids.
            "speciesIds": _species_ids(self._conn),
            **_classify(motes, rels),
        }

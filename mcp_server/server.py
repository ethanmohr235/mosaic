# mcp_server/server.py
"""MCP-facing bridge over the core data layer -- a third adapter
alongside cli/ and ui/, exposing the same repository functions as tools
an MCP client can call.

Deliberately *not* named `mcp/`: run_mcp.py puts the project root on
sys.path, so a top-level package by that name would shadow the installed
`mcp` SDK.

Unlike ui/api.py, which holds one long-lived connection for the open
garden, every tool here opens and closes its own (see _connect). MCP
calls are stateless and the server is a separate process from the UI --
holding a connection open for the server's whole life would mean sitting
on a SQLite lock while the user works in the GUI on the same file.
"""
import dataclasses
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from mcp.server import MCPServer
from mcp.server.auth.provider import OAuthAuthorizationServerProvider
from mcp.server.auth.settings import AuthSettings

from core import folders as folders_repo
from core import garden_io
from core import gardens as gardens_repo
from core import library as library_repo
from core import graph as graph_repo
from core import modoc as modoc_repo
from core import modoc_bind as modoc_bind_repo
from core import motes as motes_repo
from core import portals as portals_repo
from core import relationships as rel_repo
from core import reserved as reserved_repo
from core import simulation as simulation_repo
from core import species as species_repo
from core.db import get_connection, initialize_database

# How long a write waits out a lock held by another process (the GUI,
# the CLI) before giving up. core/db.py deliberately isn't touched: this
# is set per-connection here, so the CLI and UI keep their current
# behavior exactly.
_BUSY_TIMEOUT_MS = 5000

_INSTRUCTIONS = (
    "MonOSS is a knowledge graph where every entity -- person, task, note, "
    "meeting, idea -- is one object type called a mote, carrying free-form "
    "JSON properties and connected by typed relationships. Separate graphs "
    "are called gardens; every tool takes an optional `garden` and defaults "
    "to 'default'. A mote acts as a species when others point at it with an "
    "`instance_of` relationship, giving it a type; a species may have a "
    "template whose properties new instances start from. Prefer neighborhood() "
    "over list_motes() to explore a large garden, and search_motes() over "
    "list_motes() when you're looking for something specific by name or "
    "property rather than browsing. A rule mote's `equation` can reference "
    "another mote's property across a relationship with targets('type').prop "
    "or sources('type').prop -- these resolve live in every simulation run, "
    "so prefer them over hardcoding a value copied from another mote."
)


@contextmanager
def _connect(garden: str | None) -> Iterator[sqlite3.Connection]:
    """One short-lived connection per tool call. garden=None resolves to
    'default', creating it if absent -- same as the CLI."""
    conn = get_connection(gardens_repo.resolve_garden(garden))
    try:
        conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        initialize_database(conn)
        yield conn
    finally:
        conn.close()


# ---- gardens ----


def list_gardens() -> list[str]:
    """Names of every garden (separate, self-contained graph) on this
    machine. Call this first when unsure which garden holds the data."""
    return gardens_repo.list_gardens()


def create_garden(name: str) -> dict[str, Any]:
    """Create a new empty garden. Names allow 1-64 letters, digits, '_'
    or '-'. Fails if one by that name already exists."""
    return {"garden": gardens_repo.create_garden(name)}


def export_garden(garden: str | None = None) -> dict[str, Any]:
    """Every mote and relationship in one JSON document, in MonOSS's
    import/export format. This is the whole garden, so prefer
    neighborhood() for exploring and use this for backups or bulk
    analysis."""
    with _connect(garden) as conn:
        return garden_io.export_garden(conn)


# ---- motes ----


def list_motes(garden: str | None = None) -> list[dict[str, Any]]:
    """Every mote in the garden with its properties. Can be large --
    neighborhood() is usually the better way to explore."""
    with _connect(garden) as conn:
        return [dataclasses.asdict(m) for m in motes_repo.list_motes(conn)]


def search_motes(
    query: str, limit: int = 60, garden: str | None = None
) -> list[dict[str, Any]]:
    """Find motes by text instead of scanning list_motes() yourself.
    Case-insensitive substring match against every mote's id and its
    properties' keys and values. "key = value" (e.g. "status = open")
    matches that property's value exactly instead of as a substring;
    "key =" with nothing after the '=' matches any mote that has that
    key at all. An empty query matches everything, capped by `limit`."""
    with _connect(garden) as conn:
        return [dataclasses.asdict(m) for m in motes_repo.search_motes(conn, query, limit)]


def get_mote(mote_id: str, garden: str | None = None) -> dict[str, Any] | None:
    """One mote's id and properties, or null if no mote has that id."""
    with _connect(garden) as conn:
        mote = motes_repo.get_mote(conn, mote_id)
        return dataclasses.asdict(mote) if mote is not None else None


def create_mote(properties: dict[str, Any], garden: str | None = None) -> dict[str, Any]:
    """Create a mote with the given properties and return it, including
    its generated id. Property values keep the JSON type you send: 21 is
    stored as a number, "21" as a string. `name` is conventional but not
    required. Some keys are reserved and carry behavior -- see
    reserved_names()."""
    with _connect(garden) as conn:
        mote_id = motes_repo.create_mote(conn, properties)
        return dataclasses.asdict(motes_repo.get_mote(conn, mote_id))


def update_mote(
    mote_id: str, properties: dict[str, Any], garden: str | None = None
) -> dict[str, Any]:
    """Set or overwrite the given properties on a mote and return the
    whole updated mote. Keys not mentioned are left alone -- use
    delete_property() to remove one."""
    with _connect(garden) as conn:
        motes_repo.update_mote(conn, mote_id, properties)
        return dataclasses.asdict(motes_repo.get_mote(conn, mote_id))


def delete_property(mote_id: str, key: str, garden: str | None = None) -> bool:
    """Remove a single property from a mote. Returns false if the mote
    had no such key."""
    with _connect(garden) as conn:
        return motes_repo.delete_property(conn, mote_id, key)


def delete_mote(mote_id: str, garden: str | None = None) -> bool:
    """Delete a mote and every relationship touching it. Returns false if
    no mote had that id. This is not undoable."""
    with _connect(garden) as conn:
        return motes_repo.delete_mote(conn, mote_id)


# ---- relationships ----


def list_relationships(
    from_id: str | None = None,
    type: str | None = None,
    garden: str | None = None,
) -> list[dict[str, Any]]:
    """Relationships in the garden, optionally filtered by source mote
    and/or type. Omit both filters for all of them."""
    with _connect(garden) as conn:
        rels = rel_repo.list_relationships(conn, from_id, type)
        return [dataclasses.asdict(r) for r in rels]


def mote_relationships(mote_id: str, garden: str | None = None) -> list[dict[str, Any]]:
    """Every relationship where this mote is either endpoint -- incoming
    as well as outgoing, which list_relationships(from_id=...) won't
    give you."""
    with _connect(garden) as conn:
        rels = graph_repo.relationships_touching(conn, mote_id)
        return [dataclasses.asdict(r) for r in rels]


def create_relationship(
    from_id: str, to_id: str, type: str, garden: str | None = None
) -> dict[str, Any]:
    """Connect two motes with a typed, directed relationship. The type is
    free-form (e.g. 'wrote', 'lives_in'), except for the reserved ones
    the engine interprets -- see reserved_names(), and prefer
    set_species()/set_template()/set_folder() over creating those by
    hand."""
    with _connect(garden) as conn:
        rel_id = rel_repo.create_relationship(conn, from_id, to_id, type)
        return dataclasses.asdict(rel_repo.get_relationship(conn, rel_id))


def update_relationship(
    rel_id: str,
    from_id: str | None = None,
    to_id: str | None = None,
    type: str | None = None,
    garden: str | None = None,
) -> dict[str, Any] | None:
    """Repoint or retype an existing relationship. Supply at least one of
    from_id, to_id, type; omitted fields are left alone."""
    with _connect(garden) as conn:
        rel_repo.update_relationship(conn, rel_id, from_id, to_id, type)
        rel = rel_repo.get_relationship(conn, rel_id)
        return dataclasses.asdict(rel) if rel is not None else None


def delete_relationship(rel_id: str, garden: str | None = None) -> bool:
    """Delete one relationship, leaving both motes in place. Returns
    false if no relationship had that id."""
    with _connect(garden) as conn:
        return rel_repo.delete_relationship(conn, rel_id)


# ---- species ----


def list_species(garden: str | None = None) -> list[dict[str, Any]]:
    """Every mote currently acting as a species -- i.e. the target of at
    least one instance_of or template_of relationship. Species are how
    MonOSS expresses "is-a"; nothing else marks them structurally."""
    with _connect(garden) as conn:
        return [dataclasses.asdict(m) for m in species_repo.list_species(conn)]


def species_of(mote_id: str, garden: str | None = None) -> list[dict[str, Any]]:
    """The species this mote is declared an instance of. A mote may
    belong to more than one."""
    with _connect(garden) as conn:
        return [dataclasses.asdict(m) for m in species_repo.species_of(conn, mote_id)]


def instances_of(species_id: str, garden: str | None = None) -> list[dict[str, Any]]:
    """Every mote declared an instance of this species. Excludes the
    species' template, which holds placeholder values rather than real
    data."""
    with _connect(garden) as conn:
        return [dataclasses.asdict(m) for m in species_repo.instances_of(conn, species_id)]


def set_species(mote_id: str, species_id: str, garden: str | None = None) -> dict[str, Any]:
    """Declare an existing mote an instance of a species. Additive -- it
    does not replace species the mote already belongs to."""
    with _connect(garden) as conn:
        rel_id = species_repo.set_species(conn, mote_id, species_id)
        return dataclasses.asdict(rel_repo.get_relationship(conn, rel_id))


def set_template(mote_id: str, species_id: str, garden: str | None = None) -> dict[str, Any]:
    """Make a mote the template for a species -- the mote whose
    properties new instances start from. A template is not itself an
    instance and never appears among a species' instances."""
    with _connect(garden) as conn:
        rel_id = species_repo.set_template(conn, mote_id, species_id)
        return dataclasses.asdict(rel_repo.get_relationship(conn, rel_id))


def template_for(species_id: str, garden: str | None = None) -> dict[str, Any] | None:
    """This species' template mote, or null if it has none."""
    with _connect(garden) as conn:
        template = species_repo.template_for(conn, species_id)
        return dataclasses.asdict(template) if template is not None else None


def create_instance(
    species_id: str, overrides: dict[str, Any] | None = None, garden: str | None = None
) -> dict[str, Any]:
    """Create a new mote as an instance of a species, seeded from the
    species' template if it has one, with `overrides` applied on top.
    Without a template this is just a bare mote plus an instance_of
    relationship."""
    with _connect(garden) as conn:
        mote_id = species_repo.create_instance(conn, species_id, overrides or {})
        return dataclasses.asdict(motes_repo.get_mote(conn, mote_id))


# ---- folders ----


def list_folders(garden: str | None = None) -> list[dict[str, Any]]:
    """Every mote currently acting as a folder -- the target of at least
    one in_folder relationship. Folders are purely organizational: unlike
    a species, a folder means nothing to the simulation engine."""
    with _connect(garden) as conn:
        return [dataclasses.asdict(m) for m in folders_repo.list_folders(conn)]


def folders_of(mote_id: str, garden: str | None = None) -> list[dict[str, Any]]:
    """The folders this mote is filed under. A mote may be in more than
    one."""
    with _connect(garden) as conn:
        return [dataclasses.asdict(m) for m in folders_repo.folders_of(conn, mote_id)]


def folder_members(folder_id: str, garden: str | None = None) -> list[dict[str, Any]]:
    """Every mote filed under this folder."""
    with _connect(garden) as conn:
        return [dataclasses.asdict(m) for m in folders_repo.folder_members(conn, folder_id)]


def set_folder(mote_id: str, folder_id: str, garden: str | None = None) -> dict[str, Any]:
    """File a mote under a folder. Additive -- it does not remove folders
    the mote is already in. Any mote can act as a folder; nothing marks
    one structurally, so this is the whole of "make that a folder"."""
    with _connect(garden) as conn:
        rel_id = folders_repo.set_folder(conn, mote_id, folder_id)
        return dataclasses.asdict(rel_repo.get_relationship(conn, rel_id))


# ---- reusable motes (the library) ----


def list_library() -> list[dict[str, Any]]:
    """Every entry in the reusable-motes library -- standalone copies of
    motes worth dropping into any garden later. The library is a reserved
    garden (`_library`) rather than a separate store, but it is left out
    of list_gardens(), so this is how you find what's in it."""
    with _connect(library_repo.LIBRARY_GARDEN) as conn:
        return [dataclasses.asdict(m) for m in motes_repo.list_motes(conn)]


def save_to_library(mote_id: str, garden: str | None = None) -> dict[str, Any]:
    """Snapshot a mote's properties into a new library entry. A copy, not
    a link: editing either side afterwards never affects the other, and
    the mote's relationships are not captured."""
    with _connect(garden) as conn:
        entry_id = library_repo.save_to_library(conn, mote_id)
    with _connect(library_repo.LIBRARY_GARDEN) as library_conn:
        return dataclasses.asdict(motes_repo.get_mote(library_conn, entry_id))


def reuse_from_library(
    library_mote_id: str, overrides: dict[str, Any] | None = None, garden: str | None = None
) -> dict[str, Any]:
    """Create a new mote in `garden` from a library entry, with
    `overrides` applied on top -- the same seeding create_instance() does
    from a species' template. Nothing links the new mote back to the
    entry."""
    with _connect(garden) as conn:
        mote_id = library_repo.reuse_from_library(conn, library_mote_id, overrides or {})
        return dataclasses.asdict(motes_repo.get_mote(conn, mote_id))


# ---- portals ----


def create_portal(
    target_garden: str,
    target_mote_id: str,
    properties: dict[str, Any] | None = None,
    garden: str | None = None,
) -> dict[str, Any]:
    """Create a portal mote in `garden` (default 'default') pointing at
    target_mote_id in target_garden -- a one-way navigational reference,
    not a relationship (relationships require both ends in the same
    garden's motes table). Fails if target_garden doesn't exist or
    target_mote_id isn't in it. `properties` are set on the new mote
    alongside its pointer, same as create_instance's overrides."""
    with _connect(garden) as conn:
        mote_id = portals_repo.create_portal(conn, target_garden, target_mote_id, properties or {})
        return dataclasses.asdict(motes_repo.get_mote(conn, mote_id))


def resolve_portal(mote_id: str, garden: str | None = None) -> dict[str, Any]:
    """Follow a portal mote's target and return the live mote it points
    at, without switching anything -- MCP calls are stateless, so unlike
    the app there's no "current garden" to switch. Raises if mote_id
    isn't a portal, or if its target garden/mote no longer exists."""
    with _connect(garden) as conn:
        mote = motes_repo.get_mote(conn, mote_id)
        if mote is None:
            raise LookupError(f"No such mote: {mote_id!r}")
        target_garden, target_mote = portals_repo.resolve_portal(mote.properties)
        return {"garden": target_garden, "mote": dataclasses.asdict(target_mote)}


# ---- graph ----


def neighborhood(mote_id: str, hops: int = 2, garden: str | None = None) -> dict[str, Any]:
    """The motes and relationships within `hops` edges of a starting
    mote. Hop-counting is undirected, so a relationship pointing *into*
    the start mote still reaches its source. The usual way to explore a
    garden without pulling all of it."""
    with _connect(garden) as conn:
        motes, rels = graph_repo.neighborhood(conn, mote_id, hops)
        return {
            "motes": [dataclasses.asdict(m) for m in motes],
            "relationships": [dataclasses.asdict(r) for r in rels],
        }


def whole_garden(garden: str | None = None) -> dict[str, Any]:
    """Every mote and relationship at once. Unlike neighborhood(), this
    reaches motes with no connections, which no amount of hopping can."""
    with _connect(garden) as conn:
        motes, rels = graph_repo.whole_garden(conn)
        return {
            "motes": [dataclasses.asdict(m) for m in motes],
            "relationships": [dataclasses.asdict(r) for r in rels],
        }


# ---- simulation (read and preview only) ----


def preview_simulation(step_count: int = 1, garden: str | None = None) -> dict[str, Any]:
    """Solve the garden's rules for `step_count` steps entirely in
    memory and return what *would* change. Writes nothing and logs
    nothing. Each step starts from the previous step's solved values, so
    a rule can use `t` or `prev(x)` to express time-dependent behavior.
    Applying a run to the graph is deliberately not exposed here -- do
    that in the app."""
    with _connect(garden) as conn:
        result = simulation_repo.run_time_series(conn, step_count, commit=False)
        return {
            "steps": simulation_repo.step_payloads(result.steps),
            "failed_at": result.failed_at,
            "error": result.error,
        }


def list_runs(limit: int | None = None, garden: str | None = None) -> list[dict[str, Any]]:
    """Past simulation runs, most recent first. Includes failed runs and
    runs that were saved for review without being applied to the graph
    (`committed` tells them apart)."""
    with _connect(garden) as conn:
        runs = simulation_repo.list_runs(conn, limit=limit)
        return [dataclasses.asdict(r) for r in runs]


def get_run(run_id: str, garden: str | None = None) -> dict[str, Any] | None:
    """One logged run's status and metadata, or null if no run has that
    id."""
    with _connect(garden) as conn:
        run = simulation_repo.get_run(conn, run_id)
        return dataclasses.asdict(run) if run is not None else None


def run_changes(run_id: str, garden: str | None = None) -> list[dict[str, Any]]:
    """A logged run's net old -> new property changes. For the
    tick-by-tick breakdown of a multi-step run, use run_ticks()."""
    with _connect(garden) as conn:
        changes = simulation_repo.run_changes(conn, run_id)
        return [dataclasses.asdict(c) for c in changes]


def run_ticks(run_id: str, garden: str | None = None) -> dict[str, Any]:
    """A logged run's individual steps, oldest first, as {"steps": [...]}
    -- the same shape preview_simulation() returns, since they are the
    same steps whether freshly solved or read back out of the log. A
    steady-state run has exactly one; a run committed from an N-step
    preview has N."""
    with _connect(garden) as conn:
        return {"steps": simulation_repo.step_payloads(simulation_repo.run_ticks(conn, run_id))}


def mote_history(mote_id: str, garden: str | None = None) -> dict[str, list[float]]:
    """Every value this mote's properties have taken across the garden's
    committed run history, oldest first per key. Excludes saved-only
    runs, which were never the mote's real state."""
    with _connect(garden) as conn:
        return simulation_repo.mote_history(conn, mote_id)


# ---- notes ----


def resolve_modoc(
    mote: str, step: int = 0, garden: str | None = None
) -> dict[str, Any]:
    """Read a mote's `modoc` note and everything its macros currently
    resolve to: which mote each `\\mote{...}` points at, what each
    `\\val{...}` comes to, and each `\\graph{...}`'s series.

    `step` 0 reads the garden as stored; a higher step resolves the note
    against a simulation preview that far ahead, exactly like
    preview_simulation() and just as nondestructive -- nothing is written
    or logged either way. Writing a note is ordinary update_mote() on the
    `modoc` property; there is no separate write tool.

    `diagnostics` is what is wrong with the note (unknown commands,
    unresolvable references); a note always resolves, however broken."""
    with _connect(garden) as conn:
        source = modoc_repo.read_source(conn, mote)
        document = modoc_repo.parse(source)
        resolution = modoc_bind_repo.resolve(conn, mote, document, step=step)
        return {
            "source": source,
            "anchorId": resolution.anchor_id,
            "step": resolution.step,
            "projected": resolution.projected,
            "links": {ref: dataclasses.asdict(t) for ref, t in resolution.links.items()},
            "values": {
                node.expression if node.ref is None else f"[{node.ref}]{node.expression}":
                    dataclasses.asdict(resolution.values[modoc_repo.value_key(node)])
                for node in modoc_repo.values(document)
            },
            "graphs": [
                dataclasses.asdict(resolution.graphs[modoc_repo.graph_key(node)])
                for node in modoc_repo.graphs(document)
            ],
            "diagnostics": [
                dataclasses.asdict(d)
                for d in modoc_bind_repo.problems(document, resolution)
            ],
        }


# ---- conventions ----


def reserved_names() -> dict[str, Any]:
    """Everything the engine and UI interpret specially: reserved property
    keys and relationship types (with what each one means and expects),
    the full equation expression language usable inside a rule's
    `equation` (targets/sources, property(), prev(x), t, sum/count,
    abs/min/max), and every built-in physical-property provider with its
    operations, required inputs, and units. Call this before writing an
    equation or a property-model mote by hand -- it's the complete
    reference, not just a name check."""
    return {
        "properties": sorted(reserved_repo.RESERVED_PROPERTIES),
        "relationshipTypes": sorted(reserved_repo.RESERVED_RELATIONSHIP_TYPES),
        "propertyMeanings": reserved_repo.PROPERTY_MEANINGS,
        "relationshipTypeMeanings": reserved_repo.RELATIONSHIP_TYPE_MEANINGS,
        "equationLanguage": reserved_repo.EQUATION_LANGUAGE,
        "propertyModelProviders": reserved_repo.PROPERTY_MODEL_PROVIDERS,
    }


# Registered by build_server() below rather than the @mcp.tool() decorator,
# so the same tools can be mounted on either the plain instance (stdio,
# tests, `mcp dev`) or an OAuth-wired one (see run_mcp.py --public-url)
# without duplicating every signature and docstring.
_TOOLS: tuple[Callable[..., Any], ...] = (
    list_gardens, create_garden, export_garden,
    list_motes, search_motes, get_mote, create_mote, update_mote, delete_property, delete_mote,
    list_relationships, mote_relationships, create_relationship, update_relationship,
    delete_relationship,
    list_species, species_of, instances_of, set_species, set_template, template_for,
    create_instance,
    list_folders, folders_of, folder_members, set_folder,
    list_library, save_to_library, reuse_from_library,
    create_portal, resolve_portal,
    neighborhood, whole_garden,
    preview_simulation, list_runs, get_run, run_changes, run_ticks, mote_history,
    resolve_modoc,
    reserved_names,
)


def build_server(
    *,
    auth_server_provider: OAuthAuthorizationServerProvider[Any, Any, Any] | None = None,
    auth_settings: AuthSettings | None = None,
) -> MCPServer:
    """A fresh MCPServer with every tool registered. auth_server_provider
    and auth_settings are given together or not at all (MCPServer itself
    enforces this) -- see mcp_server/auth.py for what the OAuth path is
    and isn't."""
    server = MCPServer(
        "monoss",
        instructions=_INSTRUCTIONS,
        auth_server_provider=auth_server_provider,
        auth=auth_settings,
    )
    for tool in _TOOLS:
        server.add_tool(tool)
    return server


# The plain, unauthenticated instance: used by stdio (run_mcp.py with no
# flags), by `tests/test_mcp.py` importing tool functions directly, and by
# `uv run mcp dev mcp_server/server.py` for interactive inspection.
mcp = build_server()

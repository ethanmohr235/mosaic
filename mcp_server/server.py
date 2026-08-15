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

from core import garden_io
from core import gardens as gardens_repo
from core import graph as graph_repo
from core import motes as motes_repo
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
    "property rather than browsing."
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


def _steps_payload(steps: list) -> list[dict[str, Any]]:
    """Shapes StepResult/RunTick objects for the wire. Their values/
    unsolved fields are keyed by (mote_id, key) tuples, which don't
    survive JSON -- changes carry everything a caller needs, and unsolved
    counts stay prose-only inside each step's summary. Mirrors
    ui/api.py::_time_series_payload, which shapes the same objects for
    the JS bridge."""
    return [
        {
            "step": s.step,
            "changes": [dataclasses.asdict(c) for c in s.changes],
            "summary": s.summary,
        }
        for s in steps
    ]


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
    relationship_type: str | None = None,
    garden: str | None = None,
) -> list[dict[str, Any]]:
    """Relationships in the garden, optionally filtered by source mote
    and/or type. Omit both filters for all of them."""
    with _connect(garden) as conn:
        rels = rel_repo.list_relationships(conn, from_id, relationship_type)
        return [dataclasses.asdict(r) for r in rels]


def mote_relationships(mote_id: str, garden: str | None = None) -> list[dict[str, Any]]:
    """Every relationship where this mote is either endpoint -- incoming
    as well as outgoing, which list_relationships(from_id=...) won't
    give you."""
    with _connect(garden) as conn:
        rels = graph_repo.relationships_touching(conn, mote_id)
        return [dataclasses.asdict(r) for r in rels]


def create_relationship(
    from_id: str, to_id: str, relationship_type: str, garden: str | None = None
) -> dict[str, Any]:
    """Connect two motes with a typed, directed relationship. The type is
    free-form (e.g. 'wrote', 'lives_in'), except for the reserved ones
    the engine interprets -- see reserved_names(), and prefer
    set_species()/set_template() over creating those by hand."""
    with _connect(garden) as conn:
        rel_id = rel_repo.create_relationship(conn, from_id, to_id, relationship_type)
        return dataclasses.asdict(rel_repo.get_relationship(conn, rel_id))


def update_relationship(
    rel_id: str,
    from_id: str | None = None,
    to_id: str | None = None,
    relationship_type: str | None = None,
    garden: str | None = None,
) -> dict[str, Any] | None:
    """Repoint or retype an existing relationship. Supply at least one of
    from_id, to_id, relationship_type; omitted fields are left alone."""
    with _connect(garden) as conn:
        rel_repo.update_relationship(conn, rel_id, from_id, to_id, relationship_type)
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
            "steps": _steps_payload(result.steps),
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


def run_ticks(run_id: str, garden: str | None = None) -> list[dict[str, Any]]:
    """A logged run's individual steps, oldest first. A steady-state run
    has exactly one; a run committed from an N-step preview has N."""
    with _connect(garden) as conn:
        return _steps_payload(simulation_repo.run_ticks(conn, run_id))


def mote_history(mote_id: str, garden: str | None = None) -> dict[str, list[float]]:
    """Every value this mote's properties have taken across the garden's
    committed run history, oldest first per key. Excludes saved-only
    runs, which were never the mote's real state."""
    with _connect(garden) as conn:
        return simulation_repo.mote_history(conn, mote_id)


# ---- conventions ----


def reserved_names() -> dict[str, Any]:
    """Property keys and relationship types the engine and UI interpret
    specially (species membership, equations, links, calendar dates,
    property models). They are ordinary editable properties -- nothing in
    the schema enforces them -- but setting one turns on behavior, so
    check here before inventing a key."""
    return {
        "properties": sorted(reserved_repo.RESERVED_PROPERTIES),
        "relationshipTypes": sorted(reserved_repo.RESERVED_RELATIONSHIP_TYPES),
    }


# Registered by build_server() below rather than the @mcp.tool() decorator,
# so the same 31 tools can be mounted on either the plain instance (stdio,
# tests, `mcp dev`) or an OAuth-wired one (see run_mcp.py --public-url)
# without duplicating every signature and docstring.
_TOOLS: tuple[Callable[..., Any], ...] = (
    list_gardens, create_garden, export_garden,
    list_motes, search_motes, get_mote, create_mote, update_mote, delete_property, delete_mote,
    list_relationships, mote_relationships, create_relationship, update_relationship,
    delete_relationship,
    list_species, species_of, instances_of, set_species, set_template, template_for,
    create_instance,
    neighborhood, whole_garden,
    preview_simulation, list_runs, get_run, run_changes, run_ticks, mote_history,
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

# mcp_server/server.py
"""MCP-facing bridge over the core data layer -- a third adapter
alongside cli/ and monarch/, exposing the same repository functions as tools
an MCP client can call.

Deliberately *not* named `mcp/`: run_mcp.py puts the project root on
sys.path, so a top-level package by that name would shadow the installed
`mcp` SDK.

Unlike monarch/api.py, which holds one long-lived connection for the open
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

from core import calc as calc_repo
from core import derivations as derivations_repo
from core import garden_io
from core import health as health_repo
from core import csv_import as csv_import_repo
from core import uncertainty as uncertainty_repo
from core import projects as projects_repo
from core import scenarios as scenarios_repo
from core import gardens as gardens_repo
from core import graph as graph_repo
from core import modoc as modoc_repo
from core import modoc_bind as modoc_bind_repo
from core import motes as motes_repo
from core import relationships as rel_repo
from core import reserved as reserved_repo
from core import simulation as simulation_repo
from core import species as species_repo
from core import sync as sync_repo
from core import worksheet as worksheet_repo
from core.db import DEFAULT_GARDEN, get_connection, initialize_database

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


# ---- multi-device sync ----
#
# A garden syncs through a self-hosted sync server (sync_server/) -- pass
# its `http(s)://<token>@host:port/` URL to sync_init. See core/sync.py.
# sync_init points a garden at one; the others read/write that garden's
# own change log through it.


def sync_init(sync_dir: str, garden: str | None = None, clone: bool = False) -> dict[str, Any]:
    """Point a garden at a sync server (its http(s)://<token>@host:port/
    URL). If that target already holds sync history for this garden and
    the garden doesn't exist locally yet, pass clone=True to materialize
    it from that history -- otherwise this refuses, rather than silently
    creating an empty garden that would then diverge from history you
    clearly meant to join."""
    name = garden or gardens_repo.get_default_garden() or DEFAULT_GARDEN
    return sync_repo.init(name, sync_dir, clone=clone)


def sync_push(garden: str | None = None, resend: bool = False) -> dict[str, Any]:
    """Write this device's changes since the last push out to the sync
    target, as one new batch. Other devices pick them up on their next
    sync_pull(). `resend=True` resends this device's entire history --
    only needed when moving an already-synced garden onto a new, empty
    sync_dir."""
    with _connect(garden) as conn:
        return sync_repo.push(conn, gardens_repo.resolve_garden(garden), resend=resend)


def sync_pull(garden: str | None = None) -> dict[str, Any]:
    """Apply other devices' changes from the sync server. `skipped`
    lists anything not applied as-is and why: "shadowed" means a local
    edit was already newer (last-write-wins, per property); "delete_won"
    means the entity this change targets was already deleted locally."""
    with _connect(garden) as conn:
        return sync_repo.pull(conn, gardens_repo.resolve_garden(garden))


def sync_run(garden: str | None = None, resend: bool = False) -> dict[str, Any]:
    """sync_push() then sync_pull() -- the everyday one-shot sync."""
    with _connect(garden) as conn:
        return sync_repo.run(conn, gardens_repo.resolve_garden(garden), resend=resend)


def sync_status(garden: str | None = None) -> dict[str, Any]:
    """This garden's sync configuration and progress: the server it
    syncs through, how many local changes are still unpushed, and how
    far each other known device's changes have been applied."""
    with _connect(garden) as conn:
        return sync_repo.status(conn, gardens_repo.resolve_garden(garden))


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
    set_species()/set_template() over creating those by hand."""
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


def preview_simulation(
    step_count: int = 1,
    garden: str | None = None,
    solver: str = "linear",
    solver_configuration: dict[str, Any] | None = None,
    scenario: str | None = None,
) -> dict[str, Any]:
    """Solve the garden's rules for `step_count` steps entirely in
    memory and return what *would* change. Writes nothing, provenance
    included. Each step starts from the previous step's solved values, so
    a rule can use `t` or `prev(x)` to express time-dependent behavior.
    Applying a run to the graph is deliberately not exposed here -- do
    that in the app.

    `solver` picks the backend that finishes whatever the substitution
    loop can't resolve on its own: `"linear"` (the default) is exact but
    fails if a rule is genuinely nonlinear in its unknowns (e.g. `area ==
    side * side`); `"newton"` solves that iteratively instead, tunable
    via `solver_configuration` (`max_iterations`, `tolerance`,
    `epsilon`).

    `scenario` names a mote gathering overrides (see list_scenarios) --
    the garden is solved as that scenario would have it. A preview never
    writes either way; a scenario changes which question is being
    previewed, not whether the answer is applied.

    `constraint_violations` lists comparison relations (`efficiency <= 1`)
    the answer does not satisfy, each labelled with its step. These are
    reported, never enforced: a constraint says something about the
    answer, and an answer that breaks a limit is often exactly what you
    ran to find. A constraint depending on something the run could not
    determine is reported as *not checked* rather than passing."""
    with _connect(garden) as conn:
        result = simulation_repo.run_time_series(
            conn, step_count, solver=solver,
            solver_configuration=solver_configuration, scenario=scenario,
        )
        return {
            "steps": simulation_repo.step_payloads(result.steps),
            "failed_at": result.failed_at,
            "error": result.error,
            "constraint_violations": result.constraint_violations,
        }


# ---- notes ----


def resolve_modoc(mote: str, garden: str | None = None) -> dict[str, Any]:
    """Read a mote's `modoc` note and everything its macros currently
    resolve to: which mote each `\\mote{...}` points at, what each
    `\\val{...}` comes to, and the figure each `\\graph{...}` draws.

    A note has one reading -- the garden as it stands. It used to take a
    `step` and resolve against a simulation preview that far ahead; ask
    preview_simulation() for that instead, which is the tool for what a
    garden will be rather than what it is. Nothing here writes either
    way, and writing a note is ordinary update_mote() on the
    `modoc` property; there is no separate write tool.

    `diagnostics` is what is wrong with the note (unknown commands,
    unresolvable references); a note always resolves, however broken."""
    with _connect(garden) as conn:
        source = modoc_repo.read_source(conn, mote)
        # assemble(), not parse(): `\include` splices another note's blocks
        # in, and the numbering counts over the whole assembled document.
        document = modoc_bind_repo.assemble(conn, mote)
        resolution = modoc_bind_repo.resolve(conn, mote, document)
        return {
            "source": source,
            "anchorId": resolution.anchor_id,
            "links": {ref: dataclasses.asdict(t) for ref, t in resolution.links.items()},
            "values": {
                node.expression if node.ref is None else f"[{node.ref}]{node.expression}":
                    dataclasses.asdict(resolution.values[modoc_repo.value_key(node)])
                for node in modoc_repo.values(document)
            },
            "graphs": [
                _figure_summary(node, resolution.graphs[modoc_repo.graph_key(node)])
                for node in modoc_repo.graphs(document)
            ],
            "diagnostics": [
                dataclasses.asdict(d)
                for d in modoc_bind_repo.problems(document, resolution)
            ],
        }


def _figure_summary(node, figure) -> dict[str, Any]:
    """One `\\graph{...}`: the saved Moxie view it names, re-solved.

    Summarised rather than dumped whole. A sweep's points are worth
    sending -- there are at most core/plotview.py::NOTE_POINTS of them
    per curve, and the shape of a curve is the answer -- but a surface is
    hundreds of cells that no reader of a note is asking for, so it
    reports its extent instead of its grid."""
    summary = {
        "view": node.view,
        "name": figure.name,
        "mode": figure.mode,
        "viewId": figure.view_id,
        "x": figure.x_label,
        "y": figure.y_label,
        "error": figure.error,
    }
    if figure.mode == "surface":
        summary["grid"] = {"rows": figure.rows, "cols": figure.cols,
                           "lo": figure.lo, "hi": figure.hi}
    elif figure.mode == "correlate":
        summary["dots"] = figure.dots
    else:
        summary["series"] = [
            {"label": s.label, "unit": s.unit, "points": s.points}
            for s in figure.series
        ]
    return summary


# ---- provenance and the incidence index ----


def why(
    mote_id: str,
    key: str,
    depth: int = derivations_repo.DEFAULT_DEPTH,
    garden: str | None = None,
) -> dict[str, Any]:
    """How a solved property value was determined: the equations that
    produced it, the known values they read, and -- recursively -- how
    each of those was itself determined.

    Explains the last solve that wrote to the garden (a solve made in
    the app or the CLI), which is the one whose values are on the motes
    now; previews and scenario solves are never recorded. `method` is "substitution" when
    one equation determined the value alone, or "solver" when several
    equations determined several values *together*; in that case
    `also_determined` names the others and no finer attribution exists.
    An empty `equations` with a `note` means the last solve did not
    derive this value at all -- it was set directly, or is unchanged --
    or that no solve has been recorded yet.

    Values are in each dimension's base SI unit, which is what the solver
    worked in, not the unit a mote declares."""
    with _connect(garden) as conn:
        explanation = derivations_repo.explain(conn, mote_id, key, depth=depth)
        return dataclasses.asdict(explanation)


def _equations_payload(touches) -> list[dict[str, Any]]:
    """Equations as a client sees them. `determines`/`reads` are dropped:
    they are frozensets of tuples, the whole index rather than the answer
    to the question asked, and every caller here already knows which
    value it asked about."""
    return [
        {
            "rule_mote_id": t.rule_mote_id,
            "rule_name": t.rule_name,
            "anchor_mote_id": t.anchor_mote_id,
            "equation": t.equation,
        }
        for t in touches
    ]


def impact(
    mote_id: str, key: str, max_distance: int | None = None, garden: str | None = None
) -> dict[str, Any]:
    """What could move if this value changed, nearest first.

    A breadth-first walk of the equation graph, not a forward walk of a
    dependency tree: MonOSS solves constraint systems, so everything in a
    coupled block moves together and `distance` is hops through that
    graph rather than a causal ordering. A value nothing is free to
    determine can be a cause but never an effect, so it never appears.

    Read from the rules on every call -- no index is stored, so this is
    always current."""
    with _connect(garden) as conn:
        effects = simulation_repo.incidence_index(conn).impact(
            (mote_id, key), max_distance=max_distance
        )
        return {
            "variable": {"mote_id": mote_id, "key": key},
            "effects": [
                {
                    "mote_id": e.variable[0],
                    "key": e.variable[1],
                    "distance": e.distance,
                    "through": _equations_payload(e.through),
                }
                for e in effects
            ],
        }


def requires(mote_id: str, key: str, garden: str | None = None) -> dict[str, Any]:
    """What must be known before this value can be determined.

    `free` is whether the value is among its mote's declared freedoms --
    if it is not, nothing can determine it, which is the most common
    modelling mistake. Each entry in `needs` is "stored" (a value exists
    on the mote), "derived" (free, and some equation determines it), or
    "missing" (neither -- this is what to fix)."""
    with _connect(garden) as conn:
        requirement = simulation_repo.incidence_index(conn).requires((mote_id, key))
        return {
            "variable": {"mote_id": mote_id, "key": key},
            "free": requirement.free,
            "determinable": requirement.determinable,
            "producers": _equations_payload(requirement.producers),
            "needs": [
                {"mote_id": n.variable[0], "key": n.variable[1], "status": n.status}
                for n in requirement.needs
            ],
        }


def producers(mote_id: str, key: str, garden: str | None = None) -> list[dict[str, Any]]:
    """Equations that could determine this value. Empty when the value is
    not free to move -- use requires() to tell that case apart from a
    value nothing happens to constrain."""
    with _connect(garden) as conn:
        index = simulation_repo.incidence_index(conn)
        return _equations_payload(index.producers((mote_id, key)))


def consumers(mote_id: str, key: str, garden: str | None = None) -> list[dict[str, Any]]:
    """Equations that read this value as a known quantity."""
    with _connect(garden) as conn:
        index = simulation_repo.incidence_index(conn)
        return _equations_payload(index.consumers((mote_id, key)))


def list_scenarios(garden: str | None = None) -> list[dict[str, Any]]:
    """Named perturbations of the garden -- motes with a `varies`
    relationship to override motes, each of which shadows one mote's
    properties via `overrides`.

    Solving under a scenario answers what *would* happen and never writes
    to the graph, so a garden can be asked several questions without
    being edited between them. An override may carry `freedoms`, which
    changes the question rather than the inputs (sizing vs. rating)."""
    with _connect(garden) as conn:
        return [
            {
                "id": scenario.mote_id,
                "name": scenario.name,
                "overrides": [
                    {
                        "id": o.mote_id,
                        "target_mote_id": o.target_id,
                        "properties": o.properties,
                    }
                    for o in scenario.overrides
                ],
            }
            for scenario in scenarios_repo.list_scenarios(conn)
        ]


def check_roles(garden: str | None = None) -> dict[str, Any]:
    """Check how every assembly is wired to its declared parts.

    A species' `roles` property names the parts its instances should
    have; each part is an ordinary relationship whose type is the role's
    name. This reports roles wired to the wrong number of motes or to a
    mote of the wrong species -- and, separately, which rules are
    asserted about one mote rather than about a whole species, since that
    is inferred from what `applies_to` points at and changes the first
    time someone makes an instance of that mote."""
    with _connect(garden) as conn:
        report = simulation_repo.check_roles(conn)
        return {
            "motes_checked": report.motes_checked,
            "problems": report.problems,
            "once_scoped": report.once_scoped,
        }


def solve_health(garden: str | None = None) -> dict[str, Any]:
    """Whether this garden's model can be solved, and which values stand in
    the way -- answered **before** anything is run.

    A failed solve says "underdetermined", which is true of the whole
    garden and names nothing. This says which *coupled block* of unknowns
    is short an equation (`groups`), which free values nothing can
    determine at all (`culprits`), and which equations are dimensionally
    inconsistent (`unitProblems`). Note that `equations == unknowns` is
    neither necessary nor sufficient: read `verdict`, which is computed
    from the blocks, not from the totals."""
    with _connect(garden) as conn:
        report = health_repo.check(conn)
        return {
            "equations": report.equations,
            "unknowns": report.unknowns,
            "checks": report.checks,
            "verdict": report.verdict,
            "solvable": report.solvable,
            "summary": report.summary,
            "groups": [
                {"values": list(g.labels), "equations": list(g.equations),
                 "shortfall": g.shortfall, "message": g.message}
                for g in report.groups
            ],
            "culprits": [
                {"value": c.label, "reason": c.reason} for c in report.culprits
            ],
            "inert": [
                {"equation": i.equation, "label": i.label, "anchor": i.anchor}
                for i in report.inert
            ],
            "unitProblems": list(report.unit_problems),
        }


def _owner_json(owner: "projects_repo.Owner | None") -> dict[str, Any] | None:
    if owner is None:
        return None
    return {"garden": owner.garden, "moteId": owner.mote_id, "name": owner.name,
            "owns": owner.owns, "cover": owner.cover.to_json(),
            "properties": owner.properties}


def _node_json(node: "projects_repo.Node") -> dict[str, Any]:
    return {"garden": node.garden, "name": node.name, "loose": node.loose,
            "owner": _owner_json(node.owner),
            "children": [_node_json(child) for child in node.children]}


def project_tree() -> dict[str, Any]:
    """Every garden, nested under the mote that owns it -- what Home lists.

    A mote carrying `garden: <name>` owns that garden and is its cover;
    nesting is recursion. Roots are gardens nobody owns (hubs and loose
    gardens alike). Reads every garden, read-only. `problems` is the
    one-owner check: two owners for one garden, a claim on a garden that
    doesn't exist, or an ownership loop -- each with a ready sentence."""
    return {
        "roots": [_node_json(node) for node in projects_repo.tree()],
        "problems": [
            {"kind": p.kind, "garden": p.garden, "message": p.message,
             "owners": [_owner_json(o) for o in p.owners]}
            for p in projects_repo.check()
        ],
    }


def start_project(mote_id: str, name: str | None = None, garden: str | None = None) -> dict[str, Any]:
    """Give a mote (in `garden`) a garden of its own: creates the garden and
    writes `garden: <name>` onto the mote in one step, making the mote the
    project's cover. With no `name`, one is derived from the mote's name
    and never collides with an existing garden; a `name` given explicitly
    adopts a loose garden of that name. Refused if the mote already owns a
    garden or the named garden already has an owner."""
    with _connect(garden) as conn:
        created, _ = projects_repo.start_project(conn, mote_id, name)
        return {"garden": created, "owner": mote_id}


def propagate_uncertainty(solver: str = "linear", garden: str | None = None) -> dict[str, Any]:
    """Each solved value's ±, propagated from the givens' ± -- computed on
    read, never stored. A given carries its ± in the reserved `uncertainty`
    map ({x: 0.5} absolute in x's unit, {x: "2%"} relative). First order,
    givens independent, combined root-sum-square; each sensitivity is a
    central difference of the solve itself, so it works with every solver.
    `contributions` is each result's budget, largest `share` of variance
    first. Values and ± are in each result's declared unit (`unit` null =
    base SI, i.e. never solved -- solve first)."""
    with _connect(garden) as conn:
        report = uncertainty_repo.propagate(conn, solver)
    return {
        "results": [
            {"label": r.label, "moteId": r.mote_id, "key": r.key, "value": r.value,
             "sigma": r.sigma, "unit": r.unit,
             "contributions": [
                 {"label": c.label, "sigma": c.sigma, "sensitivity": c.sensitivity,
                  "share": c.share} for c in r.contributions]}
            for r in report.results
        ],
        "inputs": [{"label": c.label, "sigma": c.sigma} for c in report.inputs],
        "problems": list(report.problems),
    }


def import_csv(
    text: str,
    file: str,
    name: str | None = None,
    delimiter: str = ",",
    dry_run: bool = False,
    garden: str | None = None,
) -> dict[str, Any]:
    """Import measured data from CSV `text`: one mote per row, the header
    naming properties with an optional unit in brackets (`load [kN]`),
    every value marked `source: measured` with `file` as its note, and each
    row linked `imported_from` one import mote recording the file. A `name`
    column names the rows; otherwise they are `<name> 1`, `<name> 2`...
    Any problem refuses the whole file and writes nothing; `dry_run`
    reports what would be written."""
    parsed = csv_import_repo.parse(text, file, delimiter=delimiter)
    result: dict[str, Any] = {
        "columns": [{"key": c.key, "unit": c.unit} for c in parsed.columns],
        "rows": len(parsed.rows),
        "problems": list(parsed.problems),
        "imported": False,
    }
    if parsed.problems or dry_run:
        return result
    with _connect(garden) as conn:
        imported, _ = csv_import_repo.apply(conn, parsed, name)
    return {**result, "imported": True, "importId": imported.import_id,
            "rowIds": list(imported.row_ids)}


def read_worksheet(garden: str | None = None) -> str:
    """This garden's calculation as lines of text -- its givens, then its
    equations -- the form `write_worksheet` takes back.

    A projection of the graph, not a stored document: every `once`-scoped
    rule and every number those rules read. A rule about a *species* is a
    reusable model asserted once per instance and is deliberately absent."""
    with _connect(garden) as conn:
        return worksheet_repo.read(conn)


def write_worksheet(text: str, garden: str | None = None) -> dict[str, Any]:
    """Write lines of calculation into ordinary motes, rules, `applies_to`
    and freedoms. `bd.x = 80 in` is a given; `bd.l = sqrt(bd.x^2 + bd.y^2)`
    is an equation and makes `l` free on `bd`; `bd.Ax = ?` makes `Ax` free
    with no value; `bd.Ax + bd.Bx == 0` is a relation that declares no
    freedom; a trailing `! ΣFx` names the rule.

    **This replaces, and it deletes.** The lines are the whole set of this
    garden's `once`-scoped equations, so a rule whose line is absent is
    removed -- `deleted` says which. Call `read_worksheet` first and edit
    what it returns, rather than sending lines written from scratch against
    a garden you have not read. Rules about a species are never touched
    (`untouched` counts them). A line that does not parse is reported in
    `problems` and nothing is deleted while any line is broken."""
    with _connect(garden) as conn:
        applied = worksheet_repo.apply(conn, text)
        return {
            "motesCreated": list(applied.motes_created),
            "givensWritten": applied.givens_written,
            "rulesWritten": applied.rules_written,
            "deleted": list(applied.deleted),
            "untouched": applied.untouched,
            "problems": list(applied.problems),
        }


def calc_sections(
    unit: str | None = None, figures: int | None = None, garden: str | None = None
) -> dict[str, Any]:
    """A calculation's five standing sections: what was **given**, what was
    **assumed**, what came from **outside** the garden, the **answers**, and
    the **checks**.

    The same five a `.modoc` note's `\\given` / `\\assumptions` / `\\answers`
    / `\\checks` macros print, so a client can read a finished calculation
    without rendering a document. Givens and answers are scoped to the motes
    the equations actually mention; assumptions are not, since an assumption
    may be about something no equation touches. `unit` converts every
    quantity for display and `figures` quotes it to that many significant
    figures -- neither changes anything stored."""
    with _connect(garden) as conn:
        return {
            "given": [_calc_row(r, unit, figures) for r in calc_repo.givens(conn)],
            "answers": [_calc_row(r, unit, figures) for r in calc_repo.answers(conn)],
            "assumptions": [
                _calc_row(r, unit, figures) for r in calc_repo.assumptions(conn)
            ],
            "external": [_calc_row(r, unit, figures) for r in calc_repo.external(conn)],
            "checks": [
                {"equation": c.equation, "label": c.label, "status": c.status,
                 "detail": c.detail, "offBy": c.off_by}
                for c in calc_repo.checks(conn)
            ],
            "verification": [
                {"label": c.label, "moteId": c.mote_id, "key": c.key,
                 "externalKey": c.external_key,
                 "hand": c.hand.converted(unit).text(figures),
                 "external": c.external.converted(unit).text(figures),
                 "errorPercent": c.error, "note": c.note, "problem": c.problem}
                for c in calc_repo.verification(conn)
            ],
        }


def _calc_row(row, unit: str | None, figures: int | None) -> dict[str, Any]:
    shown = row.quantity.converted(unit)
    return {
        "label": row.label,
        "moteId": row.mote_id,
        "mote": row.mote,
        "key": row.key,
        "value": row.quantity.value,
        "unit": shown.unit,
        "text": shown.text(figures),
        "sigma": shown.sigma,
        "source": row.source.kind if row.source else None,
        "note": row.source.note if row.source else None,
    }


def shown_work(
    mote_id: str,
    key: str,
    unit: str | None = None,
    figures: int | None = None,
    garden: str | None = None,
) -> dict[str, Any]:
    """How one value came out: the equation, the equation with the values
    written into it, and the result.

    Read from the last solve's record -- the same record `why` reads, so
    this and `why` explain one value one way. The substituted values are in
    the unit each mote declares rather than base SI (somebody who wrote
    `80 in` has to see 80), and where one line mixes two units of a
    dimension they are converted to one and the step's `note` says so.

    A value the last solve did not determine has no work to show and says
    so in `note` -- which is an answer, not a failure. Solve first
    (`preview_simulation` never writes, so it leaves no record; the app or
    the CLI has to do the writing solve)."""
    with _connect(garden) as conn:
        shown = calc_repo.work(conn, mote_id, key, unit, figures)
        return {
            "label": shown.label,
            "text": shown.quantity.text(figures),
            "value": shown.quantity.value,
            "unit": shown.quantity.unit,
            "steps": [
                {"kind": s.kind, "text": s.text, "note": s.note} for s in shown.steps
            ],
            "alsoDetermined": list(shown.also_determined),
            "note": shown.note,
            "error": shown.error,
        }


# ---- conventions ----


def reserved_names() -> dict[str, Any]:
    """Everything the engine and UI interpret specially: reserved property
    keys and relationship types (with what each one means and expects),
    the **value shapes** recognized under any key at all (`valueShapes` --
    notably how to write when something happens, which has no reserved
    name), the full equation expression language usable inside a rule's
    `equation` (targets/sources, property(), prev(x), t, sum/count,
    abs/min/max), and every built-in physical-property provider with its
    operations, required inputs, and units. Call this before writing a
    date, an equation or a property-model mote by hand -- it's the
    complete reference, not just a name check."""
    return {
        "properties": sorted(reserved_repo.RESERVED_PROPERTIES),
        "relationshipTypes": sorted(reserved_repo.RESERVED_RELATIONSHIP_TYPES),
        "propertyMeanings": reserved_repo.PROPERTY_MEANINGS,
        "relationshipTypeMeanings": reserved_repo.RELATIONSHIP_TYPE_MEANINGS,
        "valueShapes": reserved_repo.VALUE_SHAPES,
        "equationLanguage": reserved_repo.EQUATION_LANGUAGE,
        "propertyModelProviders": reserved_repo.PROPERTY_MODEL_PROVIDERS,
    }


# Registered by build_server() below rather than the @mcp.tool() decorator,
# so the same tools can be mounted on either the plain instance (stdio,
# tests, `mcp dev`) or an OAuth-wired one (see run_mcp.py --public-url)
# without duplicating every signature and docstring.
_TOOLS: tuple[Callable[..., Any], ...] = (
    list_gardens, create_garden, export_garden,
    sync_init, sync_push, sync_pull, sync_run, sync_status,
    list_motes, search_motes, get_mote, create_mote, update_mote, delete_property, delete_mote,
    list_relationships, mote_relationships, create_relationship, update_relationship,
    delete_relationship,
    list_species, species_of, instances_of, set_species, set_template, template_for,
    create_instance,
    neighborhood, whole_garden,
    preview_simulation,
    why, impact, requires, producers, consumers,
    list_scenarios, check_roles,
    solve_health, read_worksheet, write_worksheet,
    project_tree, start_project, import_csv, propagate_uncertainty,
    calc_sections, shown_work,
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

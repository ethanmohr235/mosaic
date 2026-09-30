# monolith/payload.py
"""What is left of Monolith's payload once the table moved into core/.

Column discovery, the schema, the default query, a query's rows and the
one-line summary are `core/table.py`'s now -- the console's `find` table,
the CLI and an MCP tool all have to agree with each other about what
columns a garden has, and `docs/architecture.md`'s charter is explicit
that an adapter holding logic of its own is a bug. They are re-exported
here so this window keeps working unchanged while it is being ported into
the console (`docs/console.md`, step U2); there is one implementation, in
core, and this file adds nothing to it.

What genuinely is this window's own stays: the relationship matrix (an
n x n grid nothing else draws), the row detail panel, and the view rail's
row shape.
"""
from core import query as query_repo
from core import table as table_repo
from core.entities import Mote, Relationship
from core.query import Index, Query
from core.table import (  # noqa: F401 -- the window's imports, one implementation
    DEFAULT_PROPERTY_COLUMNS,
    DISTINCT_LIMIT,
    BUILTIN_LABELS,
    EDITABLE_BUILTINS,
    GRAPH_LABELS,
    Column,
    Schema,
    column_key,
    default_query,
    describe,
    guess_kind,
    kind_of,
    label_for,
    summarize,
)
from core.views import name_of, query_of

# How many rows the relationship matrix covers. It is n x n cells at 22px
# -- past this it is neither drawable nor readable, and the panel says so
# rather than silently showing a corner of the truth.
MATRIX_ROWS = 24


def table_payload(
    index: Index,
    schema: Schema,
    the_query: Query,
    motes: list[Mote],
    relationships: list[Relationship],
) -> dict:
    """One query's worth of answer, plus the matrix over the first rows.

    The answer itself is `core/table.py::answer`; this adds the one part of
    it that is this window's own."""
    resolved, _problems = table_repo.resolve_query(the_query, schema)
    rows = query_repo.evaluate(index, motes, resolved)
    return {
        **table_repo.answer(index, schema, the_query, motes),
        "matrix": matrix_payload(index, rows, relationships),
    }


def matrix_payload(index: Index, rows: list[Mote], relationships: list[Relationship]) -> dict:
    """The relationship matrix: the visible rows on both axes, and every
    edge that runs between two of them. Capped, and honest about the cap
    -- a matrix showing 24 of 300 rows must say which 24."""
    shown = rows[:MATRIX_ROWS]
    position = {mote.id: i for i, mote in enumerate(shown)}
    edges = [
        {"from": position[r.from_id], "to": position[r.to_id], "type": r.type, "id": r.id}
        for r in relationships
        if r.from_id in position and r.to_id in position
    ]
    return {
        "motes": [describe(index, mote.id) for mote in shown],
        "edges": edges,
        "shown": len(shown),
        "rows": len(rows),
        "types": sorted({r.type for r in relationships}),
    }


# ---- saved views ----


def view_payload(mote: Mote, index: Index) -> dict:
    parsed = query_of(mote)
    return {**describe(index, mote.id), "query": parsed.to_json(),
            "summary": summarize(index, parsed), "viewName": name_of(mote)}


def panel_payload(mote: Mote, index: Index, relationships: list[Relationship]) -> dict:
    """One mote in full -- its properties and every relationship touching
    it -- for the row detail the table opens beside itself."""
    touching = [r for r in relationships if mote.id in (r.from_id, r.to_id)]
    return {
        **describe(index, mote.id),
        "properties": mote.properties,
        "relationships": [_relationship(r, mote.id, index) for r in touching],
    }


def _relationship(relationship: Relationship, mote_id: str, index: Index) -> dict:
    outgoing = relationship.from_id == mote_id
    other = relationship.to_id if outgoing else relationship.from_id
    return {
        "id": relationship.id,
        "type": relationship.type,
        "outgoing": outgoing,
        "other": describe(index, other),
    }

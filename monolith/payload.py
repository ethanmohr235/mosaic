# monolith/payload.py
"""A garden shaped as a table, for the JS bridge.

The mockup Monolith was designed against declares its columns -- status,
owner, priority. A garden declares nothing: motes carry free-form JSON,
and two motes side by side need not share a single key. So the columns
are *discovered* here, once per request, from what the garden actually
contains: every property key anyone has used, every relationship type
anyone has drawn, plus the handful of things every mote has (name, id,
species, degree, last-updated).

That discovery is the whole reason this module exists rather than the
frontend filtering `whole_garden()` itself. It also pays for the cell
editor: a column's distinct values *are* its enum, so clicking a `status`
cell offers todo/doing/blocked without anyone having declared a status
type, and offers nothing but a text field for a column where every value
differs.

Filtering, ordering and grouping are core/query.py's, not this module's.
This shapes its answers into JSON-able dicts and decides how a value
reads in a cell -- nothing here re-implements a predicate.
"""
from dataclasses import dataclass

from core import query
from core import reserved
from core.entities import Mote, Relationship
from core.query import Field, Index, Query

# How many distinct values a column offers as options before the editor
# gives up and shows a plain text field. A column with 200 distinct
# values is not an enum, and a 200-row popover is not a picker.
DISTINCT_LIMIT = 40

# How many rows the relationship matrix covers. It is n x n cells at 22px
# -- past this it is neither drawable nor readable, and the panel says so
# rather than silently showing a corner of the truth.
MATRIX_ROWS = 24

_DATE_LENGTHS = (10, 19, 20, 24, 25, 26, 29, 32)


def describe(index: Index, mote_id: str | None) -> dict:
    """Everything a payload says about a mote beyond its cells: its name,
    the species that colours it, and its kinds. The same three fields
    mora/payload.py sends, so a mote reads the
    same colour in every window."""
    if not mote_id or mote_id not in index.motes:
        return {"id": mote_id, "name": mote_id or "", "speciesId": None,
                "speciesName": None, "kinds": []}
    species = index.species_ids(mote_id)
    return {
        "id": mote_id,
        "name": index.name_of(mote_id),
        "speciesId": species[0] if species else None,
        "speciesName": index.name_of(species[0]) if species else None,
        "kinds": list(index.kinds.get(mote_id, [])),
    }


# ---- column discovery ----


@dataclass(frozen=True)
class Column:
    """One column of the table: what it asks, what its answers look like,
    and how many motes actually have one."""
    field: Field
    label: str
    kind: str
    fill: int
    editable: bool
    values: tuple[str, ...] = ()
    reserved: bool = False

    def to_json(self) -> dict:
        return {
            "field": self.field.to_json(),
            "key": column_key(self.field),
            "label": self.label,
            "kind": self.kind,
            "fill": self.fill,
            "editable": self.editable,
            "values": list(self.values),
            "reserved": self.reserved,
            "ops": list(query.OPS[self.kind]),
        }


def column_key(field: Field) -> str:
    """A column's identity in one string, so the frontend can key a list
    on it and localStorage can remember which columns are on."""
    if field.source == query.REL:
        return f"{query.REL}:{field.direction}:{field.key}"
    return f"{field.source}:{field.key}"


BUILTIN_LABELS = {
    query.BUILTIN_NAME: "Name",
    query.BUILTIN_ID: "ID",
    query.BUILTIN_SPECIES: "Species",
    query.BUILTIN_DEGREE: "Links",
    query.BUILTIN_UPDATED: "Updated",
}
GRAPH_LABELS = {
    query.GRAPH_INSTANCE_OF: "Instance of",
    query.GRAPH_KIND: "Kinds",
    query.GRAPH_RELATED: "Related by",
}
# Only `name` is a property under the hood, so only `name` can be typed
# into. The rest are the graph's own answers -- a degree is edited by
# drawing an edge, not by typing 4 into a cell.
EDITABLE_BUILTINS = {query.BUILTIN_NAME}


@dataclass(frozen=True)
class Schema:
    """Every column this garden could show, in the order the rail lists
    them: the builtins first, then property keys by how many motes carry
    them, then the graph's own columns, then one column per relationship
    type per direction."""
    columns: tuple[Column, ...]

    @staticmethod
    def of(index: Index, motes: list[Mote], relationships: list[Relationship]) -> "Schema":
        return Schema((
            *_builtin_columns(index, motes),
            *_property_columns(motes),
            *_graph_columns(index, motes),
            *_relationship_columns(relationships),
        ))

    def column(self, field: Field) -> Column:
        """The named column, or a plausible one invented on the spot -- a
        saved view may name a property key that no mote carries any more,
        and it should come back as an empty column rather than as a
        KeyError in the window opening it."""
        key = column_key(field)
        for column in self.columns:
            if column_key(column.field) == key:
                return column
        return Column(field, label_for(field), kind_of(field, ()), 0, field.source == query.PROPERTY)

    def to_json(self) -> dict:
        return {"columns": [c.to_json() for c in self.columns], "ops": {k: list(v) for k, v in query.OPS.items()}}


def label_for(field: Field) -> str:
    if field.source == query.BUILTIN:
        return BUILTIN_LABELS.get(field.key, field.key)
    if field.source == query.GRAPH:
        return GRAPH_LABELS.get(field.key, field.key)
    if field.source == query.REL:
        arrow = "→" if field.direction == query.OUT else "←"
        return f"{arrow} {field.key}"
    return field.key


def kind_of(field: Field, values: tuple) -> str:
    return field.kind or guess_kind(values)


def guess_kind(values: tuple) -> str:
    """What a property column holds, decided by looking. Nothing declares
    it, which is the reason `estimate > 3` can mean what it says on a
    garden where nobody ever said estimate was a number."""
    present = [v for v in values if v is not None and v != ""]
    if not present:
        return query.TEXT
    if all(isinstance(v, bool) for v in present):
        return query.BOOL
    if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in present):
        return query.NUMBER
    if all(_looks_like_a_date(v) for v in present):
        return query.DATE
    return query.TEXT


def _looks_like_a_date(value) -> bool:
    """ISO 8601 by shape, the way core/calendar.py treats dates: a string
    starting yyyy-mm-dd. Enough to earn before/after, which compare as
    strings and are correct for exactly this shape."""
    if not isinstance(value, str) or len(value) not in _DATE_LENGTHS:
        return False
    head = value[:10]
    return len(head) == 10 and head[4] == "-" and head[7] == "-" and \
        head[:4].isdigit() and head[5:7].isdigit() and head[8:10].isdigit()


def _builtin_columns(index: Index, motes: list[Mote]) -> list[Column]:
    return [
        Column(
            Field(query.BUILTIN, key),
            BUILTIN_LABELS[key],
            query.NUMBER if key == query.BUILTIN_DEGREE else
            query.DATE if key == query.BUILTIN_UPDATED else query.TEXT,
            fill=len(motes),
            editable=key in EDITABLE_BUILTINS,
        )
        for key in query.BUILTINS
    ]


def _property_columns(motes: list[Mote]) -> list[Column]:
    """Every key anyone has used, commonest first -- a garden's real
    schema, read off the data. Reserved keys are flagged rather than
    hidden: `equation` is a column worth seeing, and worth knowing is not
    ordinary user data before you bulk-edit it."""
    values: dict[str, list] = {}
    for mote in motes:
        for key, value in mote.properties.items():
            values.setdefault(key, []).append(value)
    columns = []
    for key, found in values.items():
        distinct = _distinct(found)
        columns.append(Column(
            Field(query.PROPERTY, key),
            key,
            guess_kind(tuple(found)),
            fill=len(found),
            editable=True,
            values=distinct,
            reserved=key in reserved.RESERVED_PROPERTIES,
        ))
    columns.sort(key=lambda c: (-c.fill, c.label.casefold()))
    return columns


def _distinct(values: list) -> tuple[str, ...]:
    """A column's options, if it has few enough to be an enum. Ordered by
    how often each is used, so the popover opens on the common answers."""
    counts: dict[str, int] = {}
    for value in values:
        if value is None or value == "" or isinstance(value, (dict, list)):
            continue
        text = query.text_of_scalar(value)
        counts[text] = counts.get(text, 0) + 1
    if len(counts) > DISTINCT_LIMIT:
        return ()
    return tuple(sorted(counts, key=lambda t: (-counts[t], t.casefold())))


def _graph_columns(index: Index, motes: list[Mote]) -> list[Column]:
    return [
        Column(
            Field(query.GRAPH, key),
            GRAPH_LABELS[key],
            query.LIST,
            fill=sum(1 for m in motes if query.value_of(index, m, Field(query.GRAPH, key))),
            editable=False,
        )
        for key in query.GRAPH_FIELDS
    ]


def _relationship_columns(relationships: list[Relationship]) -> list[Column]:
    """One column per relationship type per direction. Editable, which is
    the point: setting `→ depends_on` on a cell writes relationships, and
    that is the join a table over a graph is for."""
    counts: dict[tuple[str, str], int] = {}
    for relationship in relationships:
        counts[(relationship.type, query.OUT)] = counts.get((relationship.type, query.OUT), 0) + 1
        counts[(relationship.type, query.IN)] = counts.get((relationship.type, query.IN), 0) + 1
    columns = [
        Column(Field(query.REL, type_, direction), label_for(Field(query.REL, type_, direction)),
               query.LIST, fill=count, editable=True,
               reserved=type_ in reserved.RESERVED_RELATIONSHIP_TYPES)
        for (type_, direction), count in counts.items()
    ]
    columns.sort(key=lambda c: (c.field.key.casefold(), c.field.direction != query.OUT))
    return columns


# How many discovered property columns a garden nobody has configured
# opens with. Enough to be a table rather than a list; few enough that a
# garden with 80 keys does not open as a horizontal scrollbar.
DEFAULT_PROPERTY_COLUMNS = 4


def default_query(schema: Schema) -> Query:
    """What Monolith shows on a garden it has never been opened on:
    name, the commonest few property keys, and when each mote last
    changed, newest first. Discovered rather than configured, which is
    the only kind of default a schemaless store can have."""
    properties = [
        c.field for c in schema.columns
        if c.field.source == query.PROPERTY and not c.reserved
        and c.field.key != query.NAME_KEY
    ][:DEFAULT_PROPERTY_COLUMNS]
    columns = (
        Field(query.BUILTIN, query.BUILTIN_NAME),
        Field(query.BUILTIN, query.BUILTIN_SPECIES),
        *properties,
        Field(query.BUILTIN, query.BUILTIN_DEGREE),
        Field(query.BUILTIN, query.BUILTIN_UPDATED),
    )
    return Query(
        sort=query.Sort(Field(query.BUILTIN, query.BUILTIN_UPDATED), query.DESC),
        columns=columns,
    )


# ---- the table ----


def table_payload(
    index: Index,
    schema: Schema,
    the_query: Query,
    motes: list[Mote],
    relationships: list[Relationship],
) -> dict:
    """One query's worth of answer: the visible columns, the rows cut
    into groups, the footer counts, and the matrix over the first rows."""
    columns = [schema.column(f) for f in the_query.columns] or [schema.columns[0]]
    rows = query.evaluate(index, motes, the_query)
    groups = query.group_rows(index, rows, the_query.group)
    return {
        "columns": [c.to_json() for c in columns],
        "groups": [_group(index, group, columns, the_query.group is not None) for group in groups],
        "matched": len(rows),
        "total": len(motes),
        "matrix": matrix_payload(index, rows, relationships),
    }


def _group(index: Index, group: query.Group, columns: list[Column], labelled: bool) -> dict:
    return {
        "key": group.key,
        "label": group.label,
        "labelled": labelled,
        "count": len(group.motes),
        "rows": [_row(index, mote, columns) for mote in group.motes],
    }


def _row(index: Index, mote: Mote, columns: list[Column]) -> dict:
    return {**describe(index, mote.id), "cells": [_cell(index, mote, c) for c in columns]}


def _cell(index: Index, mote: Mote, column: Column) -> dict:
    """A cell carries the text it draws *and* the raw value behind it, so
    the editor opens on what is stored rather than on what was rendered
    -- a date cell reading "Aug 25" still edits 2026-08-25."""
    value = query.value_of(index, mote, column.field)
    empty = value is None or value == "" or value == []
    return {
        "key": column_key(column.field),
        "text": query.text_of(index, value),
        "value": None if empty else value,
        "kind": column.kind,
        "editable": column.editable,
        "empty": empty,
        "ids": list(value) if isinstance(value, list) else [],
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
    raw = mote.properties.get(query.QUERY_KEY)
    parsed = Query.from_json(raw if isinstance(raw, dict) else None)
    return {**describe(index, mote.id), "query": parsed.to_json(),
            "summary": summarize(index, parsed)}


def summarize(index: Index, the_query: Query) -> str:
    """The one-line description under a view's name in the rail, and the
    subtitle in the topbar. "no filter" rather than an empty string: a
    view that filters nothing is a deliberate thing to have saved."""
    if not the_query.conditions:
        return "no filter"
    join = f" {the_query.join.upper()} "
    return join.join(_condition_text(index, c) for c in the_query.conditions)


def _condition_text(index: Index, condition: query.Condition) -> str:
    label = label_for(condition.field)
    if condition.op in query.EMPTY_OPS:
        return f"{label} {condition.op}"
    value = condition.value
    if value in index.motes:
        value = index.name_of(value)
    return f"{label} {condition.op} {value or '…'}"


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

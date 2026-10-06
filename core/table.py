# core/table.py
"""A garden shaped as a table: which columns it has, and one query's rows.

The mockup the table was designed against declares its columns -- status,
owner, priority. A garden declares nothing: motes carry free-form JSON,
and two motes side by side need not share a single key. So the columns are
**discovered**, once per request, from what the garden actually contains:
every property key anyone has used, every relationship type anyone has
drawn, plus the handful of things every mote has (name, id, species,
degree, last-updated).

That discovery also pays for the cell editor. A column's distinct values
*are* its enum, so a `status` cell offers todo/doing/blocked on a garden
where nobody declared a status type, and offers a plain text field on a
column where every value differs.

This lived in `monolith/payload.py`, where it was the one window's own
answer. It is in `core/` because a discovered schema is not presentation:
the console's `find` table, the CLI and an MCP tool all have to agree with
each other about what columns a garden has and what kind each one holds,
and `docs/architecture.md`'s charter is explicit that an adapter holding
logic of its own is a bug.

Filtering, ordering and grouping stay `core/query.py`'s; nothing here
re-implements a predicate. What this module adds on top is the one step
that genuinely needs a schema: `resolve_query()` decides each condition's
operator from the column's *discovered* kind, and resolves a relative date
(`7d`, `today`) against the clock. Both happen at evaluate time, not when
a query is written down, which is why a saved view never freezes -- `due:
7d` still means "the next week" a month from now.
"""
import sqlite3
from dataclasses import dataclass, replace
from datetime import date

from dateutil.relativedelta import relativedelta

from core import graph as graph_repo
from core import query
from core import reserved
from core import species as species_repo
from core.entities import Mote, Relationship
from core.query import Condition, Field, Index, Query

# How many distinct values a column offers as options before the editor
# gives up and shows a plain text field. A column with 200 distinct values
# is not an enum, and a 200-row popover is not a picker.
DISTINCT_LIMIT = 40

_DATE_LENGTHS = (10, 19, 20, 24, 25, 26, 29, 32)

# A bare `yyyy-mm-dd`, which is what makes prefix comparison correct.
DATE_ONLY_LENGTH = 10


def updated_at(conn: sqlite3.Connection) -> dict[str, str]:
    """When each mote last changed, from the timestamp core/motes.py
    already writes on every property -- feeding the `updated` builtin.

    One grouped query rather than one per row: this fills a column, and a
    column is every mote."""
    rows = conn.execute(
        "SELECT mote_id, MAX(updated_at) AS updated FROM mote_properties GROUP BY mote_id"
    ).fetchall()
    return {row["mote_id"]: row["updated"] for row in rows if row["updated"]}


def read(conn: sqlite3.Connection) -> tuple[list[Mote], list[Relationship], Index]:
    """One consistent read of the whole garden, indexed. Every caller here
    re-reads rather than caching: a table showing a stale row after
    somebody else's edit is worse than a read costing a few milliseconds,
    and the columns are discovered from the same pass that fills them."""
    motes, relationships = graph_repo.whole_garden(conn)
    return motes, relationships, Index.of(motes, relationships, updated_at(conn))


def describe(index: Index, mote_id: str | None) -> dict:
    """Everything a row says about a mote beyond its cells: its name, the
    species that colours it, and its kinds. The same three fields every
    other window's payload sends, so a mote reads the same colour
    everywhere."""
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
    """A column's identity in one string, so a caller can key a list on it
    and remember which columns are on."""
    if field.source == query.REL:
        return f"{query.REL}:{field.direction}:{field.key}"
    return f"{field.source}:{field.key}"


def field_of(key: str) -> Field:
    """`column_key` read back: what a surface hands in when a gesture names
    a column it was sent (a header click sorts by `property:due`). Raises
    ValueError for a key `column_key` could not have written."""
    source, _, rest = (key or "").partition(":")
    if source == query.REL:
        direction, _, name = rest.partition(":")
        if direction in (query.OUT, query.IN) and name:
            return Field(query.REL, name, direction)
    elif source in (query.PROPERTY, query.BUILTIN, query.GRAPH) and rest:
        return Field(source, rest)
    raise ValueError(f"not a column key: {key!r}")


BUILTIN_LABELS = {
    query.BUILTIN_NAME: "Name",
    query.BUILTIN_ID: "ID",
    query.BUILTIN_SPECIES: "Species",
    query.BUILTIN_DEGREE: "Links",
    query.BUILTIN_UPDATED: "Updated",
}
GRAPH_LABELS = {
    query.GRAPH_INSTANCE_OF: "Species",
    query.GRAPH_KIND: "Kinds",
    query.GRAPH_RELATED: "Related by",
}
UPDATED_FIELD = Field(query.BUILTIN, query.BUILTIN_UPDATED)

# Only `name` is a property under the hood, so only `name` can be typed
# into. The rest are the graph's own answers -- a degree is edited by
# drawing an edge, not by typing 4 into a cell.
EDITABLE_BUILTINS = {query.BUILTIN_NAME}


@dataclass(frozen=True)
class Schema:
    """Every column this garden could show, in the order a rail lists
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
        KeyError in whatever opened it."""
        key = column_key(field)
        for column in self.columns:
            if column_key(column.field) == key:
                return column
        return Column(field, label_for(field), kind_of(field, ()), 0,
                      field.source == query.PROPERTY)

    def known(self, field: Field) -> bool:
        """Whether this garden actually has that column. Feeds the
        problem report rather than any refusal: a view naming a key
        nobody carries any more is worth saying out loud."""
        key = column_key(field)
        return any(column_key(c.field) == key for c in self.columns)

    def to_json(self) -> dict:
        return {
            "columns": [c.to_json() for c in self.columns],
            "ops": {k: list(v) for k, v in query.OPS.items()},
        }


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
    if all(looks_like_a_date(v) for v in present):
        return query.DATE
    return query.TEXT


def looks_like_a_date(value) -> bool:
    """ISO 8601 by shape, the way core/calendar.py treats dates: a string
    starting yyyy-mm-dd. Enough to earn before/after, which compare as
    strings and are correct for exactly this shape."""
    if not isinstance(value, str) or len(value) not in _DATE_LENGTHS:
        return False
    return _is_date_prefix(value[:DATE_ONLY_LENGTH])


def _is_date_prefix(head: str) -> bool:
    return (
        len(head) == DATE_ONLY_LENGTH
        and head[4] == "-" and head[7] == "-"
        and head[:4].isdigit() and head[5:7].isdigit() and head[8:10].isdigit()
    )


def _builtin_columns(index: Index, motes: list[Mote]) -> list[Column]:
    return [
        Column(
            Field(query.BUILTIN, key),
            BUILTIN_LABELS[key],
            Field(query.BUILTIN, key).kind,
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
        columns.append(Column(
            Field(query.PROPERTY, key),
            key,
            guess_kind(tuple(found)),
            fill=len(found),
            editable=True,
            values=_distinct(found),
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
        for direction in (query.OUT, query.IN):
            counts[(relationship.type, direction)] = \
                counts.get((relationship.type, direction), 0) + 1
    columns = [
        Column(Field(query.REL, type_, direction),
               label_for(Field(query.REL, type_, direction)),
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
    """What a table shows on a garden it has never been opened on: name,
    the commonest few property keys, and when each mote last changed,
    newest first. Discovered rather than configured, which is the only
    kind of default a schemaless store can have."""
    properties = [
        c.field for c in schema.columns
        if c.field.source == query.PROPERTY and not c.reserved
        and c.field.key != query.NAME_KEY
    ][:DEFAULT_PROPERTY_COLUMNS]
    columns = (
        Field(query.BUILTIN, query.BUILTIN_NAME),
        # `instance_of`, not the `species` builtin: every column a default
        # table shows has to have a written form, since a header click
        # rewrites the pane's `find` line, and `species` is spelled `type`
        # there (core/findtext.py). It sorts and groups by species name.
        Field(query.GRAPH, query.GRAPH_INSTANCE_OF),
        *properties,
        Field(query.BUILTIN, query.BUILTIN_DEGREE),
        Field(query.BUILTIN, query.BUILTIN_UPDATED),
    )
    return Query(
        sort=query.Sort(Field(query.BUILTIN, query.BUILTIN_UPDATED), query.DESC),
        columns=columns,
    )


# ---- resolving a query against the schema it will run on ----
#
# The two things that cannot be decided when a query is written down, only
# when it is run: which operator a condition means on a column whose kind
# was discovered, and what date a relative one like `7d` names. Doing both
# here is what lets a saved view keep meaning what it said -- `due:7d` is
# the next week whenever it is opened, and `estimate:>3` becomes a numeric
# `>` the day estimate stops being text. The operator vocabulary itself is
# core/query.py's (`intent_of`, `op_for`).


def resolve_query(
    the_query: Query, schema: Schema, today: str | None = None
) -> tuple[Query, list[str]]:
    """`the_query` with every condition's operator decided by the column's
    discovered kind and every relative date turned into an ISO one.

    Returns the problems alongside, rather than raising: a query reaches
    this from a saved view a person may have hand-edited, and a garden
    whose `estimate` key has since gone should come back as a table with a
    note under it, not as an exception in whatever opened it."""
    problems: list[str] = []
    conditions = []
    for condition in the_query.conditions:
        resolved = _resolve_condition(condition, schema, today, problems)
        if resolved is not None:
            conditions.append(resolved)
    for field in (*the_query.columns, the_query.group,
                  the_query.sort.field if the_query.sort else None):
        if field is not None and not schema.known(field):
            problems.append(f"no {label_for(field)!r} column in this garden")
    return replace(the_query, conditions=tuple(conditions)), problems


def _resolve_condition(
    condition: Condition, schema: Schema, today: str | None, problems: list[str]
) -> Condition | None:
    column = schema.column(condition.field)
    intent = query.intent_of(condition.op)
    op = query.op_for(intent, column.kind)
    if op is None:
        problems.append(
            f"{label_for(condition.field)} holds {column.kind} values, so "
            f"{condition.op!r} says nothing about it -- the condition is ignored"
        )
        return None
    value = condition.value
    if column.kind == query.DATE and op not in query.EMPTY_OPS:
        value = resolve_date(value, today)
    return Condition(condition.field, op, value)


_UNITS = {"d": "days", "w": "weeks", "m": "months", "y": "years"}
TODAY = "today"


def resolve_date(value: str, today: str | None = None) -> str:
    """`today`, `7d`, `-3d`, `2w`, `1m` as the ISO date they name; anything
    else unchanged.

    Stored verbatim in the query and resolved here, at evaluate time, so a
    saved view never freezes: `due:7d` means the next week whenever it is
    opened, not the week it was written in. `today` is passed in rather
    than read from the clock, so a test can say what day it is."""
    text = (value or "").strip()
    if not text:
        return value
    base = today or date.today().isoformat()
    if text.casefold() == TODAY:
        return base
    unit = _UNITS.get(text[-1:].casefold())
    if unit is None:
        return value
    try:
        count = int(text[:-1])
    except ValueError:
        return value
    return (date.fromisoformat(base) + relativedelta(**{unit: count})).isoformat()


# ---- the table ----


def table(
    conn: sqlite3.Connection, the_query: Query | None = None, today: str | None = None
) -> dict:
    """One query's worth of answer: the discovered schema, the visible
    columns, the rows cut into groups, the footer counts and the summary.

    One call rather than five because they have to agree -- a column list
    from before an edit and rows from after it would draw a table with a
    missing column.

    `the_query` omitted means the default read off the garden itself, which
    is the only kind of default a schemaless store can have."""
    motes, relationships, index = read(conn)
    schema = Schema.of(index, motes, relationships)
    asked = the_query if the_query is not None else default_query(schema)
    return {
        "schema": schema.to_json(),
        "query": asked.to_json(),
        "summary": summarize(index, asked),
        # The palette universe rides along with the answer rather than
        # being a call of its own: it changes only when somebody makes a
        # new species, and it must be the garden's *whole* species list --
        # a palette claimed from a subgraph would give one species two
        # colours in two surfaces (docs/architecture.md, *Species colour*).
        "speciesIds": sorted(m.id for m in species_repo.list_species(conn)),
        **answer(index, schema, asked, motes, today),
    }


def answer(
    index: Index,
    schema: Schema,
    the_query: Query,
    motes: list[Mote],
    today: str | None = None,
) -> dict:
    """The rows half of `table()`, off a read a caller already holds.

    Separate from `table()` because two callers need exactly this and
    nothing else around it: `table()` itself, and a surface adding its own
    shape to the same rows."""
    resolved, problems = resolve_query(the_query, schema, today)
    columns = [schema.column(f) for f in the_query.columns] or [schema.columns[0]]
    matched = query.evaluate(index, motes, resolved)
    groups = query.group_rows(index, matched, resolved.group)
    labelled = resolved.group is not None
    # Rendered once and referenced from both shapes. `rows` is the flat
    # list in the order `evaluate()` left them, for a caller drawing a
    # plain table; `groups` is the same rows cut into headers. Building the
    # second from the first rather than beside it is what keeps them from
    # ever disagreeing about a cell.
    drawn = {mote.id: _row(index, mote, columns) for mote in matched}
    return {
        "columns": [c.to_json() for c in columns],
        "groups": [_group(g, drawn, labelled) for g in groups],
        "rows": [drawn[mote.id] for mote in matched],
        "matched": len(matched),
        "total": len(motes),
        "problems": problems,
    }


def _group(group: query.Group, drawn: dict[str, dict], labelled: bool) -> dict:
    return {
        "key": group.key,
        "label": group.label,
        "labelled": labelled,
        "count": len(group.motes),
        "rows": [drawn[mote.id] for mote in group.motes],
    }


def _row(index: Index, mote: Mote, columns: list[Column]) -> dict:
    return {**describe(index, mote.id), "cells": [_cell(index, mote, c) for c in columns]}


def _cell(index: Index, mote: Mote, column: Column) -> dict:
    """A cell carries the text it draws *and* the raw value behind it, so
    the editor opens on what is stored rather than on what was rendered --
    a date cell reading "Aug 25" still edits 2026-08-25."""
    value = query.value_of(index, mote, column.field)
    empty = value is None or value == "" or value == []
    text = query.text_of(index, value)
    if column.field == UPDATED_FIELD and isinstance(value, str):
        # A write time to the minute: the stored stamp carries microseconds
        # and an offset, which is precision nobody reads in a table.
        text = value[:16].replace("T", " ")
    return {
        "key": column_key(column.field),
        "text": text,
        "value": None if empty else value,
        "kind": column.kind,
        "editable": column.editable,
        "empty": empty,
        "ids": list(value) if isinstance(value, list) else [],
    }


def summarize(index: Index, the_query: Query) -> str:
    """The one-line description under a view's name, and a table's
    subtitle. "no filter" rather than an empty string: a view that filters
    nothing is a deliberate thing to have saved."""
    if not the_query.conditions:
        return "no filter"
    join = f" {the_query.join.upper()} "
    return join.join(_condition_text(index, c) for c in the_query.conditions)


def _condition_text(index: Index, condition: Condition) -> str:
    label = label_for(condition.field)
    if condition.op in query.EMPTY_OPS:
        return f"{label} {condition.op}"
    value = condition.value
    if value in index.motes:
        value = index.name_of(value)
    return f"{label} {condition.op} {value or '…'}"

# core/query.py
"""Structured queries over a garden: which motes, in what order, grouped
how.

The language, not the window. `core/modoc.py` is the note language and
Mora is merely an editor for it; this is the same split -- Monolith
(monolith/) is a table editor over the queries defined here, and the CLI
or an MCP tool can run the very same saved query without importing an
app. A saved query is an ordinary mote carrying a `query` property, the
way a note is an ordinary mote carrying `modoc`; carrying one is data,
not a job in the graph's machinery, so it is deliberately not a
`core/kinds.py` kind.

The thing a garden does not have is a schema, so a query cannot name
columns the way SQL does. A **field** is therefore one of four sources:

    property   a JSON property key, present on some motes and not others
    builtin    something every mote has -- name, id, species, kinds,
               degree, last-updated
    rel        the motes across one relationship type, in one direction
    graph      what the graph makes this mote -- its species, its kinds,
               the relationship types touching it

The last two are the point. A table over a graph that could only filter
on properties would be a spreadsheet with extra steps; `depends_on
includes "Schema translation"` and `kind is species` are questions only
this shape of store can answer.

`rel` and `graph` fields are list-valued, which is why they share one set
of operators (includes/excludes/empty) rather than each inventing its
own. "Orphan" is not a predicate here for the same reason: it is already
sayable as `related is empty`.
"""
from collections.abc import Iterable
from dataclasses import dataclass, field as dataclass_field

from core import kinds as kinds_repo
from core.entities import Mote, Relationship
from core.species import INSTANCE_OF

QUERY_KEY = "query"

NAME_KEY = "name"

# ---- field sources ----

PROPERTY = "property"
BUILTIN = "builtin"
REL = "rel"
GRAPH = "graph"
SOURCES = (PROPERTY, BUILTIN, REL, GRAPH)

# ---- builtin fields ----

BUILTIN_NAME = "name"
BUILTIN_ID = "id"
BUILTIN_SPECIES = "species"
BUILTIN_DEGREE = "degree"
BUILTIN_UPDATED = "updated"
BUILTINS = (BUILTIN_NAME, BUILTIN_ID, BUILTIN_SPECIES, BUILTIN_DEGREE, BUILTIN_UPDATED)

# ---- graph fields ----

GRAPH_INSTANCE_OF = "instance_of"
GRAPH_KIND = "kind"
GRAPH_RELATED = "related"
GRAPH_FIELDS = (GRAPH_INSTANCE_OF, GRAPH_KIND, GRAPH_RELATED)

# ---- directions, for a rel field ----

OUT = "out"
IN = "in"

# ---- value kinds and the operators each accepts ----
#
# The kind is the *column's*, guessed from the values in it
# (monolith/payload.py::guess_kind) -- this module never guesses, it only
# says which operators go with which answer. Shared with the frontend so
# the operator picker offers exactly what evaluate() can honour.

TEXT = "text"
NUMBER = "number"
DATE = "date"
BOOL = "bool"
LIST = "list"

EMPTY_OPS = ("is empty", "is not empty")
OPS: dict[str, tuple[str, ...]] = {
    TEXT: ("contains", "is", "is not", *EMPTY_OPS),
    NUMBER: ("=", ">", "<", *EMPTY_OPS),
    DATE: ("is", "before", "after", *EMPTY_OPS),
    BOOL: ("is", "is not", *EMPTY_OPS),
    LIST: ("includes", "excludes", *EMPTY_OPS),
}

AND = "and"
OR = "or"

ASC = "asc"
DESC = "desc"


@dataclass(frozen=True)
class Field:
    """One column's worth of question. `key` means a property key, a
    builtin name, a relationship type or a graph field depending on
    `source`; `direction` is read only for a rel field."""
    source: str
    key: str
    direction: str = OUT

    @property
    def kind(self) -> str:
        """The value kind this field always has, where the field itself
        decides it. A property column's kind is not knowable here -- it
        depends on what is actually stored across the garden -- so it
        comes back empty and the caller (a Schema) guesses."""
        if self.source in (REL, GRAPH):
            return LIST
        if self.source == BUILTIN:
            return NUMBER if self.key == BUILTIN_DEGREE else TEXT
        return ""

    def to_json(self) -> dict:
        return {"source": self.source, "key": self.key, "direction": self.direction}

    @staticmethod
    def from_json(raw: dict) -> "Field | None":
        if not isinstance(raw, dict) or raw.get("source") not in SOURCES:
            return None
        key = raw.get("key")
        if not isinstance(key, str) or not key:
            return None
        direction = raw.get("direction") if raw.get("direction") in (OUT, IN) else OUT
        return Field(raw["source"], key, direction)


@dataclass(frozen=True)
class Condition:
    field: Field
    op: str
    value: str = ""

    def to_json(self) -> dict:
        return {"field": self.field.to_json(), "op": self.op, "value": self.value}

    @staticmethod
    def from_json(raw: dict) -> "Condition | None":
        if not isinstance(raw, dict):
            return None
        field = Field.from_json(raw.get("field") or {})
        op = raw.get("op")
        if field is None or not _known_op(op):
            return None
        value = raw.get("value")
        return Condition(field, op, "" if value is None else str(value))


@dataclass(frozen=True)
class Sort:
    field: Field
    direction: str = ASC

    def to_json(self) -> dict:
        return {"field": self.field.to_json(), "direction": self.direction}

    @staticmethod
    def from_json(raw: dict) -> "Sort | None":
        if not isinstance(raw, dict):
            return None
        field = Field.from_json(raw.get("field") or {})
        if field is None:
            return None
        direction = raw.get("direction") if raw.get("direction") in (ASC, DESC) else ASC
        return Sort(field, direction)


@dataclass(frozen=True)
class Query:
    """What the window is showing: the filter, the order, the grouping,
    and which columns are on. Columns live here rather than beside the
    query because a saved view that forgot its columns would come back
    showing the wrong table."""
    join: str = AND
    conditions: tuple[Condition, ...] = ()
    sort: Sort | None = None
    group: Field | None = None
    columns: tuple[Field, ...] = ()

    def to_json(self) -> dict:
        return {
            "join": self.join,
            "conditions": [c.to_json() for c in self.conditions],
            "sort": self.sort.to_json() if self.sort else None,
            "group": self.group.to_json() if self.group else None,
            "columns": [c.to_json() for c in self.columns],
        }

    @staticmethod
    def from_json(raw: dict | None) -> "Query":
        """Forgiving on purpose: a saved query lives on a mote, and a
        mote's properties are editable from the CLI, from MCP and from
        Monarch's detail panel. A view that has been hand-edited into
        nonsense should come back as a narrower view, not as an exception
        in the window that opens it."""
        if not isinstance(raw, dict):
            return Query()
        return Query(
            join=OR if raw.get("join") == OR else AND,
            conditions=tuple(_each(raw.get("conditions"), Condition.from_json)),
            sort=Sort.from_json(raw.get("sort") or {}),
            group=Field.from_json(raw.get("group") or {}),
            columns=tuple(_each(raw.get("columns"), Field.from_json)),
        )


def _each(raw, parse):
    for item in raw if isinstance(raw, list) else []:
        parsed = parse(item)
        if parsed is not None:
            yield parsed


def _known_op(op) -> bool:
    return isinstance(op, str) and any(op in ops for ops in OPS.values())


def has_query(properties: dict) -> bool:
    """The test for "this mote is a saved view", mirroring
    core/modoc.py::has_modoc: the property is present, even if what it
    holds is an empty query."""
    return isinstance(properties.get(QUERY_KEY), (dict, str))


# ---- the graph, indexed for the questions above ----


@dataclass(frozen=True)
class Index:
    """One read of the garden, arranged so that every field is a lookup
    rather than a scan. Built once per query rather than per mote --
    `evaluate()` is called on whole gardens, and `kinds_in()` alone is a
    full pass over the relationship list."""
    motes: dict[str, Mote]
    outgoing: dict[str, list[Relationship]]
    incoming: dict[str, list[Relationship]]
    kinds: dict[str, list[str]]
    updated: dict[str, str]

    @staticmethod
    def of(
        motes: Iterable[Mote],
        relationships: Iterable[Relationship],
        updated: dict[str, str] | None = None,
    ) -> "Index":
        motes = list(motes)
        relationships = list(relationships)
        outgoing: dict[str, list[Relationship]] = {}
        incoming: dict[str, list[Relationship]] = {}
        for relationship in relationships:
            outgoing.setdefault(relationship.from_id, []).append(relationship)
            incoming.setdefault(relationship.to_id, []).append(relationship)
        return Index(
            motes={m.id: m for m in motes},
            outgoing=outgoing,
            incoming=incoming,
            kinds=kinds_repo.kinds_in(motes, relationships),
            updated=dict(updated or {}),
        )

    def name_of(self, mote_id: str) -> str:
        mote = self.motes.get(mote_id)
        name = mote.properties.get(NAME_KEY) if mote else None
        return name if isinstance(name, str) and name else mote_id[:8]

    def degree(self, mote_id: str) -> int:
        return len(self.outgoing.get(mote_id, [])) + len(self.incoming.get(mote_id, []))

    def species_ids(self, mote_id: str) -> list[str]:
        return [r.to_id for r in self.outgoing.get(mote_id, []) if r.type == INSTANCE_OF]

    def neighbours(self, mote_id: str, type_: str, direction: str) -> list[str]:
        if direction == IN:
            return [r.from_id for r in self.incoming.get(mote_id, []) if r.type == type_]
        return [r.to_id for r in self.outgoing.get(mote_id, []) if r.type == type_]

    def relationship_types(self, mote_id: str) -> list[str]:
        edges = self.outgoing.get(mote_id, []) + self.incoming.get(mote_id, [])
        return sorted({r.type for r in edges})


def value_of(index: Index, mote: Mote, field: Field):
    """A field's value for one mote: a scalar for a property or a
    builtin, a list of mote ids for a rel field, a list of strings for a
    graph field. `None` where the mote simply does not have it -- which
    is what "is empty" tests and what sorting pushes to the end."""
    if field.source == PROPERTY:
        return mote.properties.get(field.key)
    if field.source == REL:
        return index.neighbours(mote.id, field.key, field.direction)
    if field.source == GRAPH:
        return _graph_value(index, mote, field.key)
    return _builtin_value(index, mote, field.key)


def _graph_value(index: Index, mote: Mote, key: str) -> list[str]:
    if key == GRAPH_INSTANCE_OF:
        return index.species_ids(mote.id)
    if key == GRAPH_KIND:
        return list(index.kinds.get(mote.id, []))
    if key == GRAPH_RELATED:
        return index.relationship_types(mote.id)
    return []


def _builtin_value(index: Index, mote: Mote, key: str):
    if key == BUILTIN_ID:
        return mote.id
    if key == BUILTIN_DEGREE:
        return index.degree(mote.id)
    if key == BUILTIN_UPDATED:
        return index.updated.get(mote.id)
    if key == BUILTIN_SPECIES:
        species = index.species_ids(mote.id)
        return index.name_of(species[0]) if species else None
    return index.name_of(mote.id)


def text_of(index: Index, value) -> str:
    """How a value reads in a cell, a group header or a condition chip.
    Mote ids become names here and nowhere else, so a rel column shows
    "Schema translation" while `includes` still matches on either."""
    if value is None or value == "" or value == []:
        return ""
    if isinstance(value, list):
        return ", ".join(index.name_of(v) if v in index.motes else str(v) for v in value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, tuple)):
        return str(value)
    return str(value)


# ---- matching ----


def matches(index: Index, mote: Mote, query: Query) -> bool:
    """No conditions matches everything -- an empty Where bar is "all
    motes", not "no motes"."""
    if not query.conditions:
        return True
    results = (_test(index, mote, c) for c in query.conditions)
    return all(results) if query.join == AND else any(results)


def _test(index: Index, mote: Mote, condition: Condition) -> bool:
    value = value_of(index, mote, condition.field)
    empty = value is None or value == "" or value == []
    if condition.op == "is empty":
        return empty
    if condition.op == "is not empty":
        return not empty
    if empty:
        # An absent value answers the empty operators, and `excludes`,
        # and nothing else. A mote that is an instance of nothing really
        # does exclude every species; but it is not "status is not
        # doing", because it has no status to disagree with -- were it
        # both "is" and "is not", the two would stop partitioning the
        # garden and a pair of complementary views would overlap.
        return condition.op == "excludes"
    if isinstance(value, list):
        return _test_list(index, value, condition)
    return _test_scalar(value, condition)


def _test_list(index: Index, value: list, condition: Condition) -> bool:
    """A rel or graph field. `includes` matches a neighbour by id or by
    name, because the id is what the graph stores and the name is what
    the person typing the condition knows it as."""
    wanted = condition.value.strip().casefold()
    names = {str(v).casefold() for v in value} | {
        index.name_of(str(v)).casefold() for v in value if str(v) in index.motes
    }
    found = wanted in names
    return found if condition.op == "includes" else not found


def _test_scalar(value, condition: Condition) -> bool:
    op, wanted = condition.op, condition.value.strip()
    if op in ("=", ">", "<"):
        return _compare_numbers(value, wanted, op)
    left, right = text_of_scalar(value).casefold(), wanted.casefold()
    if op == "contains":
        return right in left
    if op == "is":
        return left == right
    if op == "is not":
        return left != right
    if op == "before":
        return left < right
    if op == "after":
        return left > right
    return True


def text_of_scalar(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _compare_numbers(value, wanted: str, op: str) -> bool:
    try:
        left, right = float(value), float(wanted)
    except (TypeError, ValueError):
        return False
    if op == "=":
        return left == right
    return left > right if op == ">" else left < right


# ---- ordering and grouping ----


def evaluate(index: Index, motes: Iterable[Mote], query: Query) -> list[Mote]:
    """The rows the query asks for, filtered and sorted. Motes with no
    value for the sort field go last in either direction -- an empty cell
    is missing data, not a smallest value, and burying it under 200 rows
    of nulls is how a sort stops being useful."""
    rows = [m for m in motes if matches(index, m, query)]
    if query.sort is None:
        return rows
    field = query.sort.field
    reverse = query.sort.direction == DESC
    rows.sort(key=lambda m: _sort_key(index, m, field), reverse=reverse)
    rows.sort(key=lambda m: 1 if _is_empty(value_of(index, m, field)) else 0)
    return rows


def _is_empty(value) -> bool:
    return value is None or value == "" or value == []


def _sort_key(index: Index, mote: Mote, field: Field) -> tuple[float, str]:
    """Numbers before text, each in its own order, so a column holding
    both does not raise and does not interleave. The pair sorts as one
    key: numeric values carry their number and an empty string, text
    carries infinity and its own casefolded self."""
    value = value_of(index, mote, field)
    if isinstance(value, bool):
        return (float("inf"), text_of_scalar(value))
    if isinstance(value, (int, float)):
        return (float(value), "")
    text = text_of(index, value)
    try:
        return (float(text), "")
    except ValueError:
        return (float("inf"), text.casefold())


@dataclass(frozen=True)
class Group:
    """One group header and the rows under it."""
    key: str
    label: str
    motes: list[Mote] = dataclass_field(default_factory=list)


UNSET = "unset"


def group_rows(index: Index, rows: Iterable[Mote], field: Field | None) -> list[Group]:
    """Rows in the order `evaluate()` left them, cut into groups. One
    group holding everything when nothing is grouped by, so a caller
    renders one shape either way. A list-valued field groups by its whole
    value ("blocks, depends_on"), not once per element: a row appearing
    under two headers would break the selection maths the table's
    shift-click range and bulk edits depend on."""
    rows = list(rows)
    if field is None:
        return [Group("", "", rows)]
    groups: dict[str, Group] = {}
    for mote in rows:
        value = value_of(index, mote, field)
        label = text_of(index, value)
        key = label or UNSET
        groups.setdefault(key, Group(key, label or UNSET)).motes.append(mote)
    ordered = sorted(groups.values(), key=lambda g: (g.key == UNSET, g.key.casefold()))
    return ordered

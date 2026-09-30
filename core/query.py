# core/query.py
"""Structured queries over a garden: which motes, in what order, grouped
how.

The language, not the window. `core/modoc.py` is the note language and
Mora is merely an editor for it; this is the same split -- the console's
`find` table (console/monolith.py) is a view over the queries defined here, and the CLI
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
from core import rules as rules_repo
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
# (core/table.py::guess_kind) -- this module never guesses, it only says
# which operators go with which answer. Shared with every surface so an
# operator picker offers exactly what evaluate() can honour, and read by
# core/table.py::resolve_query to decide which operator a written
# condition means on the column it lands on.

TEXT = "text"
NUMBER = "number"
DATE = "date"
BOOL = "bool"
LIST = "list"

EMPTY_OPS = ("is empty", "is not empty")
# Every scalar kind takes `is not` and `contains`, and text and dates take
# before/after, because `_test_scalar` below answers all of them from one
# string comparison -- there was never an evaluator reason for a number
# column to lack `is not`, only that nothing had asked for one. The text
# grammar (core/findtext.py) asks: `estimate!:3` has to mean something.
OPS: dict[str, tuple[str, ...]] = {
    TEXT: ("contains", "is", "is not", "before", "after", *EMPTY_OPS),
    NUMBER: ("=", ">", "<", "is not", "contains", *EMPTY_OPS),
    DATE: ("is", "before", "after", "is not", "contains", *EMPTY_OPS),
    BOOL: ("is", "is not", *EMPTY_OPS),
    LIST: ("includes", "excludes", *EMPTY_OPS),
}

AND = "and"
OR = "or"

ASC = "asc"
DESC = "desc"

# ---- what an operator *meant*, independent of the column it lands on ----
#
# A garden has no schema, so `estimate:>3` cannot know whether it is a
# numeric `>` or a lexical `after` until somebody looks at what `estimate`
# holds. The intent is what survives that ignorance: a written condition
# records one, and core/table.py::resolve_query turns it into the operator
# the discovered kind accepts, at evaluate time. That is what keeps a
# saved view meaning what it said the day the column's kind changes.

IS = "is"
NOT = "not"
CONTAINS = "contains"
GREATER = "greater"
LESS = "less"
EMPTY = "empty"
NOT_EMPTY = "not empty"
INTENTS = (IS, NOT, CONTAINS, GREATER, LESS, EMPTY, NOT_EMPTY)

_OP_BY_KIND: dict[str, dict[str, str]] = {
    TEXT: {IS: "is", NOT: "is not", CONTAINS: "contains",
           GREATER: "after", LESS: "before"},
    NUMBER: {IS: "=", NOT: "is not", CONTAINS: "contains",
             GREATER: ">", LESS: "<"},
    DATE: {IS: "is", NOT: "is not", CONTAINS: "contains",
           GREATER: "after", LESS: "before"},
    BOOL: {IS: "is", NOT: "is not"},
    LIST: {IS: "includes", NOT: "excludes"},
}
_OP_BY_EMPTY_INTENT = {EMPTY: EMPTY_OPS[0], NOT_EMPTY: EMPTY_OPS[1]}
_INTENT_OF_OP = {
    "is": IS, "=": IS, "includes": IS,
    "is not": NOT, "excludes": NOT,
    "contains": CONTAINS,
    ">": GREATER, "after": GREATER,
    "<": LESS, "before": LESS,
    EMPTY_OPS[0]: EMPTY, EMPTY_OPS[1]: NOT_EMPTY,
}


def intent_of(op: str) -> str:
    """What a concrete operator meant. An unknown operator reads as `is`,
    matching this module's forgiving reading of a hand-edited view."""
    return _INTENT_OF_OP.get(op, IS)


def op_for(intent: str, kind: str) -> str | None:
    """The operator `intent` becomes on a column of `kind`, or None where
    the pair means nothing: `kind:>x` asks a list of kinds to be greater
    than something, and there is no honest answer to give it."""
    if intent in _OP_BY_EMPTY_INTENT:
        return _OP_BY_EMPTY_INTENT[intent]
    return _OP_BY_KIND.get(kind, {}).get(intent)


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
            if self.key == BUILTIN_DEGREE:
                return NUMBER
            # `updated` is a timestamp core/motes.py writes, so it earns
            # before/after and a relative date (`updated:-7d`) rather
            # than being compared as prose.
            return DATE if self.key == BUILTIN_UPDATED else TEXT
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
        """A mote's `name`, or -- for a rule a worksheet left unnamed --
        its equation (core/rules.py::label_of). A row reading
        `l == sqrt(x^2 + y^2)` says what that mote is; one reading
        `1f3533ca` says nothing at all."""
        mote = self.motes.get(mote_id)
        if mote is None:
            return mote_id[:8]
        return rules_repo.label_of(mote.properties, mote_id)

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
    left = _same_precision(left, right)
    if op == "is":
        return left == right
    if op == "is not":
        return left != right
    if op == "before":
        return left < right
    if op == "after":
        return left > right
    return True


_DATE_ONLY = 10


def _same_precision(left: str, right: str) -> str:
    """`left` truncated to `right`'s precision when `right` is a bare
    `yyyy-mm-dd` and `left` is a longer timestamp.

    Which is what makes `due:2026-10-01` match a mote due
    `2026-10-01T09:00`. Comparing the full strings would answer "is it
    exactly midnight" to a question that was about the day, and would
    likewise put every timed event on the 1st *after* the 1st. A whole
    day is the unit someone naming a date without a time meant."""
    if len(right) != _DATE_ONLY or len(left) <= _DATE_ONLY:
        return left
    head = left[:_DATE_ONLY]
    return head if head[4:5] == "-" and head[7:8] == "-" else left


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

# core/findtext.py
"""One typed line as a query: `status:todo due:<7d --sort -updated`.

`core/query.py` is the query *language* -- fields, operators, joins,
sorting, grouping -- and Monolith was an editor for it built out of
dropdowns. The console has one line to type into instead, so the same
query needs a written form, and this is it: `parse_find()` reads a line
into a Query, `format_find()` writes one back out, and
`format_find(parse_find(text)) == text` for text already in canonical
form.

That round trip is the reason this is a module and not a regex in a
bridge. A console shows you the line you typed, a saved view shows you the
line that reproduces it, and a completion offers to extend the line you
are halfway through -- three things that only agree if one function
answers what a query *reads* as.

    key:value      is            (includes, on a list-valued column)
    key~value      contains
    key:>value     greater       (after, on a date column)
    key:<value     less          (before, on a date column)
    key!:value     is not        (excludes)
    key:?          is empty
    key!:?         is not empty

    or             join with OR rather than AND
    --sort key     ascending; `--sort -key` descending
    --group key
    --cols a,b,c
    --view name    start from a saved view; conditions here add to it

Which operator each of those *becomes* is not decided here. A garden has
no schema, so whether `estimate:>3` is a numeric `>` or a lexical `after`
depends on what `estimate` actually holds, and that is discovered per
read: `core/table.py::resolve_query()` settles it at evaluate time, along
with turning `7d` into a date. This module records the intent and the
value verbatim -- which is what lets a saved view keep meaning what it
said when the garden underneath it changes.
"""
import shlex
from dataclasses import dataclass, replace

from core import query
from core import table as table_repo
from core.query import Condition, Field, Query, Sort

# `type:` and `species:` both ask the question `instance_of` answers.
# Two spellings because both are what people call it, and neither is worth
# making the other's synonym in the field map below.
TYPE_ALIASES = ("type", "species")

GRAPH_KEYS = {
    "kind": query.GRAPH_KIND,
    "related": query.GRAPH_RELATED,
}
BUILTIN_KEYS = {
    "name": query.BUILTIN_NAME,
    "id": query.BUILTIN_ID,
    "degree": query.BUILTIN_DEGREE,
    "updated": query.BUILTIN_UPDATED,
}

OUT_PREFIX = "->"
IN_PREFIX = "<-"

EMPTY_VALUE = "?"
JOIN_WORD = "or"

SORT_FLAG = "--sort"
GROUP_FLAG = "--group"
COLS_FLAG = "--cols"
VIEW_FLAG = "--view"
FLAGS = (SORT_FLAG, GROUP_FLAG, COLS_FLAG, VIEW_FLAG)

DESC_MARKER = "-"


class FindSyntaxError(ValueError):
    """A line that doesn't parse. Always names the token at fault, because
    a one-line input box has nowhere to put a paragraph and the person
    typing it needs to know which word to fix."""


@dataclass(frozen=True)
class FindSpec:
    """A parsed line: the query it says, and the saved view it starts from.

    `view` is a *name*, not a query, because parsing takes no connection:
    resolving a name to a mote is the caller's to do, and keeping it out of
    here is what lets the grammar be tested without a garden."""
    query: Query
    view: str | None = None


# ---- parsing ----


def parse_find(text: str) -> FindSpec:
    """One line as a FindSpec. An empty line is every mote, unsorted and
    uncolumned -- the caller fills in `core/table.py::default_query`'s
    columns, since what a sensible default is depends on the garden."""
    try:
        tokens = shlex.split(text or "")
    except ValueError as e:
        raise FindSyntaxError(f"unbalanced quotes: {e}") from None

    join = query.AND
    conditions: list[Condition] = []
    sort: Sort | None = None
    group: Field | None = None
    columns: tuple[Field, ...] = ()
    view: str | None = None

    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in FLAGS:
            value = _flag_value(tokens, index, token)
            index += 2
            if token == SORT_FLAG:
                sort = _parse_sort(value)
            elif token == GROUP_FLAG:
                group = parse_field(value)
            elif token == COLS_FLAG:
                columns = _parse_columns(value)
            else:
                view = value
            continue
        index += 1
        if token.casefold() == JOIN_WORD:
            join = query.OR
            continue
        conditions.append(parse_condition(token))

    return FindSpec(
        query=Query(join=join, conditions=tuple(conditions), sort=sort,
                    group=group, columns=columns),
        view=view,
    )


def _flag_value(tokens: list[str], index: int, flag: str) -> str:
    if index + 1 >= len(tokens):
        raise FindSyntaxError(f"{flag} needs a value")
    value = tokens[index + 1]
    if value in FLAGS:
        raise FindSyntaxError(f"{flag} needs a value, got {value!r}")
    return value


def _parse_sort(value: str) -> Sort:
    descending = value.startswith(DESC_MARKER)
    field = parse_field(value[1:] if descending else value)
    return Sort(field, query.DESC if descending else query.ASC)


def _parse_columns(value: str) -> tuple[Field, ...]:
    return tuple(parse_field(part) for part in value.split(",") if part.strip())


def parse_condition(token: str) -> Condition:
    """One `key<op>value` token. The operator is found before the field is,
    because `->depends_on:x` and `a!:b` both contain characters the field
    half is allowed to use."""
    key, intent, value = _split_condition(token)
    if not key:
        raise FindSyntaxError(f"{token!r} has no field before its operator")
    field = parse_field(key)
    # `?` is the empty test, and only on the two plain operators: `due:>?`
    # would be asking whether a missing value is greater than something,
    # which is a typo rather than a question.
    if value == EMPTY_VALUE and intent in (query.IS, query.NOT):
        intent = query.EMPTY if intent == query.IS else query.NOT_EMPTY
        value = ""
    elif not value:
        raise FindSyntaxError(f"{token!r} has no value after its operator")
    # Stored as a concrete operator, so this is an ordinary Condition and
    # every existing reader of one still works. Every intent has a `text`
    # operator, and that is the one written down; which operator it
    # *really* means is settled against the column's discovered kind at
    # evaluate time -- see core/table.py::resolve_query.
    return Condition(field, query.op_for(intent, query.TEXT), value)


def _split_condition(token: str) -> tuple[str, str, str]:
    """`(key, intent, value)`. Longest operator first, so `!:?` is not read
    as `!:` followed by a stray `?`."""
    if "~" in token:
        key, _, value = token.partition("~")
        return key, query.CONTAINS, value
    if "!:" in token:
        key, _, value = token.partition("!:")
        return key, query.NOT, value
    if ":" not in token:
        raise FindSyntaxError(
            f"{token!r} is not a condition -- write `key:value`, `key~value`, "
            f"`key:>value`, `key!:value` or `key:?`"
        )
    key, _, value = token.partition(":")
    if value.startswith(">"):
        return key, query.GREATER, value[1:]
    if value.startswith("<"):
        return key, query.LESS, value[1:]
    return key, query.IS, value


def parse_field(text: str) -> Field:
    """A field spec as a Field. Anything the map below doesn't claim is a
    property key, which is the right default in a store where a property
    is the only thing a person can invent."""
    text = text.strip()
    if not text:
        raise FindSyntaxError("a field name cannot be empty")
    if text.startswith(OUT_PREFIX):
        return _rel_field(text[len(OUT_PREFIX):], query.OUT, text)
    if text.startswith(IN_PREFIX):
        return _rel_field(text[len(IN_PREFIX):], query.IN, text)
    lowered = text.casefold()
    if lowered in TYPE_ALIASES:
        return Field(query.GRAPH, query.GRAPH_INSTANCE_OF)
    if lowered in GRAPH_KEYS:
        return Field(query.GRAPH, GRAPH_KEYS[lowered])
    if lowered in BUILTIN_KEYS:
        return Field(query.BUILTIN, BUILTIN_KEYS[lowered])
    return Field(query.PROPERTY, text)


def _rel_field(key: str, direction: str, token: str) -> Field:
    if not key:
        raise FindSyntaxError(f"{token!r} names a direction but no relationship type")
    return Field(query.REL, key, direction)


# ---- writing one back out ----


def format_find(spec: FindSpec | Query) -> str:
    """The canonical line for a query: view first, then the conditions in
    order, then the flags. One fixed order so the round trip holds -- a
    caller that re-renders the line under the cursor must not shuffle it
    while someone is typing."""
    the_query = spec.query if isinstance(spec, FindSpec) else spec
    view = spec.view if isinstance(spec, FindSpec) else None
    parts: list[str] = []
    if view:
        parts += [VIEW_FLAG, _quote(view)]
    separator = f" {JOIN_WORD} " if the_query.join == query.OR else " "
    body = separator.join(format_condition(c) for c in the_query.conditions)
    if body:
        parts.append(body)
    if the_query.sort is not None:
        marker = DESC_MARKER if the_query.sort.direction == query.DESC else ""
        parts += [SORT_FLAG, _quote(marker + format_field(the_query.sort.field))]
    if the_query.group is not None:
        parts += [GROUP_FLAG, _quote(format_field(the_query.group))]
    if the_query.columns:
        parts += [COLS_FLAG, _quote(",".join(format_field(f) for f in the_query.columns))]
    return " ".join(parts)


def format_condition(condition: Condition) -> str:
    field = format_field(condition.field)
    intent = query.intent_of(condition.op)
    if intent == query.EMPTY:
        return f"{field}:{EMPTY_VALUE}"
    if intent == query.NOT_EMPTY:
        return f"{field}!:{EMPTY_VALUE}"
    value = condition.value
    if intent == query.CONTAINS:
        return f"{field}~{_quote(value)}"
    if intent == query.NOT:
        return f"{field}!:{_quote(value)}"
    prefix = {query.GREATER: ">", query.LESS: "<"}.get(intent, "")
    return f"{field}:{_quote(prefix + value)}"


def format_field(field: Field) -> str:
    """The spelling `parse_field` reads back. `instance_of` comes out as
    `type` rather than `species`, which is the one place the two aliases
    stop being equals -- a canonical form has to pick one."""
    if field.source == query.REL:
        prefix = OUT_PREFIX if field.direction == query.OUT else IN_PREFIX
        return f"{prefix}{field.key}"
    if field.source == query.GRAPH:
        if field.key == query.GRAPH_INSTANCE_OF:
            return TYPE_ALIASES[0]
        return next((k for k, v in GRAPH_KEYS.items() if v == field.key), field.key)
    if field.source == query.BUILTIN:
        return next((k for k, v in BUILTIN_KEYS.items() if v == field.key), field.key)
    return field.key


def _quote(value: str) -> str:
    """Quoted only where it has to be, so the canonical form of an
    ordinary condition is the plain thing a person types. shlex.quote
    would wrap `>7d` for the `>`, which shlex.split never needed."""
    if value and not any(c.isspace() or c in "'\"" for c in value):
        return value
    return shlex.quote(value)


# ---- completion ----


def find_completions(schema: table_repo.Schema, token: str) -> list[tuple[str, str]]:
    """`(text, description)` pairs for the token under the cursor: field
    names while one is being typed, and that field's own values once it
    carries an operator.

    Values come from the column's discovered distinct set, which is the
    same thing that makes a cell editor offer todo/doing/blocked -- so a
    garden that never declared a status type still completes one."""
    if _has_operator(token):
        key, _intent, partial = _split_condition_quietly(token)
        return _value_completions(schema, key, partial, token)
    return _field_completions(schema, token)


def _has_operator(token: str) -> bool:
    return "~" in token or ":" in token


def _split_condition_quietly(token: str) -> tuple[str, str, str]:
    try:
        return _split_condition(token)
    except FindSyntaxError:
        return token, query.IS, ""


def _field_completions(schema: table_repo.Schema, token: str) -> list[tuple[str, str]]:
    needle = token.strip().casefold()
    found = []
    for spelling, description in _field_vocabulary(schema):
        if spelling.casefold().startswith(needle):
            found.append((f"{spelling}:", description))
    return found


def _field_vocabulary(schema: table_repo.Schema) -> list[tuple[str, str]]:
    """Every field name that can be typed, in the order the schema lists
    its columns -- so the commonest property keys come before the rarest,
    the same ranking the column rail uses."""
    vocabulary = [(TYPE_ALIASES[0], "the species a mote is an instance of")]
    for column in schema.columns:
        spelling = format_field(column.field)
        # A column whose spelling reads back as another field (the
        # `species` builtin reads as `type`) has no written form to offer.
        if spelling == TYPE_ALIASES[0] or parse_field(spelling) != column.field:
            continue
        vocabulary.append((spelling, f"{column.label} ({column.kind}, {column.fill})"))
    return vocabulary


def _value_completions(
    schema: table_repo.Schema, key: str, partial: str, token: str
) -> list[tuple[str, str]]:
    try:
        field = parse_field(key)
    except FindSyntaxError:
        return []
    column = schema.column(field)
    prefix = token[:len(token) - len(partial)]
    needle = partial.casefold()
    found = [
        (prefix + value, column.label)
        for value in column.values
        if value.casefold().startswith(needle)
    ]
    if not needle:
        found.append((prefix + EMPTY_VALUE, f"{column.label} is empty"))
    return found


# ---- rewriting a line for a gesture ----

_UNSET = object()


def rewrite(text: str, sort: Field | None | object = _UNSET,
            group: Field | None | object = _UNSET) -> str:
    """`text` with its sort or grouping changed, in canonical form -- what
    a header click or a `g` in the table turns into, so the pane's command
    stays the whole truth about what it shows and JS never writes find
    syntax.

    Sorting by the field already sorted on flips its direction; any other
    field sorts ascending. `group=None` removes the grouping."""
    spec = parse_find(text)
    the_query = spec.query
    if sort is not _UNSET:
        if sort is None:
            the_query = replace(the_query, sort=None)
        elif the_query.sort is not None and the_query.sort.field == sort:
            flipped = query.ASC if the_query.sort.direction == query.DESC else query.DESC
            the_query = replace(the_query, sort=Sort(sort, flipped))
        else:
            the_query = replace(the_query, sort=Sort(sort, query.ASC))
    if group is not _UNSET:
        the_query = replace(the_query, group=group)
    return format_find(FindSpec(the_query, spec.view))


def with_view(spec: FindSpec, saved: Query) -> Query:
    """The spec's own conditions added to a saved view's, which is what
    `--view` means: the view is where you started, not what you are stuck
    with. Its sort, grouping and columns stand unless the line overrode
    them."""
    return replace(
        saved,
        join=spec.query.join if spec.query.conditions else saved.join,
        conditions=saved.conditions + spec.query.conditions,
        sort=spec.query.sort or saved.sort,
        group=spec.query.group or saved.group,
        columns=spec.query.columns or saved.columns,
    )

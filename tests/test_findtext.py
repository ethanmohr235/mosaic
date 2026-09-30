"""core/findtext.py -- one typed line as a query.

Two things earn a test here. That every form of the grammar reads as the
Condition it says, and that `format_find(parse_find(text)) == text` for
canonical text -- the round trip is what lets a console echo the line you
typed, a saved view show the line that reproduces it, and a completion
extend the line you are halfway through, all from one answer.
"""
import pytest

from core import query as q
from core import table as table_repo
from core.findtext import (
    FindSpec,
    FindSyntaxError,
    find_completions,
    format_find,
    parse_field,
    parse_find,
    with_view,
)
from core.motes import create_mote
from core.relationships import create_relationship
from core.table import Schema, read


def only(text: str) -> q.Condition:
    conditions = parse_find(text).query.conditions
    assert len(conditions) == 1, conditions
    return conditions[0]


# ---- the field map ----

def test_a_bare_key_is_a_property(conn):
    assert parse_field("status") == q.Field(q.PROPERTY, "status")


def test_type_and_species_both_ask_what_instance_of_answers(conn):
    expected = q.Field(q.GRAPH, q.GRAPH_INSTANCE_OF)
    assert parse_field("type") == expected
    assert parse_field("species") == expected


def test_the_builtins_and_graph_fields_have_their_own_spellings(conn):
    assert parse_field("name") == q.Field(q.BUILTIN, q.BUILTIN_NAME)
    assert parse_field("id") == q.Field(q.BUILTIN, q.BUILTIN_ID)
    assert parse_field("degree") == q.Field(q.BUILTIN, q.BUILTIN_DEGREE)
    assert parse_field("updated") == q.Field(q.BUILTIN, q.BUILTIN_UPDATED)
    assert parse_field("kind") == q.Field(q.GRAPH, q.GRAPH_KIND)
    assert parse_field("related") == q.Field(q.GRAPH, q.GRAPH_RELATED)


def test_an_arrow_names_a_relationship_in_one_direction(conn):
    assert parse_field("->depends_on") == q.Field(q.REL, "depends_on", q.OUT)
    assert parse_field("<-depends_on") == q.Field(q.REL, "depends_on", q.IN)


def test_a_direction_with_no_type_is_a_syntax_error(conn):
    with pytest.raises(FindSyntaxError, match="no relationship type"):
        parse_field("->")


# ---- the operators ----

def test_every_operator_form_reads_as_the_intent_it_says(conn):
    assert q.intent_of(only("status:doing").op) == q.IS
    assert q.intent_of(only("status~do").op) == q.CONTAINS
    assert q.intent_of(only("estimate:>3").op) == q.GREATER
    assert q.intent_of(only("estimate:<3").op) == q.LESS
    assert q.intent_of(only("status!:doing").op) == q.NOT
    assert only("status:?").op == q.EMPTY_OPS[0]
    assert only("status!:?").op == q.EMPTY_OPS[1]


def test_the_value_is_kept_verbatim(conn):
    """Including a relative date -- resolving it here would freeze a saved
    view on the day it was written."""
    assert only("due:7d").value == "7d"
    assert only("estimate:>3").value == "3"


def test_a_quoted_value_may_contain_spaces(conn):
    assert only('owner:"Rina Okafor"').value == "Rina Okafor"


def test_a_question_mark_is_only_the_empty_test_on_the_plain_operators(conn):
    """`due:>?` is a typo, not a question about whether a missing value is
    greater than something."""
    condition = only("due:>?")
    assert q.intent_of(condition.op) == q.GREATER
    assert condition.value == "?"


def test_several_conditions_default_to_and(conn):
    parsed = parse_find("status:doing estimate:>3").query
    assert parsed.join == q.AND
    assert len(parsed.conditions) == 2


def test_a_bare_or_switches_the_join(conn):
    parsed = parse_find("status:doing or status:todo").query
    assert parsed.join == q.OR
    assert len(parsed.conditions) == 2


def test_a_token_with_no_operator_names_what_is_wrong(conn):
    with pytest.raises(FindSyntaxError, match="'doing'"):
        parse_find("doing")


def test_a_token_with_no_value_names_what_is_wrong(conn):
    with pytest.raises(FindSyntaxError, match="no value"):
        parse_find("status:")


def test_unbalanced_quotes_are_a_syntax_error(conn):
    with pytest.raises(FindSyntaxError, match="quotes"):
        parse_find('owner:"Rina')


# ---- the flags ----

def test_sort_ascending_and_descending(conn):
    assert parse_find("--sort updated").query.sort == q.Sort(
        q.Field(q.BUILTIN, q.BUILTIN_UPDATED), q.ASC)
    assert parse_find("--sort -updated").query.sort == q.Sort(
        q.Field(q.BUILTIN, q.BUILTIN_UPDATED), q.DESC)


def test_group_and_cols(conn):
    parsed = parse_find("--group status --cols name,status,->depends_on").query
    assert parsed.group == q.Field(q.PROPERTY, "status")
    assert parsed.columns == (
        q.Field(q.BUILTIN, q.BUILTIN_NAME),
        q.Field(q.PROPERTY, "status"),
        q.Field(q.REL, "depends_on", q.OUT),
    )


def test_view_is_carried_as_a_name_not_resolved(conn):
    """Parsing takes no connection, so a name stays a name -- looking it up
    is the caller's, which is what lets the grammar be tested with no
    garden at all."""
    spec = parse_find("--view 'This week' status:doing")
    assert spec.view == "This week"
    assert len(spec.query.conditions) == 1


def test_a_flag_with_no_value_names_the_flag(conn):
    with pytest.raises(FindSyntaxError, match="--sort needs a value"):
        parse_find("status:doing --sort")
    with pytest.raises(FindSyntaxError, match="--group needs a value"):
        parse_find("--group --sort name")


def test_an_empty_line_is_every_mote(conn):
    parsed = parse_find("").query
    assert parsed == q.Query()


# ---- the round trip ----

CANONICAL = [
    "status:doing",
    "status~do",
    "estimate:>3",
    "estimate:<3",
    "status!:doing",
    "status:?",
    "status!:?",
    "due:7d",
    "status:doing estimate:>3",
    "status:doing or status:todo",
    "owner:'Rina Okafor'",
    "type:Task",
    "kind:species",
    "related:?",
    "->depends_on:Schema",
    "<-depends_on:Schema",
    "--sort updated",
    "--sort -updated",
    "--group status",
    "--cols name,status,->depends_on",
    "--view 'This week' status:doing",
    "--view Inbox status:doing --sort -updated --group status --cols name,status",
]


@pytest.mark.parametrize("text", CANONICAL)
def test_canonical_text_round_trips(conn, text):
    assert format_find(parse_find(text)) == text


def test_a_query_built_from_dropdowns_also_formats(conn):
    """A view saved from the old picker carries concrete operators (`=`,
    `includes`). It has to come back as a line somebody can read, which is
    what `intent_of` is for."""
    built = q.Query(conditions=(
        q.Condition(q.Field(q.PROPERTY, "estimate"), "=", "3"),
        q.Condition(q.Field(q.GRAPH, q.GRAPH_INSTANCE_OF), "includes", "Task"),
    ))
    assert format_find(built) == "estimate:3 type:Task"


def test_species_formats_back_as_type(conn):
    """The one place the two aliases stop being equals: a canonical form
    has to pick one spelling."""
    assert format_find(parse_find("species:Task")) == "type:Task"


# ---- --view composition ----

def test_a_lines_conditions_add_to_the_views(conn):
    saved = q.Query(
        conditions=(q.Condition(q.Field(q.PROPERTY, "status"), "is", "doing"),),
        sort=q.Sort(q.Field(q.BUILTIN, q.BUILTIN_NAME)),
    )
    spec = parse_find("estimate:>3")
    combined = with_view(spec, saved)
    assert len(combined.conditions) == 2
    assert combined.sort == saved.sort


def test_a_line_overrides_the_views_sort_when_it_names_one(conn):
    saved = q.Query(sort=q.Sort(q.Field(q.BUILTIN, q.BUILTIN_NAME)))
    combined = with_view(parse_find("--sort -updated"), saved)
    assert combined.sort == q.Sort(q.Field(q.BUILTIN, q.BUILTIN_UPDATED), q.DESC)


# ---- completion ----

@pytest.fixture
def schema(conn) -> Schema:
    species = create_mote(conn, {"name": "Task"})
    for name, status in (("Schema", "doing"), ("Backfill", "todo")):
        mote = create_mote(conn, {"name": name, "status": status})
        create_relationship(conn, mote, species, q.INSTANCE_OF)
    motes, relationships, index = read(conn)
    return Schema.of(index, motes, relationships)


def test_a_partial_token_completes_field_names(schema):
    found = dict(find_completions(schema, "stat"))
    assert "status:" in found
    assert "text" in found["status:"]


def test_a_token_with_an_operator_completes_that_columns_values(schema):
    found = dict(find_completions(schema, "status:d"))
    assert "status:doing" in found
    assert "status:todo" not in found


def test_completing_an_empty_value_offers_the_empty_test(schema):
    assert "status:?" in dict(find_completions(schema, "status:"))


def test_type_is_offered_even_though_no_column_is_called_that(schema):
    assert "type:" in dict(find_completions(schema, "ty"))


def test_completion_never_raises_on_a_half_typed_token(schema):
    for token in ("", "-", "->", "->x:", "!:", "~"):
        find_completions(schema, token)


def test_a_completion_is_a_whole_token_ready_to_replace_what_was_typed(schema):
    """So a caller swaps the token under the cursor for the completion
    rather than having to work out which half to keep."""
    assert find_completions(schema, "status:doin")[0][0] == "status:doing"


def test_every_operator_survives_a_trip_through_its_intent(conn):
    """A drift guard. An operator nobody mapped reads back as `is`, which
    would silently turn a saved `estimate < 3` into `estimate = 3` -- so
    every operator the language has must come back as itself."""
    for kind, ops in q.OPS.items():
        for op in ops:
            assert q.op_for(q.intent_of(op), kind) == op, (kind, op)


def test_every_intent_has_a_text_operator(conn):
    """`parse_condition` writes the text operator down, so a gap here
    would make some form of the grammar unwritable."""
    assert all(q.op_for(intent, q.TEXT) for intent in q.INTENTS)


@pytest.mark.parametrize("text", [t for t in CANONICAL if not t.startswith("--")])
def test_the_resolution_step_accepts_what_the_parser_writes(schema, text):
    """The seam between the two modules. Every condition the grammar can
    produce has to survive core/table.py's resolution against a real
    schema -- an operator the parser writes but resolution cannot place
    would make that line of the grammar silently do nothing."""
    parsed = parse_find(text).query
    resolved, problems = table_repo.resolve_query(parsed, schema)
    assert len(resolved.conditions) == len(parsed.conditions), problems
    for condition in resolved.conditions:
        kind = schema.column(condition.field).kind
        assert condition.op in q.OPS[kind], (text, condition, kind)

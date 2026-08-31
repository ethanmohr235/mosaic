"""core/query.py -- the query language Monolith's table is a view of.

Built on plain Mote/Relationship lists rather than through a connection:
nothing here reads or writes sqlite, and a test that opened a garden
would be testing core/graph.py instead.
"""
import pytest

from core import query as q
from core.entities import Mote, Relationship
from core.species import INSTANCE_OF


def mote(id: str, **properties) -> Mote:
    return Mote(properties=properties, id=id)


def rel(from_id: str, to_id: str, type_: str) -> Relationship:
    return Relationship(from_id=from_id, to_id=to_id, type=type_, id=f"{from_id}-{to_id}-{type_}")


@pytest.fixture
def garden() -> tuple[list[Mote], list[Relationship]]:
    motes = [
        mote("task", name="Task"),
        mote("schema", name="Schema translation", status="doing", estimate=5),
        mote("backfill", name="Backfill dry run", status="doing", estimate=3),
        mote("rehearsal", name="Cutover rehearsal", status="todo", estimate=4),
        mote("tiering", name="Cold tiering spike", status="blocked"),
        mote("lonely", name="Unfiled thought"),
    ]
    relationships = [
        rel("schema", "task", INSTANCE_OF),
        rel("backfill", "task", INSTANCE_OF),
        rel("rehearsal", "task", INSTANCE_OF),
        rel("backfill", "schema", "depends_on"),
        rel("rehearsal", "backfill", "depends_on"),
        rel("tiering", "schema", "blocks"),
    ]
    return motes, relationships


@pytest.fixture
def index(garden) -> q.Index:
    return q.Index.of(*garden, updated={"schema": "2026-08-25", "backfill": "2026-08-20"})


def ids(motes) -> list[str]:
    return [m.id for m in motes]


def prop(key: str) -> q.Field:
    return q.Field(q.PROPERTY, key)


def run(index: q.Index, garden, **kwargs) -> list[str]:
    return ids(q.evaluate(index, garden[0], q.Query(**kwargs)))


# ---- properties ----


def test_no_conditions_is_every_mote(index, garden):
    assert run(index, garden) == ids(garden[0])


def test_is_matches_case_insensitively(index, garden):
    conditions = (q.Condition(prop("status"), "is", "DOING"),)
    assert run(index, garden, conditions=conditions) == ["schema", "backfill"]


def test_contains_is_a_substring(index, garden):
    conditions = (q.Condition(prop("name"), "contains", "run"),)
    assert run(index, garden, conditions=conditions) == ["backfill"]


def test_is_empty_finds_motes_without_the_property(index, garden):
    conditions = (q.Condition(prop("status"), "is empty", ""),)
    assert run(index, garden, conditions=conditions) == ["task", "lonely"]


def test_is_not_excludes_only_motes_that_have_the_value(index, garden):
    """A mote with no status is not "status is not doing" -- an absent
    property answers the empty operators and nothing else, which is what
    keeps `is` and `is not` from both being true of it."""
    conditions = (q.Condition(prop("status"), "is not", "doing"),)
    assert run(index, garden, conditions=conditions) == ["rehearsal", "tiering"]


def test_numeric_operators_compare_as_numbers(index, garden):
    conditions = (q.Condition(prop("estimate"), ">", "3"),)
    assert run(index, garden, conditions=conditions) == ["schema", "rehearsal"]


def test_and_joins_narrow_or_joins_widen(index, garden):
    conditions = (
        q.Condition(prop("status"), "is", "doing"),
        q.Condition(prop("estimate"), "<", "5"),
    )
    assert run(index, garden, conditions=conditions, join=q.AND) == ["backfill"]
    assert run(index, garden, conditions=conditions, join=q.OR) == [
        "schema", "backfill", "rehearsal",
    ]


# ---- builtins ----


def test_degree_counts_edges_in_both_directions(index, garden):
    conditions = (q.Condition(q.Field(q.BUILTIN, q.BUILTIN_DEGREE), "=", "3"),)
    assert run(index, garden, conditions=conditions) == ["task", "schema", "backfill"]


def test_species_reads_as_the_species_name(index, garden):
    conditions = (q.Condition(q.Field(q.BUILTIN, q.BUILTIN_SPECIES), "is", "Task"),)
    assert run(index, garden, conditions=conditions) == ["schema", "backfill", "rehearsal"]


def test_updated_comes_from_the_caller_not_the_mote(index, garden):
    conditions = (q.Condition(q.Field(q.BUILTIN, q.BUILTIN_UPDATED), "after", "2026-08-22"),)
    assert run(index, garden, conditions=conditions) == ["schema"]


def test_name_falls_back_to_a_short_id(index):
    unnamed = mote("0123456789abcdef")
    index = q.Index.of([unnamed], [])
    assert q.value_of(index, unnamed, q.Field(q.BUILTIN, q.BUILTIN_NAME)) == "01234567"


# ---- relationships and graph shape ----


def test_rel_column_reads_one_direction_at_a_time(index, garden):
    backfill = garden[0][2]
    out = q.Field(q.REL, "depends_on", q.OUT)
    into = q.Field(q.REL, "depends_on", q.IN)
    assert q.value_of(index, backfill, out) == ["schema"]
    assert q.value_of(index, backfill, into) == ["rehearsal"]


def test_includes_matches_a_neighbour_by_name_or_id(index, garden):
    by_name = (q.Condition(q.Field(q.REL, "depends_on"), "includes", "Schema translation"),)
    by_id = (q.Condition(q.Field(q.REL, "depends_on"), "includes", "schema"),)
    assert run(index, garden, conditions=by_name) == ["backfill"]
    assert run(index, garden, conditions=by_id) == ["backfill"]


def test_related_is_empty_is_how_you_ask_for_orphans(index, garden):
    conditions = (q.Condition(q.Field(q.GRAPH, q.GRAPH_RELATED), "is empty", ""),)
    assert run(index, garden, conditions=conditions) == ["lonely"]


def test_kind_is_species_finds_what_is_pointed_at(index, garden):
    conditions = (q.Condition(q.Field(q.GRAPH, q.GRAPH_KIND), "includes", "species"),)
    assert run(index, garden, conditions=conditions) == ["task"]


def test_instance_of_matches_the_species_by_name(index, garden):
    conditions = (q.Condition(q.Field(q.GRAPH, q.GRAPH_INSTANCE_OF), "includes", "Task"),)
    assert run(index, garden, conditions=conditions) == ["schema", "backfill", "rehearsal"]


def test_excludes_is_the_negation_of_includes(index, garden):
    conditions = (q.Condition(q.Field(q.GRAPH, q.GRAPH_INSTANCE_OF), "excludes", "Task"),)
    assert run(index, garden, conditions=conditions) == ["task", "tiering", "lonely"]


# ---- ordering ----


def test_sort_puts_empty_values_last_in_both_directions(index, garden):
    ascending = run(index, garden, sort=q.Sort(prop("estimate"), q.ASC))
    descending = run(index, garden, sort=q.Sort(prop("estimate"), q.DESC))
    assert ascending[:3] == ["backfill", "rehearsal", "schema"]
    assert descending[:3] == ["schema", "rehearsal", "backfill"]
    assert set(ascending[3:]) == set(descending[3:]) == {"task", "tiering", "lonely"}


def test_a_column_holding_numbers_and_text_sorts_without_raising(index):
    motes = [mote("a", size="large"), mote("b", size=2), mote("c", size="10")]
    index = q.Index.of(motes, [])
    rows = q.evaluate(index, motes, q.Query(sort=q.Sort(prop("size"), q.ASC)))
    assert ids(rows) == ["b", "c", "a"]


# ---- grouping ----


def test_grouping_cuts_the_rows_and_sends_unset_to_the_end(index, garden):
    rows = q.evaluate(index, garden[0], q.Query())
    groups = q.group_rows(index, rows, prop("status"))
    assert [(g.key, ids(g.motes)) for g in groups] == [
        ("blocked", ["tiering"]),
        ("doing", ["schema", "backfill"]),
        ("todo", ["rehearsal"]),
        (q.UNSET, ["task", "lonely"]),
    ]


def test_no_grouping_is_one_unlabelled_group(index, garden):
    groups = q.group_rows(index, garden[0], None)
    assert len(groups) == 1
    assert groups[0].label == ""
    assert ids(groups[0].motes) == ids(garden[0])


def test_a_row_appears_under_exactly_one_header(index, garden):
    """A list-valued group field groups by the whole value. Two headers
    holding the same row would double it in the table, and the
    selection maths behind shift-click and bulk edits counts rows."""
    rows = q.evaluate(index, garden[0], q.Query())
    groups = q.group_rows(index, rows, q.Field(q.GRAPH, q.GRAPH_RELATED))
    seen = [m.id for g in groups for m in g.motes]
    assert sorted(seen) == sorted(ids(garden[0]))


# ---- serialization ----


def test_a_query_round_trips_through_json():
    original = q.Query(
        join=q.OR,
        conditions=(q.Condition(prop("status"), "is", "doing"),),
        sort=q.Sort(q.Field(q.BUILTIN, q.BUILTIN_UPDATED), q.DESC),
        group=q.Field(q.REL, "depends_on", q.IN),
        columns=(prop("status"), q.Field(q.GRAPH, q.GRAPH_KIND)),
    )
    assert q.Query.from_json(original.to_json()) == original


def test_a_hand_edited_query_comes_back_narrower_not_broken():
    """The property is editable from the CLI, from MCP and from Monarch's
    detail panel, so Monolith has to open whatever it finds there."""
    parsed = q.Query.from_json({
        "join": "maybe",
        "conditions": [
            {"field": {"source": "property", "key": "status"}, "op": "is", "value": "doing"},
            {"field": {"source": "nonsense", "key": "x"}, "op": "is"},
            {"field": {"source": "property", "key": "y"}, "op": "resembles"},
            "not even a dict",
        ],
        "sort": {"field": {"source": "builtin", "key": "name"}, "direction": "sideways"},
        "columns": None,
    })
    assert parsed.join == q.AND
    assert parsed.conditions == (q.Condition(prop("status"), "is", "doing"),)
    assert parsed.sort == q.Sort(q.Field(q.BUILTIN, q.BUILTIN_NAME), q.ASC)
    assert parsed.columns == ()


def test_has_query_recognizes_a_saved_view():
    assert q.has_query({q.QUERY_KEY: {}})
    assert not q.has_query({"name": "Not a view"})


# ---- rendering ----


def test_text_of_names_the_motes_a_rel_column_holds(index, garden):
    backfill = garden[0][2]
    value = q.value_of(index, backfill, q.Field(q.REL, "depends_on"))
    assert q.text_of(index, value) == "Schema translation"


def test_text_of_leaves_an_empty_value_empty(index):
    assert q.text_of(index, None) == ""
    assert q.text_of(index, []) == ""

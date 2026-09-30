"""core/table.py -- a garden shaped as a table.

What earns a test here is the part a query language alone cannot answer:
which columns a garden *discovers*, what kind each one turns out to hold,
and the one step that needs both a schema and a clock (which operator a
condition means, and what date `7d` names). The predicates themselves are
tests/test_query.py's.

Ported from tests/test_monolith_api.py, which asked the same questions of
the window that used to own this code.
"""
import pytest

from core import query as q
from core import table as table_repo
from core.motes import create_mote, update_mote
from core.relationships import create_relationship
from core.table import Schema, answer, read, resolve_date, resolve_query, table


@pytest.fixture
def garden(conn) -> dict:
    """A species, three of its instances and a relationship between two of
    them -- enough for a property column, a rel column and a kind."""
    species = create_mote(conn, {"name": "Task"})
    ids = {"species": species}
    for name, status in (("Schema", "doing"), ("Backfill", "doing"), ("Rehearsal", "todo")):
        ids[name.lower()] = create_mote(conn, {"name": name, "status": status, "estimate": 3})
        create_relationship(conn, ids[name.lower()], species, q.INSTANCE_OF)
    create_relationship(conn, ids["backfill"], ids["schema"], "depends_on")
    return ids


def schema_of(conn) -> Schema:
    motes, relationships, index = read(conn)
    return Schema.of(index, motes, relationships)


def column_keys(payload) -> list[str]:
    return [c["key"] for c in payload["schema"]["columns"]]


def row_ids(payload) -> list[str]:
    return [row["id"] for row in payload["rows"]]


# ---- what a garden's columns are ----

def test_columns_are_discovered_from_what_the_garden_holds(conn, garden):
    keys = column_keys(table(conn))
    assert "property:status" in keys
    assert "rel:out:depends_on" in keys
    assert "rel:in:depends_on" in keys
    assert "builtin:updated" in keys
    assert "graph:kind" in keys


def test_a_property_column_offers_its_distinct_values_as_options(conn, garden):
    status = next(c for c in table(conn)["schema"]["columns"] if c["key"] == "property:status")
    assert status["values"] == ["doing", "todo"]
    assert status["kind"] == q.TEXT
    assert status["editable"]


def test_a_numeric_column_is_recognized_and_offered_numeric_operators(conn, garden):
    estimate = next(c for c in table(conn)["schema"]["columns"] if c["key"] == "property:estimate")
    assert estimate["kind"] == q.NUMBER
    assert ">" in estimate["ops"]


def test_the_graphs_own_columns_are_never_editable(conn, garden):
    columns = {c["key"]: c for c in table(conn)["schema"]["columns"]}
    assert not columns["graph:kind"]["editable"]
    assert not columns["builtin:degree"]["editable"]
    assert columns["rel:out:depends_on"]["editable"]


def test_updated_is_a_date_column_not_prose(conn, garden):
    """So `updated:-7d` means the last week rather than comparing a
    timestamp as text against the literal string "-7d"."""
    updated = next(c for c in table(conn)["schema"]["columns"] if c["key"] == "builtin:updated")
    assert updated["kind"] == q.DATE
    assert "after" in updated["ops"]


def test_a_garden_nobody_configured_opens_on_a_usable_table(conn, garden):
    payload = table(conn)
    keys = [c["key"] for c in payload["columns"]]
    assert keys[0] == "builtin:name"
    assert "property:status" in keys
    assert len(row_ids(payload)) == 4


def test_a_column_the_garden_lost_is_invented_rather_than_raising(conn, garden):
    """A saved view outlives the key it filters on."""
    payload = table(conn, q.Query(columns=(q.Field(q.PROPERTY, "gone"),)))
    assert payload["columns"][0]["key"] == "property:gone"
    assert all(row["cells"][0]["empty"] for row in payload["rows"])
    assert any("gone" in problem for problem in payload["problems"])


# ---- querying ----

def test_a_condition_narrows_the_rows_but_not_the_total(conn, garden):
    payload = table(conn, q.Query(
        conditions=(q.Condition(q.Field(q.PROPERTY, "status"), "is", "doing"),),
    ))
    assert payload["matched"] == 2
    assert payload["total"] == 4
    assert set(row_ids(payload)) == {garden["schema"], garden["backfill"]}


def test_grouping_cuts_the_same_rows_into_labelled_groups(conn, garden):
    payload = table(conn, q.Query(group=q.Field(q.PROPERTY, "status")))
    assert [(g["label"], g["count"]) for g in payload["groups"]] == [
        ("doing", 2), ("todo", 1), (q.UNSET, 1),
    ]


def test_rows_and_groups_are_the_same_rows(conn, garden):
    """Rendered once and referenced twice, so a cell cannot read one way
    in the flat list and another under a header."""
    payload = table(conn, q.Query(group=q.Field(q.PROPERTY, "status")))
    grouped = [row["id"] for g in payload["groups"] for row in g["rows"]]
    assert sorted(grouped) == sorted(row_ids(payload))


def test_a_rel_cell_reads_the_neighbours_name(conn, garden):
    payload = table(conn, q.Query(columns=(q.Field(q.REL, "depends_on", q.OUT),)))
    cells = {row["id"]: row["cells"][0] for row in payload["rows"]}
    assert cells[garden["backfill"]]["text"] == "Schema"
    assert cells[garden["backfill"]]["ids"] == [garden["schema"]]
    assert cells[garden["rehearsal"]]["empty"]


def test_species_ids_ride_along_with_the_answer(conn, garden):
    assert table(conn)["speciesIds"] == [garden["species"]]


# ---- resolving a condition's operator against the discovered kind ----

def test_an_is_condition_becomes_a_numeric_equals_on_a_number_column(conn, garden):
    resolved, problems = resolve_query(
        q.Query(conditions=(q.Condition(q.Field(q.PROPERTY, "estimate"), "is", "3"),)),
        schema_of(conn),
    )
    assert resolved.conditions[0].op == "="
    assert problems == []


def test_the_same_condition_stays_text_on_a_text_column(conn, garden):
    resolved, _ = resolve_query(
        q.Query(conditions=(q.Condition(q.Field(q.PROPERTY, "status"), "is", "doing"),)),
        schema_of(conn),
    )
    assert resolved.conditions[0].op == "is"


def test_an_is_condition_becomes_includes_on_a_list_column(conn, garden):
    resolved, _ = resolve_query(
        q.Query(conditions=(q.Condition(q.Field(q.GRAPH, q.GRAPH_KIND), "is", "species"),)),
        schema_of(conn),
    )
    assert resolved.conditions[0].op == "includes"


def test_a_condition_a_column_cannot_answer_is_dropped_and_reported(conn, garden):
    """`kind:>x` asks a list of kinds to be greater than something. There
    is no honest answer, so the condition goes and the problem is said out
    loud rather than the filter quietly meaning something else."""
    resolved, problems = resolve_query(
        q.Query(conditions=(q.Condition(q.Field(q.GRAPH, q.GRAPH_KIND), "after", "x"),)),
        schema_of(conn),
    )
    assert resolved.conditions == ()
    assert len(problems) == 1
    assert "says nothing" in problems[0]


def test_the_kind_is_decided_when_the_query_runs_not_when_it_was_written(conn, garden):
    """The whole reason resolution lives here. The same saved query means a
    lexical comparison while `estimate` holds prose and a numeric one once
    it holds numbers."""
    for mote_id in ("schema", "backfill", "rehearsal"):
        update_mote(conn, garden[mote_id], {"estimate": "a while"})
    written = q.Query(conditions=(q.Condition(q.Field(q.PROPERTY, "estimate"), ">", "3"),))
    assert resolve_query(written, schema_of(conn))[0].conditions[0].op == "after"

    for mote_id in ("schema", "backfill", "rehearsal"):
        update_mote(conn, garden[mote_id], {"estimate": 5})
    assert resolve_query(written, schema_of(conn))[0].conditions[0].op == ">"


# ---- relative dates ----

def test_a_relative_date_resolves_against_today(conn):
    assert resolve_date("today", today="2026-09-30") == "2026-09-30"
    assert resolve_date("7d", today="2026-09-30") == "2026-10-07"
    assert resolve_date("-3d", today="2026-09-30") == "2026-09-27"
    assert resolve_date("2w", today="2026-09-30") == "2026-10-14"
    assert resolve_date("1m", today="2026-09-30") == "2026-10-30"
    assert resolve_date("-1y", today="2026-09-30") == "2025-09-30"


def test_an_absolute_or_unrecognized_date_is_left_alone(conn):
    assert resolve_date("2026-10-01", today="2026-09-30") == "2026-10-01"
    assert resolve_date("soon", today="2026-09-30") == "soon"
    assert resolve_date("", today="2026-09-30") == ""


def test_a_relative_date_is_only_resolved_on_a_date_column(conn, garden):
    """`status:7d` is somebody looking for the literal text, not for next
    week: only a column that holds dates gets the clock applied to it."""
    resolved, _ = resolve_query(
        q.Query(conditions=(q.Condition(q.Field(q.PROPERTY, "status"), "is", "7d"),)),
        schema_of(conn),
        today="2026-09-30",
    )
    assert resolved.conditions[0].value == "7d"


def test_a_saved_query_never_freezes_its_relative_date(conn, garden):
    update_mote(conn, garden["schema"], {"due": "2026-10-05"})
    written = q.Query(conditions=(q.Condition(q.Field(q.PROPERTY, "due"), "before", "7d"),))
    schema = schema_of(conn)
    assert resolve_query(written, schema, today="2026-09-30")[0].conditions[0].value == "2026-10-07"
    assert resolve_query(written, schema, today="2026-10-30")[0].conditions[0].value == "2026-11-06"


def test_a_date_only_value_matches_a_timed_one_on_the_same_day(conn, garden):
    """`due:2026-10-01` is a question about the day. A mote due at 09:00
    that day is an answer to it."""
    update_mote(conn, garden["schema"], {"due": "2026-10-01T09:00"})
    update_mote(conn, garden["backfill"], {"due": "2026-10-02"})
    payload = table(conn, q.Query(
        conditions=(q.Condition(q.Field(q.PROPERTY, "due"), "is", "2026-10-01"),),
    ))
    assert row_ids(payload) == [garden["schema"]]


def test_a_timed_value_on_the_boundary_day_is_not_after_it(conn, garden):
    update_mote(conn, garden["schema"], {"due": "2026-10-01T09:00"})
    update_mote(conn, garden["backfill"], {"due": "2026-10-02"})
    payload = table(conn, q.Query(
        conditions=(q.Condition(q.Field(q.PROPERTY, "due"), "after", "2026-10-01"),),
    ))
    assert row_ids(payload) == [garden["backfill"]]


# ---- the answer off a read a caller already holds ----

def test_answer_is_the_rows_half_of_table(conn, garden):
    motes, relationships, index = read(conn)
    schema = Schema.of(index, motes, relationships)
    the_query = table_repo.default_query(schema)
    assert answer(index, schema, the_query, motes)["matched"] == table(conn, the_query)["matched"]

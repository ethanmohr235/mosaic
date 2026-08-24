"""What a note's macros come to, against a real garden.

The counterpart to tests/test_modoc.py: that one proves the grammar,
this one proves the resolution -- which mote a reference finds, which
values a step produces, and that a preview never writes.
"""
import pytest

from core import modoc
from core.modoc_bind import DEFAULT_HORIZON, format_value, problems, resolve
from core.motes import create_mote, get_mote
from core.relationships import create_relationship


@pytest.fixture
def garden(conn):
    """A project of three tasks whose `progress` a rule advances by 10 a
    step, so every value in these tests has a knowable t+N."""
    species = create_mote(conn, {"name": "Task", "freedoms": "progress"})
    rule = create_mote(conn, {"name": "Grow", "equation": "progress == prev(progress) + 10"})
    create_relationship(conn, rule, species, "applies_to")

    project = create_mote(conn, {"name": "Atlas", "due": "2026-09-30", "team": 4})
    for index, status in enumerate(("todo", "todo", "done")):
        task = create_mote(conn, {"name": f"Task {index}", "status": status, "progress": 100})
        create_relationship(conn, task, species, "instance_of")
        create_relationship(conn, project, task, "contains")
    return project


def resolved(conn, note_id, source, **kwargs):
    return resolve(conn, note_id, modoc.parse(source), **kwargs)


def only_value(resolution):
    (result,) = resolution.values.values()
    return result


# ---- references ----

def test_a_link_resolves_by_id(conn, garden):
    resolution = resolved(conn, garden, f"\\mote{{{garden}}}")
    assert resolution.links[garden].mote_id == garden
    assert resolution.links[garden].name == "Atlas"


def test_a_link_resolves_by_unique_name(conn, garden):
    assert resolved(conn, garden, "\\mote{Atlas}").links["Atlas"].mote_id == garden


def test_a_link_resolves_by_name_case_insensitively(conn, garden):
    assert resolved(conn, garden, "\\mote{atlas}").links["atlas"].mote_id == garden


def test_an_id_is_never_shadowed_by_a_name(conn, garden):
    decoy = create_mote(conn, {"name": garden})
    assert resolved(conn, garden, f"\\mote{{{garden}}}").links[garden].mote_id == garden
    assert decoy


def test_a_duplicated_name_is_ambiguous_rather_than_arbitrary(conn, garden):
    create_mote(conn, {"name": "Atlas"})
    target = resolved(conn, garden, "\\mote{Atlas}").links["Atlas"]
    assert target.mote_id is None
    assert "link by id" in target.error


def test_an_unresolved_link_reports_rather_than_vanishing(conn, garden):
    target = resolved(conn, garden, "\\mote{Nowhere}").links["Nowhere"]
    assert target.mote_id is None and target.name == "Nowhere"
    assert "no mote" in target.error


def test_a_link_carries_the_kinds_the_rest_of_the_app_uses(conn, garden):
    assert resolved(conn, garden, "\\mote{Task}").links["Task"].kinds == ["species"]


# ---- values ----

def test_a_plain_property_reads_as_itself_even_when_it_is_not_a_number(conn, garden):
    assert only_value(resolved(conn, garden, "\\val{due}")).text == "2026-09-30"


def test_an_expression_goes_through_the_formula_language(conn, garden):
    assert only_value(resolved(conn, garden, "\\val{count(targets('contains'))}")).text == "3"


def test_a_value_can_be_read_off_another_mote(conn, garden):
    assert only_value(resolved(conn, garden, "\\val[Task 0]{progress}")).text == "100"


def test_a_value_anchored_at_a_missing_mote_reports(conn, garden):
    result = only_value(resolved(conn, garden, "\\val[Nowhere]{progress}"))
    assert result.value is None and "no mote" in result.error


def test_a_bad_expression_reports_and_still_renders_something(conn, garden):
    result = only_value(resolved(conn, garden, "\\val{nonesuch}"))
    assert result.text == "?nonesuch"
    assert "no property" in result.error


def test_integral_results_lose_their_decimal_point(conn, garden):
    assert only_value(resolved(conn, garden, "\\val{sum(targets('contains').progress)}")).text == "300"


@pytest.mark.parametrize("value,expected", [
    (True, "true"), (2.0, "2"), (2.5, "2.5"), ("a", "a"), ([1, 2], "[1, 2]"),
])
def test_format_value(value, expected):
    assert format_value(value) == expected


# ---- stepping ----

def test_step_zero_is_the_garden_as_stored(conn, garden):
    resolution = resolved(conn, garden, "\\val{sum(targets('contains').progress)}")
    assert resolution.projected is False
    assert only_value(resolution).text == "300"


def test_a_later_step_projects_the_rules_forward(conn, garden):
    resolution = resolved(conn, garden, "\\val{sum(targets('contains').progress)}", step=3)
    assert resolution.projected is True
    assert only_value(resolution).text == "390"


def test_t_is_readable_as_the_step_itself(conn, garden):
    assert only_value(resolved(conn, garden, "\\val{t}", step=2)).text == "2"


def test_a_preview_never_writes_to_the_graph(conn, garden):
    before = {m: get_mote(conn, m).properties for m in (garden,)}
    resolved(conn, garden, "\\val{sum(targets('contains').progress)}", step=5)
    tasks = [m for m in conn.execute("SELECT id FROM motes").fetchall()]
    assert {m: get_mote(conn, m).properties for m in (garden,)} == before
    assert all(
        get_mote(conn, row["id"]).properties.get("progress") in (None, 100) for row in tasks
    )


# ---- the anchor ----

def test_an_about_relationship_moves_where_values_are_read_from(conn, garden):
    note = create_mote(conn, {modoc.MODOC_KEY: "\\val{due}"})
    create_relationship(conn, note, garden, modoc.ABOUT)
    resolution = resolved(conn, note, "\\val{due}")
    assert resolution.anchor_id == garden
    assert only_value(resolution).text == "2026-09-30"


def test_an_ambiguous_anchor_surfaces_as_a_diagnostic(conn, garden):
    note = create_mote(conn, {modoc.MODOC_KEY: "x"})
    create_relationship(conn, note, garden, modoc.ABOUT)
    create_relationship(conn, note, create_mote(conn, {"name": "Other"}), modoc.ABOUT)
    resolution = resolved(conn, note, "text")
    assert resolution.anchor_id == note
    assert [d.kind for d in resolution.diagnostics] == ["ambiguous_anchor"]


# ---- graphs ----

def test_a_by_graph_counts_motes_grouped_by_a_property(conn, garden):
    (series,) = resolved(conn, garden, "\\graph{targets('contains') by status}").graphs.values()
    assert (series.labels, series.values) == (["todo", "done"], [2.0, 1.0])


def test_a_by_graph_over_a_non_selection_reports(conn, garden):
    (series,) = resolved(conn, garden, "\\graph{team by status}").graphs.values()
    assert "does not select motes" in series.error


def test_an_over_t_graph_plots_one_point_per_step_to_the_horizon(conn, garden):
    source = "\\graph{sum(targets('contains').progress) over t}"
    (series,) = resolved(conn, garden, source).graphs.values()
    assert series.labels == [f"t+{i}" for i in range(DEFAULT_HORIZON + 1)]
    assert series.values[:3] == [300.0, 330.0, 360.0]


def test_an_over_t_graph_reaches_at_least_as_far_as_the_current_step(conn, garden):
    source = "\\graph{sum(targets('contains').progress) over t}"
    (series,) = resolved(conn, garden, source, step=12, horizon=2).graphs.values()
    assert series.labels[-1] == "t+12"


def test_an_unreadable_graph_spec_resolves_to_an_error_not_a_crash(conn, garden):
    (series,) = resolved(conn, garden, "\\graph{nonsense}").graphs.values()
    assert series.error is not None


# ---- shared resolution ----

def test_a_repeated_macro_is_resolved_once(conn, garden):
    resolution = resolved(conn, garden, "\\val{due} and again \\val{due}")
    assert len(resolution.values) == 1


# ---- problems ----

def test_problems_gathers_parse_and_resolution_failures_together(conn, garden):
    document = modoc.parse("\\bogus\n\n\\mote{Nowhere} \\val{nonesuch}\n\n\\graph{nonsense}")
    found = problems(document, resolve(conn, garden, document))
    assert [d.kind for d in found] == [
        "unknown_command", "unresolved_link", "unresolved_value", "bad_graph", "unresolved_graph",
    ]


def test_problems_is_empty_for_a_note_that_fully_resolves(conn, garden):
    document = modoc.parse("Due \\val{due} for \\mote{Atlas}.")
    assert problems(document, resolve(conn, garden, document)) == []


def test_a_reference_that_fails_twice_is_reported_at_both_lines(conn, garden):
    document = modoc.parse("\\mote{Nowhere}\n\n\\mote{Nowhere}")
    assert [d.line for d in problems(document, resolve(conn, garden, document))] == [1, 3]

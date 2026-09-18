"""What a note's macros come to, against a real garden.

The counterpart to tests/test_modoc.py: that one proves the grammar,
this one proves the resolution -- which mote a reference finds, which
values a step produces, and that a preview never writes.
"""
import pytest

from core import modoc
from core.modoc_bind import format_value, problems, resolve
from core.motes import create_mote, get_mote, update_mote
from core.relationships import create_relationship
from core.sweep import PLOT_KEY


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


def test_a_plain_property_with_a_declared_unit_shows_it(conn, garden):
    update_mote(conn, garden, {"team": 4, "units": {"team": "kohm"}})
    assert only_value(resolved(conn, garden, "\\val{team}")).text == "4 kohm"


def test_a_unitless_property_shows_no_unit(conn, garden):
    assert only_value(resolved(conn, garden, "\\val{team}")).text == "4"


def test_a_declared_unit_of_1_shows_no_unit(conn, garden):
    # units: {"team": "1"} tells the solver the property is truly
    # dimensionless (core/units.py), not that its unit symbol is "1".
    update_mote(conn, garden, {"team": 4, "units": {"team": "1"}})
    assert only_value(resolved(conn, garden, "\\val{team}")).text == "4"


def test_a_caret_notation_unit_renders_with_superscripts(conn, garden):
    update_mote(conn, garden, {"density": 2, "units": {"density": "cm^-3"}})
    assert only_value(resolved(conn, garden, "\\val{density}")).text == "2 cm⁻³"


def test_a_dotted_reference_reads_a_plain_property_in_its_own_unit(conn, garden):
    # `n.E` must not go through the formula evaluator's base-SI
    # normalization -- a value declared in eV has to read back as eV,
    # not as its (tiny) value in joules.
    neutron = create_mote(conn, {"name": "n", "E": 1, "units": {"E": "eV"}})
    create_relationship(conn, garden, neutron, "about")
    assert only_value(resolved(conn, garden, "\\val{n.E}")).text == "1 eV"


def test_a_dotted_reference_falls_back_to_the_formula_language(conn, garden):
    # No mote named "count" exists, so this isn't the fast path -- it
    # still has to reach the formula language's own dotted syntax
    # (targets('contains').status is unrelated here, but a role/alias
    # dotted reference must keep working).
    result = only_value(resolved(conn, garden, "\\val{Nowhere.progress}"))
    assert result.value is None and result.error


def test_large_and_tiny_numbers_render_in_scientific_notation(conn, garden):
    update_mote(conn, garden, {"team": 199999999999999.97})
    assert only_value(resolved(conn, garden, "\\val{team}")).text == "2e14"


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


# ---- one reading, and no clock ----

def test_a_note_reads_the_garden_as_it_stands(conn, garden):
    resolution = resolved(conn, garden, "\\val{sum(targets('contains').progress)}")
    assert only_value(resolution).text == "300"


def test_there_is_no_step_to_resolve_at(conn, garden):
    """A note says what the garden *is*. Running it forward was a `t+N`
    preview that lived here and in Mora's footer; both are gone, and the
    question belongs to Motion. `resolve()` therefore takes no step, and
    a Resolution has nothing to report about one."""
    with pytest.raises(TypeError):
        resolved(conn, garden, "\\val{due}", step=3)
    assert not hasattr(resolved(conn, garden, "\\val{due}"), "projected")


def test_t_reads_as_zero_because_a_note_has_no_clock(conn, garden):
    assert only_value(resolved(conn, garden, "\\val{t}")).text == "0"


def test_resolving_a_note_never_writes_to_the_graph(conn, garden):
    before = {row["id"]: get_mote(conn, row["id"]).properties
              for row in conn.execute("SELECT id FROM motes").fetchall()}
    resolved(conn, garden, "\\val{sum(targets('contains').progress)} \\val{t}")
    assert {row["id"]: get_mote(conn, row["id"]).properties
            for row in conn.execute("SELECT id FROM motes").fetchall()} == before


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
#
# `\graph{...}` names a view saved in Moxie. What it *draws* is
# tests/test_plotview.py's subject; what is checked here is only that a
# note finds the right one, and that a note naming a view that isn't
# there still renders.

@pytest.fixture
def view(conn, garden):
    """A saved plot over the same garden. What it draws is
    tests/test_plotview.py's subject -- this garden's one rule reads
    `prev(progress)`, which no sweep can answer, so the figures here are
    about which view a name finds rather than about its curve."""
    return create_mote(conn, {"name": "Progress curve", PLOT_KEY: {
        "mode": "sweep",
        "x": {"mote": garden, "key": "team", "lo": 1.0, "hi": 8.0, "steps": 7},
        "series": [{"mote": garden, "key": "team", "label": "team"}],
    }})


def test_a_graph_finds_the_view_it_names(conn, garden, view):
    (figure,) = resolved(conn, garden, "\\graph{Progress curve}").graphs.values()
    assert figure.view_id == view
    assert figure.name == "Progress curve"


def test_a_graph_resolves_a_view_by_id_as_well_as_by_name(conn, garden, view):
    (figure,) = resolved(conn, garden, f"\\graph{{{view}}}").graphs.values()
    assert figure.view_id == view


def test_a_graph_naming_no_saved_view_resolves_to_an_error_not_a_crash(conn, garden):
    (figure,) = resolved(conn, garden, "\\graph{nonsense}").graphs.values()
    assert "no saved view" in figure.error


def test_a_graph_naming_a_mote_that_carries_no_view_says_so(conn, garden):
    """"That mote has no view on it" is a different fix from "no such
    mote", so an id that resolves is told the first rather than the
    second."""
    (figure,) = resolved(conn, garden, f"\\graph{{{garden}}}").graphs.values()
    assert "carries no saved view" in figure.error


def test_one_view_named_twice_is_drawn_once(conn, garden, view):
    """A sweep is the most expensive thing a note can ask for, so two
    figures of the same view share one resolution."""
    resolution = resolved(
        conn, garden, "\\graph{Progress curve}\n\n\\graph{Progress curve}"
    )
    assert len(resolution.graphs) == 1


def test_a_note_full_of_figures_runs_no_simulation(conn, garden, view):
    """A `\\graph` varies a property, not the clock. This garden's one
    rule reads `prev(progress)`, which no simulation-free read can
    answer -- so a clean resolution is also proof that resolving a note
    runs no simulation at all."""
    assert resolved(conn, garden, "\\graph{Progress curve}").diagnostics == []


# ---- shared resolution ----

def test_a_repeated_macro_is_resolved_once(conn, garden):
    resolution = resolved(conn, garden, "\\val{due} and again \\val{due}")
    assert len(resolution.values) == 1


# ---- problems ----

def test_problems_gathers_parse_and_resolution_failures_together(conn, garden):
    document = modoc.parse("\\bogus\n\n\\mote{Nowhere} \\val{nonesuch}\n\n\\graph{}")
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

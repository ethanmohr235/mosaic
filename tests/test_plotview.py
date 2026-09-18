"""Saved Moxie views, re-solved somewhere that is not Moxie.

The gardens here are small enough to have exact answers, the same
bargain tests/test_sweep.py strikes: `divider` is a series circuit whose
current is voltage/300, so a figure over it can be checked point by
point, which is the only way to tell "it re-solved the view" apart from
"it drew a line".

What a figure must *not* do gets as much room as what it must. It never
writes, it never stores the points it drew, and it never raises -- a
broken specification is an error on a Figure, because a note with one
bad figure in it has to render the other nineteen.
"""
import pytest

from core import plotview
from core.motes import create_mote, get_mote, update_mote
from core.relationships import create_relationship
from core.rules import APPLIES_TO, FREEDOMS_KEY
from core.scenarios import OVERRIDES, VARIES
from core.species import set_species
from core.sweep import PLOT_KEY


@pytest.fixture
def divider(conn):
    """A source across two resistances in series: current == voltage /
    (r_a + r_b), with r_a + r_b = 300."""
    species = create_mote(conn, {"name": "Divider", FREEDOMS_KEY: "current"})
    rule = create_mote(conn, {"name": "series", "equation": "current * (r_a + r_b) == voltage"})
    create_relationship(conn, rule, species, APPLIES_TO)
    circuit = create_mote(conn, {
        "name": "C1", "voltage": 10, "r_a": 100, "r_b": 200,
        "units": {"voltage": "V", "current": "A"},
    })
    set_species(conn, circuit, species)
    return circuit


def save_view(conn, name, plot):
    return create_mote(conn, {"name": name, PLOT_KEY: plot})


def sweep_of(circuit, **overrides):
    plot = {
        "mode": "sweep",
        "x": {"mote": circuit, "key": "voltage", "lo": 0.0, "hi": 300.0, "steps": 200},
        "series": [{"mote": circuit, "key": "current", "label": "C1.current"}],
    }
    plot.update(overrides)
    return plot


def solved_points(figure, index=0):
    return [p for p in figure.series[index].points if p is not None]


# ---- finding a view ----

def test_a_view_is_found_by_name_and_by_id(conn, divider):
    view = save_view(conn, "Load curve", sweep_of(divider))
    assert plotview.figure(conn, "Load curve").view_id == view
    assert plotview.figure(conn, view).view_id == view


def test_a_name_is_matched_case_insensitively(conn, divider):
    view = save_view(conn, "Load curve", sweep_of(divider))
    assert plotview.figure(conn, "load CURVE").view_id == view


def test_only_motes_carrying_a_view_are_candidates(conn, divider):
    """An ordinary mote sharing a view's name is not an ambiguity. A
    `\\graph` is asking for a picture, so nothing without one competes."""
    create_mote(conn, {"name": "Load curve", "note": "not a plot"})
    view = save_view(conn, "Load curve", sweep_of(divider))
    assert plotview.figure(conn, "Load curve").view_id == view


def test_two_views_of_one_name_ask_for_an_id(conn, divider):
    save_view(conn, "Load curve", sweep_of(divider))
    save_view(conn, "Load curve", sweep_of(divider))
    assert "reference one by id" in plotview.figure(conn, "Load curve").error


def test_a_missing_view_is_an_error_not_an_exception(conn, divider):
    assert "no saved view" in plotview.figure(conn, "nothing").error


def test_a_mote_that_carries_no_view_says_which_problem_it_is(conn, divider):
    assert "carries no saved view" in plotview.figure(conn, divider).error


# ---- the curve ----

def test_every_point_of_a_figure_is_a_real_solve(conn, divider):
    figure = plotview.figure(conn, save_view(conn, "Load", sweep_of(divider)))
    assert figure.mode == "sweep"
    assert figure.error is None
    for x, y in solved_points(figure):
        assert y == pytest.approx(x / 300)


def test_a_figure_samples_the_saved_range_more_coarsely_than_moxie_does(conn, divider):
    """The range is the author's and untouched; how finely it is sampled
    is this module's, because a note re-renders on every pause in typing
    and a 201-point sweep per keystroke is not a note."""
    figure = plotview.figure(conn, save_view(conn, "Load", sweep_of(divider)))
    points = solved_points(figure)
    assert len(points) <= plotview.NOTE_POINTS
    assert (points[0][0], points[-1][0]) == (0.0, 300.0)


def test_a_figure_stores_nothing_and_writes_nothing(conn, divider):
    before = dict(get_mote(conn, divider).properties)
    view = save_view(conn, "Load", sweep_of(divider))
    plotview.figure(conn, view)
    assert get_mote(conn, divider).properties == before
    # The points are not written back onto the view either: what makes a
    # saved view worth referencing is that it re-solves, and a plot that
    # cached its curve would go stale exactly like a pasted number.
    assert set(get_mote(conn, view).properties) == {"name", PLOT_KEY}


def test_a_figure_follows_the_garden_rather_than_the_afternoon_it_was_saved(conn, divider):
    view = save_view(conn, "Load", sweep_of(divider))
    first = solved_points(plotview.figure(conn, view))[-1][1]
    update_mote(conn, divider, {"r_b": 700})
    second = solved_points(plotview.figure(conn, view))[-1][1]
    assert first == pytest.approx(300 / 300)
    assert second == pytest.approx(300 / 800)


def test_axis_and_band_labels_carry_the_declared_units(conn, divider):
    figure = plotview.figure(conn, save_view(conn, "Load", sweep_of(divider)))
    assert figure.x_label == "C1.voltage (V)"
    assert figure.bands[0].label == "current (A)"


def test_a_point_that_did_not_solve_stays_a_hole(conn, divider):
    """A dense list would make a renderer draw straight through a range
    the model has no answer for -- a lie the eye believes completely."""
    update_mote(conn, divider, {"r_a": 0, "r_b": 0})
    figure = plotview.figure(conn, save_view(conn, "Load", sweep_of(divider)))
    assert figure.series[0].points
    assert all(point is None for point in figure.series[0].points)
    assert figure.error == "nothing solved across this range"


# ---- what the view says ----

def test_two_series_of_one_case_cost_one_solve_a_point(conn, divider):
    """Cases are what a sweep costs; series are what it shows. Two curves
    off the same what-if share the solve, which is the whole reason
    core/sweep.py takes cases rather than series."""
    plot = sweep_of(divider)
    plot["series"].append({"mote": divider, "key": "r_a", "label": "C1.r_a"})
    figure = plotview.figure(conn, save_view(conn, "Two", plot))
    assert len(figure.series) == 2
    assert figure.solves == len(figure.series[0].points)


def test_series_measuring_different_things_get_different_bands(conn, divider):
    plot = sweep_of(divider)
    plot["series"].append({"mote": divider, "key": "r_a", "label": "C1.r_a"})
    update_mote(conn, divider, {"units": {"voltage": "V", "current": "A", "r_a": "ohm"}})
    figure = plotview.figure(conn, save_view(conn, "Two", plot))
    assert len(figure.bands) == 2
    assert [s.band for s in figure.series] == [0, 1]


def test_a_saved_knob_is_applied_and_never_written(conn, divider):
    plot = sweep_of(divider, knobs=[{"mote": divider, "key": "r_b", "value": 700}])
    figure = plotview.figure(conn, save_view(conn, "Knobbed", plot))
    assert solved_points(figure)[-1][1] == pytest.approx(300 / 800)
    assert get_mote(conn, divider).properties["r_b"] == 200


def test_a_saved_scenario_is_the_baseline_it_was_saved_as(conn, divider):
    override = create_mote(conn, {"name": "bigger r_b", "r_b": 700})
    create_relationship(conn, override, divider, OVERRIDES)
    scenario = create_mote(conn, {"name": "Bigger"})
    create_relationship(conn, scenario, override, VARIES)
    plot = sweep_of(divider)
    plot["series"][0]["scenario"] = scenario
    figure = plotview.figure(conn, save_view(conn, "Scenario", plot))
    assert solved_points(figure)[-1][1] == pytest.approx(300 / 800)


# ---- the other two modes ----

def test_a_saved_surface_comes_back_as_a_grid(conn, divider):
    view = save_view(conn, "Grid", {
        "mode": "surface",
        "x": {"mote": divider, "key": "voltage", "lo": 0.0, "hi": 10.0, "steps": 200},
        "y": {"mote": divider, "key": "r_a", "lo": 100.0, "hi": 200.0, "steps": 200},
        "output": {"mote": divider, "key": "current"},
    })
    figure = plotview.figure(conn, view)
    assert figure.mode == "surface"
    assert figure.error is None
    assert figure.rows == figure.cols == plotview.NOTE_SURFACE_POINTS
    assert len(figure.cells) == figure.rows * figure.cols


def test_a_saved_correlation_comes_back_as_dots(conn, divider):
    view = save_view(conn, "Scatter", {
        "mode": "correlate", "xKey": "r_a", "yKey": "r_b",
    })
    figure = plotview.figure(conn, view)
    assert figure.mode == "correlate"
    assert figure.dots == [{"id": divider, "name": "C1", "x": 100.0, "y": 200.0}]


# ---- nothing raises ----

@pytest.mark.parametrize("plot, expected", [
    ({"mode": "sweep"}, "no axis"),
    ({"mode": "sweep", "x": {"mote": "nope", "key": "k", "lo": 0, "hi": 1}}, "no axis"),
    ({"mode": "flying"}, "unknown plot mode"),
    ({"mode": "correlate"}, "no two property names"),
    ({"mode": "surface"}, "no two axes"),
])
def test_an_unreadable_specification_is_an_error_on_the_figure(conn, divider, plot, expected):
    assert expected in plotview.figure(conn, save_view(conn, "Broken", plot)).error


def test_a_view_whose_plot_property_is_not_an_object_still_resolves(conn, divider):
    view = create_mote(conn, {"name": "Odd", PLOT_KEY: "a string"})
    assert "unreadable" in plotview.figure(conn, view).error

"""`plot … vs t`: time as the x axis of a sweep (core/plotcmd.py, core/sweep.py).

The tank's level is (t + 1) * inflow -- level == prev(level) + inflow from
zero -- so every drawn point is an exact number, and `sim` over the same
garden is the reference: a plot over time is the same run, drawn."""
import pytest

from core import motion, plotcmd, plotexport, plotview
from core.command_bar import CommandError
from core.knobs import KnobSpec
from core.motes import create_mote, get_mote, list_motes
from core.relationships import create_relationship
from core.rules import APPLIES_TO, FREEDOMS_KEY
from core.species import set_species
from core.sweep import Axis, Case, run_sweep


@pytest.fixture
def tank(conn):
    species = create_mote(conn, {"name": "Tank", FREEDOMS_KEY: "level"})
    rule = create_mote(conn, {"name": "fill", "equation": "level == prev(level) + inflow"})
    create_relationship(conn, rule, species, APPLIES_TO)
    t1 = create_mote(conn, {"name": "T1", "level": 0, "inflow": 2, "units": {"level": "m"}})
    set_species(conn, t1, species)
    return t1


def line(text):
    return plotcmd.parse_text(text).line


def points_of(figure, index=0):
    return [p for p in figure.series[index].points]


def draw(conn, text):
    resolved = plotcmd.resolve(conn, line(text))
    return plotview.figure_from_spec(conn, resolved.spec, max_points=500), resolved


# ---- the grammar ----

def test_time_is_an_x_axis_with_whole_tick_ranges():
    parsed = line("plot T1.level vs t 0:10")
    assert parsed.x == "t" and parsed.range == (0.0, 10.0)
    assert line(parsed.text()).text() == parsed.text() == "plot T1.level vs t 0:10"
    assert line("plot T1.level vs t").range is None


@pytest.mark.parametrize("text", [
    "plot T1.level vs t 0:2.5",
    "plot T1.level vs t -3:5",
    "plot T1.level vs t 5:5",
    "plot T1.level vs t 0:99999",
    "plot T1.level vs t 0:10 by T1.inflow 1:2",
    "plot T1.level vs t --across",
])
def test_a_time_axis_refuses_what_is_not_whole_ticks_of_a_sweep(text):
    with pytest.raises(CommandError):
        plotcmd.parse_text(text)


def test_a_zoom_lands_on_whole_ticks():
    base = line("plot T1.level vs t 0:50")
    zoomed = plotcmd.rewrite(base, {"range": [12.3, 20.7]})
    assert zoomed.range == (12.0, 21.0) and zoomed.text() == "plot T1.level vs t 12:21"
    assert plotcmd.rewrite(base, {"range": [-4, 8]}).range == (0.0, 8.0)


def test_completion_offers_t_after_vs(conn, tank):
    assert "t" in [c.value for c in plotcmd.choices(conn, "T1.level vs ")]
    assert "t" not in [c.value for c in plotcmd.choices(conn, "T1.level ")]


# ---- the run ----

def test_a_plot_over_time_is_the_sim_run_drawn(conn, tank):
    figure, resolved = draw(conn, "plot T1.level vs t 0:5")
    assert figure.error is None and figure.x_label == "t (tick)"
    assert (figure.x_lo, figure.x_hi) == (0.0, 5.0)
    pts = points_of(figure)
    assert [p[0] for p in pts] == [0, 1, 2, 3, 4, 5]
    assert [p[1] for p in pts] == [2, 4, 6, 8, 10, 12]

    run = motion.run(conn, motion.parse_text("sim --ticks 6"))
    level = next(r for r in run.rows if r.ref == "T1.level").series
    assert [p[1] for p in pts] == list(level)


def test_a_window_is_a_slice_of_the_same_run(conn, tank):
    figure, _ = draw(conn, "plot T1.level vs t 3:6")
    assert [(p[0], p[1]) for p in points_of(figure)] == [(3, 8), (4, 10), (5, 12), (6, 14)]


def test_the_default_range_is_the_sims(conn, tank):
    figure, resolved = draw(conn, "plot T1.level vs t")
    assert (figure.x_lo, figure.x_hi) == (0.0, 50.0)
    assert len(points_of(figure)) == 51 and resolved.spec["x"]["steps"] == 50


def test_a_plot_over_time_writes_nothing(conn, tank):
    before = {m.id: dict(m.properties) for m in list_motes(conn)}
    draw(conn, "plot T1.level vs t 0:5 --set T1.inflow=3")
    assert {m.id: dict(m.properties) for m in list_motes(conn)} == before


def test_knobs_and_scenarios_move_a_run_like_any_sweep(conn, tank):
    figure, _ = draw(conn, "plot T1.level vs t 0:3 --set T1.inflow=5")
    assert [p[1] for p in points_of(figure)] == [5, 10, 15, 20]


def test_two_series_share_the_run(conn, tank):
    figure, _ = draw(conn, "plot T1.level T1.inflow vs t 0:2")
    assert [p[1] for p in points_of(figure, 0)] == [2, 4, 6]
    assert [p[1] for p in points_of(figure, 1)] == [2, 2, 2]


def test_a_failing_tick_ends_the_run_and_is_a_failed_span(conn):
    species = create_mote(conn, {"name": "Tank", FREEDOMS_KEY: "level"})
    rule = create_mote(conn, {"name": "fill", "equation": "level == 6 / (3 - t)"})
    create_relationship(conn, rule, species, APPLIES_TO)
    t1 = create_mote(conn, {"name": "T1", "level": 0})
    set_species(conn, t1, species)

    figure, _ = draw(conn, "plot T1.level vs t 0:6")
    # Ticks 0-2 are good; tick 3 divides by zero, and nothing is drawn after it.
    assert points_of(figure) == [[0.0, 2.0], [1.0, 3.0], [2.0, 6.0], None]
    assert [(s.x0, s.x1) for s in figure.failed] == [(3.0, 3.0)]
    assert "division by zero" in figure.failed[0].message


def test_run_sweep_with_a_time_axis_matches_the_series_run_step_for_step(conn, tank):
    axis = Axis.over_time(0, 8)
    result = run_sweep(conn, axis, [Case("base")], [(tank, "level")])
    got = [p.values[("base", tank, "level")] for p in result.points]
    run = motion.run(conn, motion.parse_text("sim --ticks 9"))
    assert got == list(next(r for r in run.rows if r.ref == "T1.level").series)
    assert result.solves == 9 and not result.pinned


def test_a_time_axis_needs_whole_ticks_from_zero():
    with pytest.raises(ValueError):
        Axis.over_time(3, 3)
    with pytest.raises(ValueError):
        Axis.over_time(-1, 4)


# ---- saved, exported, drawn in a note ----

def test_a_saved_time_plot_round_trips_through_its_line(conn, tank):
    resolved = plotcmd.resolve(conn, line("plot T1.level vs t 2:9 --set T1.inflow=3"))
    again = plotcmd.line_of(conn, resolved.spec)
    assert again.x == "t" and again.range == (2.0, 9.0)
    assert again.sets == (KnobSpec("T1.inflow", 3.0),)
    assert plotcmd.resolve(conn, again).spec["x"] == resolved.spec["x"]


def test_save_then_draw_by_name(conn, tank):
    command = plotcmd.parse_text("plot save Fill plot T1.level vs t 0:4")
    message, _undo = plotcmd.run_save(conn, command)
    assert "Fill" in message
    figure = plotview.figure(conn, "Fill")
    assert [p[1] for p in points_of(figure)] == [2, 4, 6, 8, 10]
    assert get_mote(conn, tank).properties["inflow"] == 2


def test_a_time_plot_exports_as_a_figure(conn, tank):
    pub = plotexport.build(conn, "plot T1.level vs t 0:4")
    assert len(pub.figure["series"][0]["points"]) == 5
    assert pub.figure["xLabel"] == "t (tick)"
    svg = plotexport.svg(pub)
    assert svg.startswith("<svg") or "<svg" in svg


def test_measured_points_over_a_time_axis_are_refused_for_now(conn, tank):
    create_mote(conn, {"name": "m", "k": 1, "v": 2})
    with pytest.raises(plotexport.PlotExportError, match="time axis"):
        plotexport.build(conn, "plot T1.level vs t 0:4 --data v vs k")

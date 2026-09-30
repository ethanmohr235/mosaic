"""The `plot` verb: its grammar, its rewrites, and the specification it
resolves to (core/plotcmd.py), plus the figure, sensitivity and save
it feeds in core/plotview.py.

`divider` is the series circuit tests/test_plotview.py uses: current is
voltage / (r_a + r_b), so every drawn point can be checked exactly."""
import pytest

from core import plotcmd, plotview
from core.command_bar import CommandError
from core.motes import create_mote, get_mote, list_motes
from core.relationships import create_relationship
from core.rules import APPLIES_TO, FREEDOMS_KEY
from core.scenarios import OVERRIDES, VARIES
from core.species import instances_of, list_species, set_species
from core.sweep import PLOT_KEY


@pytest.fixture
def divider(conn):
    species = create_mote(conn, {"name": "Divider", FREEDOMS_KEY: "current"})
    rule = create_mote(conn, {"name": "series", "equation": "current * (r_a + r_b) == voltage"})
    create_relationship(conn, rule, species, APPLIES_TO)
    circuit = create_mote(conn, {
        "name": "C1", "voltage": 10, "r_a": 100, "r_b": 200,
        "units": {"voltage": "V", "current": "A"},
    })
    set_species(conn, circuit, species)
    return circuit


def line(text):
    return plotcmd.parse_text(text).line


def draw(conn, text, points=None):
    resolved = plotcmd.resolve(conn, line(text))
    steps = (resolved.spec.get("x") or {}).get("steps", 200)
    return plotview.figure_from_spec(conn, resolved.spec, max_points=points or steps + 1)


# ---- grammar ----

@pytest.mark.parametrize("text", [
    "plot C1.current vs C1.voltage 0:300",
    "plot C1.current C1.r_a vs C1.voltage 0:300 --steps 50",
    "plot C1.current vs C1.voltage 0:300 --set C1.r_a=150 --scenario hot --log",
    "plot C1.current vs C1.voltage 0:300 by C1.r_a 50:150 --steps 10",
    "plot current vs voltage --across",
    "plot C1.current C1.r_a vs C1.voltage 0:300 --hide C1.r_a",
    "plot 'Load curve'",
    "plot",
])
def test_a_line_round_trips_through_its_text(text):
    assert line(line(text).text()).text() == line(text).text()
    assert line(text).text() == text


def test_the_parts_of_a_sweep_line():
    parsed = line("plot bd.sigma bd.tau vs bd.l 4:24 --steps 200 --set bd.E=2e11 --log")
    assert parsed.ys == ("bd.sigma", "bd.tau")
    assert parsed.x == "bd.l" and parsed.range == (4.0, 24.0)
    assert parsed.steps == 200 and parsed.log and parsed.sets == (("bd.E", 2e11),)
    assert parsed.mode == "sweep"


def test_by_makes_a_surface_and_across_a_correlation():
    assert line("plot a.z vs a.x 0:1 by a.y 0:1").mode == "surface"
    assert line("plot z vs x --across").mode == "correlate"


def test_one_word_is_a_saved_plot():
    assert line('plot "Load curve"').saved == "Load curve"
    assert line("plot #abc").saved == "#abc"


def test_negative_and_scientific_ranges():
    assert line("plot a.y vs a.x -5:1e3").range == (-5.0, 1000.0)


@pytest.mark.parametrize("text, message", [
    ("plot a.y a.z", "plot what against what"),
    ("plot vs a.x", "nothing to plot"),
    ("plot a.y vs", "needs a value to sweep"),
    ("plot a.y vs a.x 4-24", "is not a range"),
    ("plot a.y vs a.x 4:4", "two different ends"),
    ("plot a.y by a.x 0:1", "misplaced"),
    ("plot a.y a.z vs a.x 0:1 by a.w 0:1", "exactly one value"),
    ("plot a.y vs a.x 0:1 --across", "--across takes two keys"),
    ("plot a.y vs a.x --steps 0", "--steps is 1 to"),
    ("plot a.y vs a.x --set a.b", "--set takes"),
    ("plot a.y vs a.x --wat", "unknown flag"),
    ("plot a.y vs a.x --scenario", "needs a value"),
    ("plot export gif", "usage: plot export"),
    ("plot save", "usage: plot save"),
])
def test_a_bad_line_says_what_is_wrong(text, message):
    with pytest.raises(CommandError, match=message):
        plotcmd.parse_text(text)


def test_save_and_export_forms():
    save = plotcmd.parse_text('plot save "Load curve" C1.current vs C1.voltage 0:300')
    assert save.kind == plotcmd.SAVE and save.name == "Load curve"
    assert save.line.ys == ("C1.current",)
    assert plotcmd.parse_text("plot save x").line is None
    assert plotcmd.parse_text('plot save x plot "Load curve"').line.saved == "Load curve"
    export = plotcmd.parse_text("plot export csv ~/out.csv")
    assert (export.kind, export.format, export.path) == (plotcmd.EXPORT, "csv", "~/out.csv")


# ---- rewriting ----

def test_a_knob_is_added_then_moved_in_place():
    base = line("plot a.y vs a.x 0:1 --set a.b=1 --set a.c=2")
    moved = plotcmd.rewrite(base, {"set": {"ref": "a.b", "value": 5}})
    assert moved.sets == (("a.b", 5.0), ("a.c", 2.0))
    added = plotcmd.rewrite(base, {"set": {"ref": "a.d", "value": 0.25}})
    assert added.text().endswith("--set a.b=1 --set a.c=2 --set a.d=0.25")
    assert plotcmd.rewrite(base, {"unset": "a.b"}).sets == (("a.c", 2.0),)


def test_zoom_log_and_hide_rewrite_the_line():
    base = line("plot a.y a.z vs a.x 0:10")
    assert plotcmd.rewrite(base, {"range": [8, 2]}).range == (2.0, 8.0)
    assert plotcmd.rewrite(base, {"log": True}).log
    hidden = plotcmd.rewrite(base, {"hide": "a.z"})
    assert hidden.hide == ("a.z",)
    assert plotcmd.rewrite(hidden, {"hide": "a.z"}).hide == ()


def test_a_saved_line_is_not_rewritten_as_text():
    with pytest.raises(CommandError):
        plotcmd.rewrite(line('plot "Load curve"'), {"log": True})


# ---- resolving and drawing ----

def test_a_sweep_draws_the_exact_curve(conn, divider):
    figure = draw(conn, "plot C1.current vs C1.voltage 0:300 --steps 30")
    assert figure.error is None
    points = [p for p in figure.series[0].points if p]
    assert len(points) == 31
    for x, y in points:
        assert y == pytest.approx(x / 300)


def test_a_range_defaults_to_around_the_current_value(conn, divider):
    spec = plotcmd.resolve(conn, line("plot C1.current vs C1.voltage")).spec
    assert (spec["x"]["lo"], spec["x"]["hi"]) == (5.0, 20.0)


def test_a_knob_overrides_without_writing(conn, divider):
    figure = draw(conn, "plot C1.current vs C1.voltage 0:300 --steps 10 --set C1.r_a=0")
    for x, y in (p for p in figure.series[0].points if p):
        assert y == pytest.approx(x / 200)
    assert get_mote(conn, divider).properties["r_a"] == 100


def test_the_resolved_knob_has_a_range_to_drag_over(conn, divider):
    resolved = plotcmd.resolve(conn, line("plot C1.current vs C1.voltage 0:300 --set C1.r_a=400"))
    knob = resolved.knobs[0]
    assert (knob.ref, knob.value) == ("C1.r_a", 400.0)
    assert knob.lo == 50.0 and knob.hi == 400.0


def test_a_hidden_series_is_in_the_legend_but_not_solved(conn, divider):
    resolved = plotcmd.resolve(conn, line("plot C1.current C1.r_a vs C1.voltage 0:300 --hide C1.current"))
    assert [(s.id, s.ref, s.hidden) for s in resolved.series] == [
        ("s0", "C1.current", True), ("s1", "C1.r_a", False)]
    assert [s["id"] for s in resolved.spec["series"]] == ["s1"]


def test_sweeping_a_freedom_says_x_is_pinned(conn, divider):
    assert plotcmd.resolve(conn, line("plot C1.voltage vs C1.current 0:1")).x_pinned
    assert not plotcmd.resolve(conn, line("plot C1.current vs C1.voltage 0:1")).x_pinned


def test_a_surface_and_a_correlation_draw(conn, divider):
    surface = draw(conn, "plot C1.current vs C1.voltage 0:300 by C1.r_a 0:100 --steps 4", points=5)
    assert surface.mode == "surface" and surface.rows == 5 and surface.cols == 5
    other = create_mote(conn, {"name": "C2", "voltage": 20, "r_a": 100, "r_b": 200})
    set_species(conn, other, next(s.id for s in list_species(conn)))
    dots = draw(conn, "plot current vs voltage --across")
    assert dots.mode == "correlate"
    assert sorted(d["name"] for d in dots.dots) == ["C1", "C2"]


def test_a_surface_with_both_axes_on_one_mote_varies_along_both(conn, divider):
    """Regression: the point's overrides were a dict literal keyed by mote,
    so y's entry replaced x's when both axes were on C1."""
    surface = draw(conn, "plot C1.current vs C1.voltage 0:300 by C1.r_a 0:100 --steps 2", points=3)
    by_cell = {(c["x"], c["y"]): c["v"] for c in surface.cells}
    assert by_cell[(300.0, 0.0)] == pytest.approx(300 / 200)
    assert by_cell[(150.0, 100.0)] == pytest.approx(150 / 300)


def test_a_scenario_is_found_by_name(conn, divider):
    hot = create_mote(conn, {"name": "hot", OVERRIDES: {"voltage": 30}})
    create_relationship(conn, hot, divider, VARIES)
    spec = plotcmd.resolve(conn, line("plot C1.current vs C1.r_a 50:150 --scenario hot")).spec
    assert spec["series"][0]["scenario"] == hot
    with pytest.raises(CommandError, match="no scenario named 'cold'"):
        plotcmd.resolve(conn, line("plot C1.current vs C1.r_a 50:150 --scenario cold"))


@pytest.mark.parametrize("text, message", [
    ("plot C9.current vs C1.voltage 0:1", "no mote named 'C9'"),
    ("plot C1 vs C1.voltage 0:1", "names no property"),
    ("plot #nope.x vs C1.voltage 0:1", "no mote #nope"),
])
def test_a_name_the_garden_lacks_is_refused(conn, divider, text, message):
    with pytest.raises(CommandError, match=message):
        plotcmd.resolve(conn, line(text))


def test_two_motes_of_one_name_ask_for_an_id(conn, divider):
    create_mote(conn, {"name": "C1", "voltage": 1})
    with pytest.raises(CommandError, match="use #id"):
        plotcmd.resolve(conn, line("plot C1.current vs C1.voltage 0:1"))


# ---- saving ----

def test_save_writes_the_spec_as_a_saved_plot_and_undoes(conn, divider):
    command = plotcmd.parse_text('plot save "Load curve" C1.current vs C1.voltage 0:300 --set C1.r_a=50')
    message, undo_command = plotcmd.run_save(conn, command)
    assert message == "saved plot 'Load curve'"
    view = next(m for m in list_motes(conn) if m.properties.get("name") == "Load curve")
    spec = view.properties[PLOT_KEY]
    assert spec["x"]["mote"] == divider and spec["knobs"] == [{"mote": divider, "key": "r_a", "value": 50.0}]
    species = next(s for s in list_species(conn) if s.properties["name"] == "Saved Plot")
    assert [m.id for m in instances_of(conn, species.id)] == [view.id]
    # A note's \graph draws what was saved.
    assert plotview.figure(conn, "Load curve").error is None
    undo_command.undo()
    assert not plotview.list_views(list_motes(conn))


def test_saving_a_name_again_updates_that_plot(conn, divider):
    plotcmd.run_save(conn, plotcmd.parse_text("plot save L C1.current vs C1.voltage 0:300"))
    message, undo_command = plotcmd.run_save(conn, plotcmd.parse_text("plot save L C1.current vs C1.voltage 0:10"))
    assert message == "updated plot 'L'"
    views = plotview.list_views(list_motes(conn))
    assert len(views) == 1 and views[0].properties[PLOT_KEY]["x"]["hi"] == 10
    undo_command.undo()
    assert plotview.list_views(list_motes(conn))[0].properties[PLOT_KEY]["x"]["hi"] == 300


def test_a_saved_plot_opens_and_writes_back_as_a_line(conn, divider):
    plotcmd.run_save(conn, plotcmd.parse_text(
        "plot save L C1.current vs C1.voltage 0:300 --steps 20 --set C1.r_a=50 --log"))
    resolved = plotcmd.resolve(conn, line("plot L"))
    assert resolved.name == "L" and resolved.knobs[0].ref == "C1.r_a"
    assert plotcmd.line_of(conn, resolved.spec).text() == \
        "plot C1.current vs C1.voltage 0:300 --steps 20 --set C1.r_a=50 --log"


# ---- sensitivity and csv ----

def test_sensitivity_ranks_knobs_at_the_cursor(conn, divider):
    spec = plotcmd.resolve(conn, line(
        "plot C1.current vs C1.voltage 0:300 --set C1.r_a=100 --set C1.r_b=200")).spec
    rows = plotview.sensitivity(conn, spec, 150)
    # current = V / (r_a + r_b): r_b carries twice r_a's share of the sum.
    assert [r.key for r in rows] == ["r_b", "r_a"]
    assert rows[0].value == pytest.approx(-0.6452, abs=1e-3)


def test_sensitivity_is_empty_without_knobs(conn, divider):
    spec = plotcmd.resolve(conn, line("plot C1.current vs C1.voltage 0:300")).spec
    assert plotview.sensitivity(conn, spec, 150) == []


def test_csv_table_is_x_then_a_column_per_series(conn, divider):
    columns, rows = plotview.csv_table(draw(conn, "plot C1.current vs C1.voltage 0:300 --steps 3"))
    assert columns == ["C1.voltage (V)", "C1.current"]
    assert [r[0] for r in rows] == [0.0, 100.0, 200.0, 300.0]
    assert rows[-1][1] == pytest.approx(1.0)


# ---- completion ----

def test_completion_offers_refs_then_vs(conn, divider):
    first = [c.value for c in plotcmd.choices(conn, "")]
    assert "C1.current" in first and "save" in first and "export" in first
    after = [c.value for c in plotcmd.choices(conn, "C1.current ")]
    assert after[0] == "vs"
    assert [c.value for c in plotcmd.choices(conn, "export ")] == ["csv", "png"]

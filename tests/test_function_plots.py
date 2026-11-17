"""Function plots: `plot P(r, 0) vs r 0:51` draws an expression along a
free variable -- the garden solved once, the expression evaluated at each
point -- through everything that draws a sweep.

The flux homework is the case again: phi(r) in cm, a limit at r = 0, and
a temperature function that declares its value in degF."""
import math

import pytest

from core import plotcmd, plotexport, plotview, worksheet
from core.command_bar import CommandError

SHEET = """phi(r [cm]) = A * sin(B * r) / r
phi.A = 5e13
phi.B = 0.0628 1/cm
P(r [cm], z [cm]) = (1 - (r/core.a)^2) * cos(pi*z/core.d)
core.a = 51 cm
core.d = 109 cm
core.H = 144 in
core.Ti = 543 degF
core.q = 10
Tb(z [in]) [degF] = core.Ti + core.q * (1 + sin(pi*z/core.H))"""


@pytest.fixture
def garden(conn):
    applied = worksheet.apply(conn, SHEET)
    assert not applied.problems, applied.problems
    return conn


def draw(conn, line: str, points: int = 1000) -> plotview.Figure:
    resolved = plotcmd.resolve(conn, plotcmd.parse_text(line).line)
    return plotview.figure_from_spec(conn, resolved.spec, max_points=points)


# ---- the line ----

def test_an_expression_makes_a_function_plot():
    line = plotcmd.parse_text("plot phi(r) vs r 0:30").line
    assert line.mode == plotcmd.FUNCTION_MODE and line.x == "r"
    assert plotcmd.parse_text("plot bd.l vs bd.x 1:9").line.mode == "sweep"


def test_a_call_typed_unquoted_keeps_its_arguments_together():
    line = plotcmd.parse_text("plot P(r, 0) P(r, 20) vs r 0:51").line
    assert line.ys == ("P(r, 0)", "P(r, 20)")


@pytest.mark.parametrize("line, message", [
    ("plot phi(r) vs r", "needs a range"),
    ("plot phi(r) bd.l vs r 0:1", "is a property"),
    ("plot phi(r) vs bd.r 0:1", "free variable"),
    ("plot phi(r) vs r 0:1 by z 0:1", "no `by`"),
    ("plot phi(r) vs r 0:1 --set bd.E=2", "--set"),
    ("plot phi(r) vs r 0:1 --scenario hot", "--scenario"),
])
def test_what_a_function_plot_does_not_take_yet_is_refused(line, message):
    with pytest.raises(CommandError, match=message):
        plotcmd.parse_text(line)


def test_a_malformed_expression_is_refused_when_resolved(garden):
    with pytest.raises(CommandError, match="phi"):
        plotcmd.resolve(garden, plotcmd.parse_text("plot 'phi(r' vs r 0:1").line)


def test_the_line_round_trips_through_its_spec(garden):
    text = "plot 'P(r, 0)' 'P(r, 20)' vs r 0:51 --steps 40 --log"
    spec = plotcmd.resolve(garden, plotcmd.parse_text(text).line).spec
    assert plotcmd.line_of(garden, spec).text() == text


def test_a_range_rewrite_keeps_the_expressions():
    line = plotcmd.parse_text("plot phi(r) vs r 0:30").line
    assert plotcmd.rewrite(line, {"range": [5, 10]}).text() == "plot 'phi(r)' vs r 5:10"


def test_functions_are_offered_as_calls(garden):
    offered = {c.value: c.description for c in plotcmd.choices(garden, "")}
    assert offered["phi(r)"] == "function" and offered["P(r, z)"] == "function"


# ---- the figure ----

def test_x_is_read_in_the_parameter_unit_and_y_in_base_si(garden):
    figure = draw(garden, "plot phi(r) vs r 0:30 --steps 6")
    assert figure.error is None and figure.x_label == "r (cm)"
    (series,) = figure.series
    assert series.unit == "m⁻¹"
    x, y = series.points[2]
    assert x == 10.0
    # 10 cm is 0.1 m: A sin(B r) / r with B = 6.28 /m.
    assert y == pytest.approx(5e13 * math.sin(6.28 * 0.1) / 0.1, rel=1e-6)


def test_a_call_at_zero_over_zero_takes_its_limit(garden):
    first = draw(garden, "plot phi(r) vs r 0:30 --steps 6").series[0].points[0]
    assert first == [0.0, pytest.approx(5e13 * 6.28, rel=1e-6)]


def test_a_declared_result_unit_is_the_axis_unit(garden):
    figure = draw(garden, "plot Tb(z) vs z -72:72 --steps 4")
    assert figure.x_label == "z (in)"
    (series,) = figure.series
    assert series.unit == "degF"
    assert [p[1] for p in series.points] == pytest.approx([543, 548.2721, 561, 573.7279, 579], rel=1e-5)


def test_curves_of_different_dimensions_get_their_own_bands(garden):
    figure = draw(garden, "plot P(r, 0) phi(r) vs r 0:51 --steps 10")
    assert len(figure.bands) == 2
    assert figure.series[0].points[-1][1] == pytest.approx(0.0, abs=1e-12)


def test_a_point_that_fails_is_a_hole_and_a_span(garden):
    figure = draw(garden, "plot sin(x)/x vs x 0:3 --steps 3")
    points = figure.series[0].points
    assert points[0] is None and points[1] == [1.0, pytest.approx(math.sin(1))]
    assert figure.failed and "0/0" in figure.failed[0].message


def test_an_unknown_function_says_so(garden):
    figure = draw(garden, "plot nope(r) vs r 0:1 --steps 2")
    assert figure.error and "nope" in figure.error


def test_a_variable_two_parameters_measure_differently_is_refused(garden):
    worksheet.apply(garden, SHEET + "\nw(t [s]) = t")
    with pytest.raises(CommandError, match="different things"):
        plotcmd.resolve(garden, plotcmd.parse_text("plot 'phi(r) + w(r)' vs r 0:1").line)


def test_a_saved_function_plot_reopens_with_its_legend(garden):
    command = plotcmd.parse_text("plot save Flux plot phi(r) vs r 0:30")
    plotcmd.run_save(garden, command)
    resolved = plotcmd.resolve(garden, plotcmd.parse_text("plot Flux").line)
    assert [s.ref for s in resolved.series] == ["phi(r)"]
    figure = plotview.figure(garden, "Flux")
    assert figure.error is None and figure.x_label == "r (cm)"


# ---- export ----

def test_a_function_plot_exports_with_its_band(garden):
    worksheet.apply(garden, SHEET.replace("phi.A = 5e13", "phi.A = 5e13 ± 1e12"))
    data, pub = plotexport.export(garden, "plot phi(r) vs r 0:30 --steps 10", "svg")
    assert data.lstrip().startswith("<") and not pub.problems
    assert pub.sigma["s0"][1] == pytest.approx(0.02 * pub.figure["series"][0]["points"][1][1], rel=1e-3)


def test_a_function_plot_exports_as_csv(garden):
    columns, rows = plotview.csv_table(draw(garden, "plot phi(r) vs r 0:30 --steps 3"))
    assert columns == ["r (cm)", "phi(r)"] and len(rows) == 4

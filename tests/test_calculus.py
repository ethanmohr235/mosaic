"""`integrate` and `deriv` (core/calculus.py) on function motes.

What earns a test: the numerics come out right, including through a
removable 0/0 and with the bounds either way round; a pole, a kink and an
estimate that never settles fail rather than return a number; a call reads
the parameter it is along, the parameters held fixed and bare-number bounds
in the declared unit; an unknown bound or point defers, so Newton solves
through it; units come out as the integrand's times (or over) the
parameter's; and the answer is recorded in provenance, said by `\\work`,
drawn in LaTeX and never mistaken for a property named after the function.
"""
import math

import pytest

from core import calc, worksheet
from core.calculus import CalculusError, derivative, integrate
from core.command_bar import _equation_property_refs
from core.formula_tex import to_tex
from core.motes import create_mote, list_motes
from core.provenance import DERIVATIVE_READ, INTEGRAL_READ
from core.simulation import SimulationError, check_garden, inferred_units, run_simulation, solve_journal
from core.units import UNITS_KEY, dimension_of
from core.worksheet import _qualify


def named(conn, name: str):
    return next(m for m in list_motes(conn) if m.properties.get("name") == name)


def function(conn, name: str, body: str, parameters: str, **extra) -> str:
    return create_mote(conn, {"name": name, "function": body, "parameters": parameters, **extra})


def solve(conn, sheet: str, solver: str = "linear"):
    """Apply `sheet` with the garden's own lines first -- applying replaces,
    so the function motes made here must come along."""
    applied = worksheet.apply(conn, f"{worksheet.read(conn)}\n{sheet}")
    assert not applied.problems, applied.problems
    return run_simulation(conn, solver=solver)


def si(x: float) -> float:
    """The sine integral, ∫₀ˣ sin(t)/t dt, by its series."""
    return sum((-1) ** n * x ** (2 * n + 1) / ((2 * n + 1) * math.factorial(2 * n + 1))
               for n in range(30))


# ---- the numerics ----

@pytest.mark.parametrize("f, a, b, expected", [
    (lambda x: x ** 3, 0.0, 2.0, 4.0),
    (math.sin, 0.0, math.pi, 2.0),
    (math.sin, 0.0, 2 * math.pi, 0.0),
    (lambda x: math.exp(-x * x), -5.0, 5.0, math.sqrt(math.pi)),
    (lambda x: 1.0 if x > 0.37 else 0.0, 0.0, 1.0, 0.63),
])
def test_integrals_come_out(f, a, b, expected):
    assert integrate(f, a, b).value == pytest.approx(expected, rel=1e-9, abs=1e-12)


def test_an_integrand_undefined_at_a_bound_is_never_asked_there():
    assert integrate(lambda x: 1 / math.sqrt(x), 0.0, 1.0).value == pytest.approx(2.0, rel=1e-8)


def test_reversed_bounds_negate_and_equal_ones_give_zero():
    assert integrate(lambda x: x, 2.0, 0.0).value == pytest.approx(-2.0)
    assert integrate(lambda x: x, 1.0, 1.0).value == 0.0


def test_a_pole_inside_the_bounds_fails_and_says_where():
    with pytest.raises(CalculusError, match="does not settle near 0.314159"):
        integrate(lambda x: 1 / (x - math.pi / 10) ** 2, 0.0, 1.0)


@pytest.mark.parametrize("f, x, expected", [
    (math.exp, 1.0, math.e),
    (math.cos, 0.0, 0.0),
    (lambda x: 1e6 + x, 3.0, 1.0),
    (lambda x: x ** 3, 1e5, 3e10),
])
def test_derivatives_come_out(f, x, expected):
    assert derivative(f, x, 0.1).value == pytest.approx(expected, rel=1e-8, abs=1e-9)


def test_a_step_too_coarse_for_the_function_is_retried_finer():
    assert derivative(lambda x: math.sin(1e4 * x), 1e-3, 0.1).value == \
        pytest.approx(1e4 * math.cos(10), rel=1e-8)


@pytest.mark.parametrize("f, x", [(abs, 0.0), (lambda x: abs(x - 0.5), 0.5)])
def test_a_kink_has_no_derivative(f, x):
    with pytest.raises(CalculusError, match="from the left -1, from the right 1"):
        derivative(f, x, 0.1)


def test_a_smooth_minimum_is_not_a_kink():
    assert derivative(lambda x: abs(x) ** 1.5, 0.0, 0.1).value == pytest.approx(0.0, abs=1e-9)


# ---- calling them ----

def test_an_integral_of_a_function_of_one_parameter(conn):
    function(conn, "q", "3 * x^2", "x")
    solve(conn, "m.Q = integrate(q, 0, 2)\nm.back = integrate(q, 2, 0)")
    props = named(conn, "m").properties
    assert props["Q"] == pytest.approx(8.0)
    assert props["back"] == pytest.approx(-8.0)


def test_an_integral_through_a_removable_singularity(conn):
    function(conn, "s", "sin(x) / x", "x")
    solve(conn, "m.I = integrate(s, -1, 1)")
    assert named(conn, "m").properties["I"] == pytest.approx(2 * si(1.0), rel=1e-9)


def test_bare_number_bounds_take_the_parameters_unit(conn):
    function(conn, "phi", "A * sin(B * r) / r", "r",
             A=5e13, B=0.0628, **{UNITS_KEY: {"r": "cm", "B": "1/cm"}})
    solve(conn, "reactor.Q = integrate(phi, 0, 10)")
    # B = 6.28 1/m and R = 0.1 m: ∫₀ᴿ A sin(Br)/r dr = A Si(BR).
    assert named(conn, "reactor").properties["Q"] == pytest.approx(5e13 * si(0.628), rel=1e-9)


def test_a_derivative(conn):
    function(conn, "g", "exp(x)", "x")
    solve(conn, "m.d = deriv(g, 1)")
    assert named(conn, "m").properties["d"] == pytest.approx(math.e, rel=1e-9)


def test_a_named_parameter_with_the_others_held(conn):
    function(conn, "f", "x * y^2", "x, y")
    solve(conn, "\n".join([
        "m.I = integrate(f, 'y', 0, 3, 2)",   # ∫₀³ 2 y² dy
        "m.dy = deriv(f, 'y', 3, 2)",         # 2 x y at x = 2, y = 3
        "m.dx = deriv(f, 'x', 2, 3)",         # y² at y = 3
    ]))
    props = named(conn, "m").properties
    assert (props["I"], props["dy"], props["dx"]) == (
        pytest.approx(18.0), pytest.approx(12.0), pytest.approx(9.0))


def test_naming_the_only_parameter_is_allowed(conn):
    function(conn, "q", "3 * x^2", "x")
    solve(conn, "m.Q = integrate(q, 'x', 0, 2)")
    assert named(conn, "m").properties["Q"] == pytest.approx(8.0)


@pytest.mark.parametrize("call, message", [
    ("integrate(f, 0, 1, 2)", r"must name the parameter.*f takes x, y, e\.g\. integrate\(f, 'x', \.\.\.\)"),
    ("deriv(f, 'z', 1, 2)", r"f has no parameter 'z' \(its parameters: x, y\)"),
    ("integrate(f, 'x', 0, 1)", r"takes x's two bounds, then y"),
    ("deriv(f, 'y', 1)", r"takes the point y is at, then x"),
    ("integrate(2, 0, 1)", r"takes a function's name first"),
])
def test_a_malformed_call_says_how(conn, call, message):
    function(conn, "f", "x * y", "x, y")
    with pytest.raises(SimulationError, match=message):
        solve(conn, f"m.v = {call}")


def test_recursion_is_refused(conn):
    function(conn, "r", "integrate(r, 0, x)", "x")
    with pytest.raises(SimulationError, match="calls itself"):
        solve(conn, "m.v = r(1)")


def test_a_pole_fails_and_says_where(conn):
    function(conn, "inv", "1 / x", "x")
    with pytest.raises(SimulationError, match=r"integrate\(inv\): it is not defined at x = 0"):
        solve(conn, "m.v = integrate(inv, -1, 1)")


def test_a_kink_fails(conn):
    function(conn, "a", "abs(x)", "x")
    with pytest.raises(SimulationError, match=r"deriv\(a\): no derivative"):
        solve(conn, "m.v = deriv(a, 0)")


def test_a_second_derivative_composes(conn):
    function(conn, "g", "x^3", "x")
    function(conn, "g1", "deriv(g, x)", "x")
    solve(conn, "m.dd = deriv(g1, 2)")
    assert named(conn, "m").properties["dd"] == pytest.approx(12.0, rel=1e-6)


# ---- solving through them ----

def test_newton_solves_for_an_unknown_bound(conn):
    function(conn, "q", "3 * x^2", "x")
    solve(conn, "m.L = ?\nintegrate(q, 0, m.L) == 8", solver="newton")
    assert named(conn, "m").properties["L"] == pytest.approx(2.0, rel=1e-8)


def test_newton_finds_where_a_derivative_is_zero(conn):
    function(conn, "p", "(x - 3)^2", "x")
    solve(conn, "m.x = ?\nderiv(p, m.x) == 0", solver="newton")
    assert named(conn, "m").properties["x"] == pytest.approx(3.0, abs=1e-6)


def test_a_bound_another_rule_solves_is_substituted_first(conn):
    function(conn, "q", "3 * x^2", "x")
    solve(conn, "m.L = 1 + 1\nm.Q = integrate(q, 0, m.L)")
    assert named(conn, "m").properties["Q"] == pytest.approx(8.0)


# ---- units ----

def test_an_integral_is_the_integrands_unit_times_the_parameters(conn):
    function(conn, "force", "k", "x", k=2.0, **{UNITS_KEY: {"x": "m", "k": "N"}})
    solve(conn, "m.W = integrate(force, 0, 3)\nm.k = deriv(force, 1)")
    units = inferred_units(conn)[named(conn, "m").id]
    assert dimension_of(units["W"]) == dimension_of("J")
    assert dimension_of(units["k"]) == dimension_of("N/m")


def test_a_bare_bound_into_an_undeclared_parameter_warns(conn):
    function(conn, "q", "3 * x^2", "x")
    applied = worksheet.apply(conn, f"{worksheet.read(conn)}\nm.Q = integrate(q, 0, 2)")
    assert not applied.problems
    problems = check_garden(conn).problems
    assert any("2 is passed to x, which declares no unit" in p for p in problems)


# ---- what is said about them ----

def test_provenance_records_the_integral_and_the_derivative(conn):
    function(conn, "q", "3 * x^2", "x")
    worksheet.apply(conn, f"{worksheet.read(conn)}\nm.Q = integrate(q, 0, 2)\nm.d = deriv(q, 1)")
    journal, error = solve_journal(conn)
    assert error is None
    reads = {r.kind: r for d in journal.derivations for r in d.inputs
             if r.kind in (INTEGRAL_READ, DERIVATIVE_READ)}
    assert reads[INTEGRAL_READ].key == "∫ x 0 → 2"  # a polynomial: exact
    assert reads[INTEGRAL_READ].value == pytest.approx(8.0)
    assert reads[DERIVATIVE_READ].key == "d/dx at 1"
    assert reads[DERIVATIVE_READ].value == pytest.approx(6.0)


def test_held_parameters_are_part_of_what_was_asked(conn):
    function(conn, "f", "x * y", "x, y", **{UNITS_KEY: {"x": "cm"}})
    worksheet.apply(conn, f"{worksheet.read(conn)}\nm.I = integrate(f, 'y', 0, 1, 2)")
    journal, _ = solve_journal(conn)
    [read] = [r for d in journal.derivations for r in d.inputs if r.kind == INTEGRAL_READ]
    assert read.key == "∫ y 0 → 1, x = 2 cm"


def test_an_inexact_estimate_says_how_close_it_is(conn):
    function(conn, "g", "sqrt(x)", "x")  # unbounded slope at 0: never exact
    worksheet.apply(conn, f"{worksheet.read(conn)}\nm.I = integrate(g, 0, 1)")
    journal, _ = solve_journal(conn)
    [read] = [r for d in journal.derivations for r in d.inputs if r.kind == INTEGRAL_READ]
    assert read.key.startswith("∫ x 0 → 1 ± ") and read.key.endswith(" relative")


def test_work_says_the_result_came_from_an_integral(conn):
    function(conn, "q", "3 * x^2", "x")
    solve(conn, "m.Q = integrate(q, 0, 2)")
    shown = calc.work(conn, named(conn, "m").id, "Q", figures=3)
    assert [s.text for s in shown.steps][0] == "m.Q == integrate(q, 0, 2)"
    result = shown.steps[-1]
    assert result.text == "8.00"
    assert result.note == "q: ∫ x 0 → 2"


@pytest.mark.parametrize("equation, tex", [
    ("Q == integrate(phi, 0, R)", r"Q = \int_{0}^{R} \phi"),
    ("Q == integrate(f, 'y', 0, 1, x0)", r"Q = \int_{0}^{1} f\left(y;\, x_{0}\right)\,\mathrm{d}y"),
    ("g == deriv(phi, x0)", r"g = \phi'\left(x_{0}\right)"),
    ("g == deriv(phi, 'r', 2)", r"g = \frac{\mathrm{d} \phi}{\mathrm{d} r}\left(2\right)"),
    ("g == deriv(f, 'y', y0, x0)",
     r"g = \frac{\partial f}{\partial y}\left(y_{0};\, x_{0}\right)"),
])
def test_drawn_in_latex(equation, tex):
    assert to_tex(equation) == tex


def test_the_function_name_is_not_read_as_a_property():
    own, _other = _equation_property_refs("integrate(phi, 0, R) + deriv(g, 'x', x0, y)")
    assert own == {"R", "x0", "y"}
    assert _qualify("Q == integrate(phi, 0, R)", "m") == "m.Q == integrate(phi, 0, m.R)"

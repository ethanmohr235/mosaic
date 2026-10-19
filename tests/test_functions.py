"""Function motes (core/functions.py) called from formulas (core/formula.py).

A function is the relation itself, asked at whatever point a caller names.
What earns a test: a call evaluates the body with its arguments bound; an
unknown argument passes through, so the linear solver and Newton both see
through a call; a bare number takes the parameter's unit; and the ways a
call can be wrong (arity, recursion, two functions of one name) say so.
"""
import math

import pytest

from core import health, worksheet
from core.formula import FormulaError, function_name_problem
from core.functions import FunctionDefinitionError, parameters_of
from core.motes import create_mote, list_motes
from core.simulation import SimulationError, check_garden, run_simulation
from core.units import UNITS_KEY


def named(conn, name: str):
    return next(m for m in list_motes(conn) if m.properties.get("name") == name)


def flux(conn, **extra) -> str:
    return create_mote(conn, {
        "name": "phi", "function": "A * sin(B * r) / r", "parameters": "r",
        "A": 5e13, "B": 0.0628, UNITS_KEY: {"r": "cm", "B": "1/cm"}, **extra,
    })


def solve(conn, sheet: str, solver: str = "linear"):
    applied = worksheet.apply(conn, sheet)
    assert not applied.problems, applied.problems
    return run_simulation(conn, solver=solver)


# ---- what a function mote is ----

def test_parameters_read_like_freedoms():
    assert parameters_of({"parameters": "r, z"}) == ["r", "z"]
    assert parameters_of({"parameters": '["r", "z"]'}) == ["r", "z"]
    assert parameters_of({"parameters": ["r"]}) == ["r"]
    assert parameters_of({}) == []


@pytest.mark.parametrize("bad", ["r, r", "r, 2x", "[1, 2]"])
def test_unreadable_parameters_are_refused(bad):
    with pytest.raises(FunctionDefinitionError):
        parameters_of({"parameters": bad})


@pytest.mark.parametrize("name", ["sin", "sinc", "sum", "t", "pi", "e", "2phi", ""])
def test_a_function_cannot_be_named_like_a_builtin(name):
    assert function_name_problem(name)


def test_an_ordinary_name_is_fine():
    assert function_name_problem("phi") is None


# ---- calling one ----

def test_a_call_evaluates_the_body_at_its_argument(conn):
    flux(conn)
    solve(conn, "reactor.r = 10 cm\nreactor.flux = phi(reactor.r)")
    expected = 5e13 * math.sin(0.0628 * 10) / 0.10  # r in base SI: 0.10 m
    assert named(conn, "reactor").properties["flux"] == pytest.approx(expected)


def test_one_function_answers_at_several_points(conn):
    flux(conn)
    solve(conn, "reactor.r = 10 cm\nreactor.edge = phi(reactor.r)\nreactor.mid = phi(5)")
    props = named(conn, "reactor").properties
    assert props["mid"] == pytest.approx(5e13 * math.sin(0.0628 * 5) / 0.05)
    assert props["edge"] != props["mid"]


def test_a_bare_number_takes_the_parameters_declared_unit(conn):
    flux(conn)
    solve(conn, "reactor.a = phi(10)\nreactor.r = 10 cm\nreactor.b = phi(reactor.r)")
    props = named(conn, "reactor").properties
    assert props["a"] == pytest.approx(props["b"])


def test_a_bare_number_into_an_undeclared_parameter_is_base_si(conn):
    create_mote(conn, {"name": "double", "function": "2 * x", "parameters": "x"})
    solve(conn, "m.y = double(3)")
    assert named(conn, "m").properties["y"] == 6


def test_an_unknown_argument_stays_linear(conn):
    create_mote(conn, {"name": "line", "function": "m * x + c", "parameters": "x",
                       "m": 2, "c": 1})
    solve(conn, "s.x = ?\nline(s.x) == 7")
    assert named(conn, "s").properties["x"] == pytest.approx(3)


def test_newton_solves_through_a_nonlinear_body(conn):
    create_mote(conn, {"name": "sq", "function": "x * x", "parameters": "x"})
    applied = worksheet.apply(conn, "s.x = 1\ns.x = ?\nsq(s.x) == 9")
    assert not applied.problems, applied.problems
    run_simulation(conn, solver="newton")
    assert named(conn, "s").properties["x"] == pytest.approx(3)


def test_functions_compose(conn):
    create_mote(conn, {"name": "double", "function": "2 * x", "parameters": "x"})
    create_mote(conn, {"name": "quad", "function": "double(double(x))", "parameters": "x"})
    solve(conn, "m.y = quad(3)")
    assert named(conn, "m").properties["y"] == 12


def test_a_parameter_shadows_t_inside_the_body(conn):
    create_mote(conn, {"name": "decay", "function": "exp(-t)", "parameters": "t"})
    solve(conn, "m.y = decay(1)")
    assert named(conn, "m").properties["y"] == pytest.approx(math.exp(-1))


# ---- a call that is wrong ----

def test_the_wrong_number_of_arguments_names_the_parameters(conn):
    flux(conn)
    worksheet.apply(conn, "m.y = phi(1, 2)")
    with pytest.raises(SimulationError, match=r"phi\(\) takes 1 argument \(r\), got 2"):
        run_simulation(conn)


def test_recursion_is_refused(conn):
    create_mote(conn, {"name": "loop", "function": "loop(x)", "parameters": "x"})
    worksheet.apply(conn, "m.y = loop(1)")
    with pytest.raises(SimulationError, match="cannot be recursive"):
        run_simulation(conn)


def test_two_functions_of_one_name_is_a_hard_error(conn):
    flux(conn)
    flux(conn)
    worksheet.apply(conn, "m.y = phi(1)")
    with pytest.raises(SimulationError, match="'phi' matches 2 motes"):
        run_simulation(conn)


def test_a_mote_without_a_body_is_not_a_function(conn):
    create_mote(conn, {"name": "phi", "A": 1})
    worksheet.apply(conn, "m.y = phi(1)")
    with pytest.raises(SimulationError, match="unknown function 'phi'"):
        run_simulation(conn)


def test_an_error_in_the_body_says_which_function(conn):
    create_mote(conn, {"name": "f", "function": "x * missing", "parameters": "x"})
    worksheet.apply(conn, "m.y = f(1)")
    with pytest.raises(SimulationError, match=r"in f\(\):.*'missing'"):
        run_simulation(conn)


def test_a_function_is_not_itself_solved(conn):
    flux(conn)
    before = named(conn, "phi").properties
    solve(conn, "reactor.r = 10 cm\nreactor.flux = phi(reactor.r)")
    assert named(conn, "phi").properties == before


def test_the_formula_level_error_for_an_unknown_name():
    from tests.test_formula import context
    from core.formula import evaluate_expression
    with pytest.raises(FormulaError, match="unknown function 'nope'"):
        evaluate_expression("nope(1)", context())


# ---- the unit check and solve health ----

def unit_problems(conn, sheet: str) -> list[str]:
    applied = worksheet.apply(conn, sheet)
    assert not applied.problems, applied.problems
    return check_garden(conn).problems


def test_a_bare_number_into_an_undeclared_parameter_warns(conn):
    create_mote(conn, {"name": "double", "function": "2 * x", "parameters": "x"})
    [problem] = unit_problems(conn, "m.y = double(3)")
    assert "3 is passed to x, which declares no unit" in problem
    assert "base SI" in problem


def test_a_bare_number_into_a_declared_parameter_is_quiet(conn):
    flux(conn)
    assert unit_problems(conn, "reactor.center = phi(0)") == []


def test_a_dimensionless_parameter_is_quiet(conn):
    create_mote(conn, {"name": "g", "function": "sinc(x)", "parameters": "x",
                       UNITS_KEY: {"x": "1"}})
    assert unit_problems(conn, "m.y = g(0)") == []


def test_an_argument_of_the_wrong_dimension_warns(conn):
    flux(conn)
    [problem] = unit_problems(conn, "reactor.T = 300 K\nreactor.f = phi(reactor.T)")
    assert "r declares" in problem and "is passed" in problem


def test_a_problem_inside_the_body_is_the_callers_problem(conn):
    create_mote(conn, {"name": "bad", "function": "x + y", "parameters": "x",
                       "y": 1, UNITS_KEY: {"x": "m", "y": "s"}})
    problems = unit_problems(conn, "m.r = 1 m\nm.z = bad(m.r)")
    assert any("different dimensions" in p for p in problems)


def test_the_result_unit_is_inferred_through_the_body(conn):
    create_mote(conn, {"name": "twice", "function": "2 * x", "parameters": "x",
                       UNITS_KEY: {"x": "m"}})
    solve(conn, "m.r = 3 m\nm.d = twice(m.r)")
    assert named(conn, "m").properties[UNITS_KEY]["d"] == "m"


def test_health_reports_a_function_no_call_can_reach(conn):
    create_mote(conn, {"name": "sin", "function": "2 * x", "parameters": "x"})
    create_mote(conn, {"name": "f", "function": "2 * x", "parameters": "x, x"})
    create_mote(conn, {"name": "g", "function": "2 *", "parameters": "x"})
    problems = health.check(conn).function_problems
    assert any(p.startswith("function sin:") and "builtin" in p for p in problems)
    assert any(p.startswith("function f:") and "repeat" in p for p in problems)
    assert any(p.startswith("function g:") and "parse" in p for p in problems)


def test_a_healthy_function_reports_nothing(conn):
    flux(conn)
    assert health.check(conn).function_problems == ()

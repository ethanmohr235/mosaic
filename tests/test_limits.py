"""core/limits.py, and the limit a function call takes at 0/0.

A function whose body is 0/0 at the point it is called is answered with
its two-sided limit, and the solve says so. What earns a test: removable
singularities come out right (including a limit of exactly 0); a jump and
a pole fail rather than average into a number; the limit is approached
along the parameter, in its declared unit; and a solve reports every
limit it took while a plain rule that hits 0/0 stays an error.
"""
import math

import pytest

from core import worksheet
from core.limits import LimitError, first_step, two_sided
from core.motes import create_mote, list_motes
from core.provenance import LIMIT_READ
from core.simulation import SimulationError, run_simulation, run_time_series, solve_journal
from core.units import UNITS_KEY


def named(conn, name: str):
    return next(m for m in list_motes(conn) if m.properties.get("name") == name)


# ---- the numerics ----

@pytest.mark.parametrize("f, expected", [
    (lambda x: math.sin(x) / x, 1.0),
    (lambda x: math.sin(6.28 * x) / x, 6.28),
    (lambda x: (1 - math.cos(x)) / x ** 2, 0.5),
    (lambda x: (1 - math.cos(x)) / x, 0.0),
    (lambda x: x / math.expm1(x), 1.0),
])
def test_removable_singularities_come_out(f, expected):
    assert two_sided(f, 0.0, 1e-3) == pytest.approx(expected, rel=1e-7, abs=1e-12)


def test_a_point_other_than_zero():
    assert two_sided(lambda x: (x * x - 4) / (x - 2), 2.0, 1e-3) == pytest.approx(4.0)


def test_a_jump_is_not_a_limit():
    with pytest.raises(LimitError, match="from the left -1, from the right 1"):
        two_sided(lambda x: abs(x) / x, 0.0, 1e-3)


def test_a_pole_is_not_a_limit():
    with pytest.raises(LimitError, match="no finite limit"):
        two_sided(lambda x: x / x ** 2, 0.0, 1e-3)


def test_a_step_too_coarse_for_the_function_is_retried_finer():
    assert two_sided(lambda x: math.sin(1e9 * x) / x, 0.0, 1e-3) == pytest.approx(1e9)


def test_the_first_step_follows_the_declared_unit():
    assert first_step(0.0, 0.01) == pytest.approx(1e-5)   # a thousandth of a cm
    assert first_step(0.0, None) == pytest.approx(1e-3)
    assert first_step(500.0, None) == pytest.approx(0.5)


# ---- at a call ----

FLUX = {"name": "phi", "function": "A * sin(B * r) / r", "parameters": "r",
        "A": 5e13, "B": 0.0628, UNITS_KEY: {"r": "cm", "B": "1/cm"}}


def test_a_call_at_zero_over_zero_takes_the_limit(conn):
    create_mote(conn, FLUX)
    worksheet.apply(conn, "reactor.center = phi(0)")
    result = run_simulation(conn)
    # A * B, with B in 1/m: 5e13 * 6.28
    assert named(conn, "reactor").properties["center"] == pytest.approx(5e13 * 6.28)
    [limit] = result.limits
    assert (limit.function, limit.parameter, limit.approach) == ("phi", "r", "r → 0 cm")
    assert "1 value came from a limit (phi: r → 0 cm)" in result.summary


def test_the_limit_agrees_with_sinc(conn):
    create_mote(conn, FLUX)
    worksheet.apply(conn, "reactor.a = phi(0)\nreactor.b = phi.A * phi.B * sinc(0)")
    result = run_simulation(conn)
    props = named(conn, "reactor").properties
    assert props["a"] == pytest.approx(props["b"], rel=1e-9)
    assert len(result.limits) == 1


def test_a_call_away_from_the_singularity_takes_none(conn):
    create_mote(conn, FLUX)
    worksheet.apply(conn, "reactor.edge = phi(10)")
    result = run_simulation(conn)
    assert result.limits == []
    assert "limit" not in result.summary


def test_the_limit_is_along_whichever_parameter_settles(conn):
    create_mote(conn, {"name": "g", "function": "sin(a * x) / x", "parameters": "a, x"})
    worksheet.apply(conn, "m.y = g(3, 0)")
    result = run_simulation(conn)
    assert named(conn, "m").properties["y"] == pytest.approx(3.0)
    [limit] = result.limits
    # Along `a` the body stays 0/0; along `x` it settles.
    assert limit.parameter == "x"


def test_a_call_with_no_limit_fails_and_says_why(conn):
    create_mote(conn, {"name": "sgn", "function": "abs(x) / x", "parameters": "x"})
    worksheet.apply(conn, "m.y = sgn(0)")
    with pytest.raises(SimulationError, match=r"in sgn\(\): 0/0 at x = 0, and no limit"):
        run_simulation(conn)


def test_a_plain_rule_at_zero_over_zero_is_still_an_error(conn):
    worksheet.apply(conn, "reactor.r = 0\nreactor.flux = sin(reactor.r) / reactor.r")
    with pytest.raises(SimulationError, match="0/0 is indeterminate.*function"):
        run_simulation(conn)


def test_a_time_series_reports_its_limits(conn):
    create_mote(conn, FLUX)
    worksheet.apply(conn, "reactor.center = phi(0)")
    preview = run_time_series(conn, 2)
    assert [limit.approach for limit in preview.limits] == ["r → 0 cm"]
    assert "came from a limit" in preview.steps[0].summary


def test_provenance_records_the_limit(conn):
    create_mote(conn, FLUX)
    worksheet.apply(conn, "reactor.center = phi(0)")
    journal, error = solve_journal(conn)
    assert error is None
    [derivation] = journal.derivations
    limits = [read for read in derivation.inputs if read.kind == LIMIT_READ]
    assert [(read.key, read.value) for read in limits] == [("r → 0 cm", pytest.approx(5e13 * 6.28))]

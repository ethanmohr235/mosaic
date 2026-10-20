import math

import pytest

from core import uncertainty, worksheet
from core.motes import create_mote, list_motes, update_mote
from core.simulation import run_simulation
from core.units import UNITS_KEY


def named(conn, name):
    return next(m for m in list_motes(conn) if m.properties.get("name") == name)


def with_spread(conn, name, **spread):
    mote = named(conn, name)
    update_mote(conn, mote.id, {"uncertainty": spread})


BAR = """\
bd.x = 80 in
bd.y = 60 in
bd.l = sqrt(bd.x^2 + bd.y^2)
"""


def test_parse_reads_absolute_and_relative_and_reports_the_rest():
    spreads, problems = uncertainty.parse_uncertainty_property(
        {"a": 0.5, "b": "2%", "c": " 1.5 % ", "d": -1, "e": "lots", "f": True})
    assert spreads["a"] == uncertainty.Spread(absolute=0.5)
    assert spreads["b"].relative == pytest.approx(0.02)
    assert spreads["c"].relative == pytest.approx(0.015)
    assert sorted(p.split(":")[0] for p in problems) == ["d", "e", "f"]
    assert uncertainty.parse_uncertainty_property(None) == ({}, [])
    assert uncertainty.parse_uncertainty_property([1])[1]


def test_sigma_of_turns_a_percentage_into_the_value_unit():
    assert uncertainty.sigma_of({"x": 200, "uncertainty": {"x": "2%"}}, "x") == pytest.approx(4)
    assert uncertainty.sigma_of({"x": 200, "uncertainty": {"x": 3}}, "x") == 3
    assert uncertainty.sigma_of({"x": 200}, "x") is None


def test_a_linear_result_gets_the_root_sum_square_of_its_inputs(conn):
    worksheet.apply(conn, "s.a = 3\ns.b = 4\ns.c = s.a + 2 * s.b\n")
    with_spread(conn, "s", a=0.3, b=0.4)
    report = uncertainty.propagate(conn)
    (c,) = report.results
    assert c.label == "s.c" and c.value == pytest.approx(11)
    assert c.sigma == pytest.approx(math.hypot(0.3, 2 * 0.4))
    shares = {p.key: p.share for p in c.contributions}
    assert sum(shares.values()) == pytest.approx(1)
    assert shares["b"] > shares["a"]
    assert {p.key: p.sensitivity for p in c.contributions} == pytest.approx({"a": 1, "b": 2})


def test_a_nonlinear_result_uses_the_local_derivative(conn):
    worksheet.apply(conn, BAR)
    run_simulation(conn)
    with_spread(conn, "bd", x=0.5, y="1%")
    (l,) = uncertainty.propagate(conn).results
    # ∂l/∂x = x/l = 0.8, ∂l/∂y = 0.6; σy = 0.6 in. Reported in l's own
    # declared unit, which the solve inferred.
    from core.units import convert
    expected = convert(math.hypot(0.8 * 0.5, 0.6 * 0.6), "in", l.unit)
    assert l.sigma == pytest.approx(expected, rel=1e-6)


def test_units_are_the_motes_own(conn):
    worksheet.apply(conn, "m.a = 2 m\nm.b = 50 cm\nm.c = m.a + m.b\n")
    update_mote(conn, named(conn, "m").id, {"uncertainty": {"b": 10}})
    run_simulation(conn)
    (c,) = uncertainty.propagate(conn).results
    # ± 10 cm on b, reported in the unit a solve infers for c.
    from core.simulation import inferred_units
    assert c.unit == inferred_units(conn)[named(conn, "m").id]["c"]
    from core.units import convert
    assert c.sigma == pytest.approx(convert(10, "cm", c.unit), rel=1e-6)


def test_a_result_nothing_uncertain_reaches_is_exactly_known(conn):
    worksheet.apply(conn, "s.a = 3\ns.b = 4\ns.c = s.a * 2\ns.d = s.b * 2\n")
    with_spread(conn, "s", b=1)
    report = uncertainty.propagate(conn)
    assert report.of(named(conn, "s").id, "c").sigma == 0
    assert report.of(named(conn, "s").id, "d").sigma == pytest.approx(2)


def test_a_freedoms_own_spread_is_ignored_and_bad_entries_are_reported(conn):
    worksheet.apply(conn, "s.a = 3\ns.c = s.a * 2\n")
    with_spread(conn, "s", a=1, c=100, z="lots")
    report = uncertainty.propagate(conn)
    assert [i.label for i in report.inputs] == ["s.a"]
    assert report.of(named(conn, "s").id, "c").sigma == pytest.approx(2)
    assert any("s.z" in p for p in report.problems)


def test_a_garden_that_does_not_solve_reports_instead_of_raising(conn):
    s = create_mote(conn, {"name": "s", "freedoms": "x"})
    from core.relationships import create_relationship
    rule = create_mote(conn, {"equation": "x == nope"})
    create_relationship(conn, rule, s, "applies_to")
    report = uncertainty.propagate(conn)
    assert report.results == () and report.problems


def test_has_uncertainty(conn):
    assert not uncertainty.has_uncertainty(conn)
    create_mote(conn, {"x": 1, "uncertainty": {"x": 0.1}})
    assert uncertainty.has_uncertainty(conn)


def test_an_unsolved_result_is_in_the_unit_a_solve_infers(conn):
    # Inferred from the equations, so a result never written still says m.
    worksheet.apply(conn, BAR)
    with_spread(conn, "bd", x=0.5)
    (l,) = uncertainty.propagate(conn).results
    assert l.unit == "m"
    assert l.sigma == pytest.approx(0.8 * 0.5 * 0.0254, rel=1e-6)


# ---- showing it ----

from core import calc, modoc
from core.modoc_bind import resolve


def test_a_quantity_prints_its_spread_to_two_figures():
    assert calc.Quantity(100.0, "mm", 0.5383).text(4) == "100.0 ± 0.54 mm"
    assert calc.Quantity(3, None, 0).text() == "3 ± 0"
    assert calc.Quantity(3, None, None).text() == "3"


def test_a_spread_converts_by_scale_and_never_by_offset():
    q = calc.Quantity(20.0, "degC", 1.0).converted("K")
    assert q.value == pytest.approx(293.15) and q.sigma == pytest.approx(1.0)
    assert calc.Quantity(1.0, "m", 0.01).converted("mm").sigma == pytest.approx(10)


def test_a_given_quotes_its_own_spread_in_given_and_val(conn):
    worksheet.apply(conn, BAR)
    run_simulation(conn)
    with_spread(conn, "bd", x=0.5)
    rows = {r.label: r for r in calc.givens(conn)}
    assert rows["bd.x"].text() == "80 ± 0.50 in"
    note = create_mote(conn, {"modoc": "\\val{bd.x}"})
    resolution = resolve(conn, note, modoc.parse("\\val{bd.x}"))
    (value,) = resolution.values.values()
    assert value.text == "80 ± 0.50 in"


def test_a_result_quotes_its_propagated_spread_in_answers_and_val(conn):
    worksheet.apply(conn, "s.a = 3\ns.b = 4\ns.c = s.a + 2 * s.b\n")
    run_simulation(conn)
    with_spread(conn, "s", a=0.3, b=0.4)
    (answer,) = calc.answers(conn)
    assert answer.quantity.sigma == pytest.approx(math.hypot(0.3, 0.8))
    note = create_mote(conn, {"modoc": ""})
    resolution = resolve(conn, note, modoc.parse("\\val{s.c}{,3}"))
    (value,) = resolution.values.values()
    assert value.text == "11.0 ± 0.85"


def test_no_spread_anywhere_changes_nothing(conn):
    worksheet.apply(conn, BAR)
    run_simulation(conn)
    assert all(r.quantity.sigma is None for r in calc.answers(conn))
    assert all(r.quantity.sigma is None for r in calc.givens(conn))

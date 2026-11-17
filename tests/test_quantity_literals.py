"""A number with a unit inside a formula (core/quantity_literals.py).

What earns a test: the spelling (where a unit ends, what binds to what),
that the value arrives in base units, that every walker -- unit check,
LaTeX, shown work, the worksheet -- reads it as typed, and that a
property sharing a unit's name is never mistaken for it.
"""
import pytest

from core import calc, formula_tex, quantity_literals, worksheet
from core.command_bar import run_command
from core.dimensions import DimensionContext, check_equation
from core.formula import Context, FormulaError, evaluate_expression
from core.function_text import expand
from core.motes import create_mote, list_motes
from core.simulation import check_garden, run_simulation
from core.units import UNITS_KEY


def named(conn, name: str):
    return next(m for m in list_motes(conn) if m.properties.get("name") == name)


def value(text: str, properties: dict | None = None) -> float:
    props = properties or {}
    ctx = Context(
        mote_id="m",
        properties=lambda mid: props if mid == "m" else {},
        unknown_keys=lambda mid: set(),
        targets=lambda mid, t: [],
        sources=lambda mid, t: [],
    )
    return evaluate_expression(text, ctx).constant


def dimension_problems(text: str, units: dict[str, str]) -> list[str]:
    ctx = DimensionContext(
        mote_id="m", units=lambda mid: units if mid == "m" else {},
        targets=lambda mid, rel: [], sources=lambda mid, rel: [],
    )
    return check_equation(text, ctx)


# ---- the spelling ----

@pytest.mark.parametrize("text, rewritten", [
    ("r / 51 cm", "r / __unit__(51, 'cm')"),
    ("(r / 51 cm)^2", "(r / __unit__(51, 'cm'))^2"),
    ("51 cm^2", "__unit__(51, 'cm^2')"),
    ("9.81 m/s^2 * h", "__unit__(9.81, 'm/s^2') * h"),
    ("1.3 kJ/(kg*K)", "__unit__(1.3, 'kJ/(kg*K)')"),
    ("1.3 kJ/(kg K)", "__unit__(1.3, 'kJ/(kg K)')"),
    ("phi(5 cm, 2 s)", "phi(__unit__(5, 'cm'), __unit__(2, 's'))"),
    ("51 cm / s", "__unit__(51, 'cm') / s"),
    ("5%", "__unit__(5, '%')"),
    ("3 in", "__unit__(3, 'in')"),
    ("2.5e-3 kg", "__unit__(2.5e-3, 'kg')"),
    ("5 s^-1", "__unit__(5, 's^-1')"),
])
def test_a_unit_runs_to_the_next_space(text, rewritten):
    assert quantity_literals.rewrite(text) == rewritten


@pytest.mark.parametrize("text", [
    "2 * x + p12.r", "x2 * 3", "min(5, x)", "targets('a 5 cm').x", "x^2", "1e3 * x",
])
def test_a_formula_without_a_quantity_is_untouched(text):
    assert quantity_literals.rewrite(text) == text


def test_a_unit_glued_to_an_operator_says_to_add_a_space():
    with pytest.raises(FormulaError, match=r"`cm\*x` is not a unit -- a unit runs to the next space"):
        value("51 cm*x")


def test_a_word_that_is_no_unit_says_so():
    with pytest.raises(FormulaError, match=r"`cmm` after 51 is not a recognized unit"):
        value("51 cmm")
    with pytest.raises(FormulaError, match=r"write `2 \* pi`"):
        value("2 pi")


def test_spans_cover_the_quantity_as_typed():
    text = "w == m * 9.81 m/s^2"
    [(start, end)] = quantity_literals.spans(text)
    assert text[start:end] == "9.81 m/s^2"
    assert quantity_literals.spans("x == 2 pi") == []  # never raises


# ---- the value ----

def test_a_quantity_arrives_in_base_units():
    assert value("51 cm") == pytest.approx(0.51)
    assert value("1.3 kJ/(kg*K)") == pytest.approx(1300)
    assert value("5%") == pytest.approx(0.05)


def test_a_quantity_is_one_atom():
    assert value("r / 51 cm", {"r": 0.388}) == pytest.approx(0.388 / 0.51)
    assert value("51 cm^2") == pytest.approx(51e-4)
    assert value("(51 cm)^2") == pytest.approx(0.51 ** 2)
    assert value("-5 cm") == pytest.approx(-0.05)


def test_an_affine_quantity_is_absolute_and_negates_before_converting():
    assert value("400 degC") == pytest.approx(673.15)
    assert value("-40 degC") == pytest.approx(233.15)
    assert value("-40 degF") == pytest.approx(233.15)


def test_the_textbook_profile_reads_as_written(conn):
    # ne-homework-8: 51 and 109 are in cm, as the book means them.
    worksheet.apply(conn, (
        "P(r [cm], z [cm]) = (1 - (r / 51 cm)^2) * cos(pi * z / 109 cm)\n"
        "m.at = P(38.8, 0)\n"
        "m.mid = P(0, 109 / 2)"
    ))
    run_simulation(conn)
    m = named(conn, "m").properties
    assert m["at"] == pytest.approx(1 - (38.8 / 51) ** 2)
    assert m["mid"] == pytest.approx(0, abs=1e-12)


def test_a_quantity_argument_keeps_its_own_unit(conn):
    worksheet.apply(conn, "f(x [cm]) = x\nm.y = f(2 m)")
    run_simulation(conn)
    assert named(conn, "m").properties["y"] == pytest.approx(2)


# ---- the unit check ----

def test_a_quantity_carries_its_dimension():
    units = {"r": "m", "y": "1"}
    assert dimension_problems("y == 1 - (r / 51 cm)^2", units) == []
    [problem] = dimension_problems("y == r + 2 s", {"r": "m", "y": "m"})
    assert "different dimensions" in problem


def test_a_quantity_is_not_a_literal_that_adapts():
    [problem] = dimension_problems("y == 5 s", {"y": "m"})
    assert "different dimensions" in problem


# ---- every walker reads it as typed ----

def test_latex_sets_a_quantity_as_a_value_reads():
    assert formula_tex.to_tex("y == r / 51 cm") == r"y = \frac{r}{51\,\mathrm{cm}}"
    assert formula_tex.to_tex("y == (51 cm)^2") == r"y = \left(51\,\mathrm{cm}\right)^{2}"
    assert formula_tex.to_tex("T == 400 degC") == r"T = 400\,\mathrm{°C}"


def test_unparse_brackets_a_quantity():
    # Bracketed, so `(51 cm)^2` never reads back as 51 cm².
    from core.formula import parse_expression
    assert quantity_literals.unparse(parse_expression("(51 cm)^2 * x")) == "(51 cm) ^ 2 * x"


def test_a_function_written_out_keeps_its_quantities(conn):
    worksheet.apply(conn, "P(r [cm]) = 1 - (r / 51 cm)^2\nm.y = P(38.8)")
    properties = {m.id: m.properties for m in list_motes(conn)}
    assert expand("m.y == P(38.8)", properties) == "m.y == 1 - ((38.8 cm) / (51 cm)) ^ 2"


def test_shown_work_never_substitutes_into_a_unit(conn):
    # `box.m` is read, and so would the `m` of `m/s^2` be, were it a name.
    worksheet.apply(conn, "box.m = 2 kg\nbox.w = box.m * 9.81 m/s^2")
    run_simulation(conn)
    shown = calc.work(conn, named(conn, "box").id, "w")
    substituted = next(s.text for s in shown.steps if s.kind == calc.SUBSTITUTED)
    assert substituted == "box.w == (2 kg) * 9.81 m/s^2"


def test_the_worksheet_never_qualifies_a_unit(conn):
    run_command(conn, "box.m = 2 kg")
    run_command(conn, "box.w ?")
    run_command(conn, "box.w == box.m * 9.81 m/s^2")
    assert "box.w = box.m * 9.81 m/s^2" in worksheet.read(conn)


def test_the_unit_check_reads_a_quantity_in_a_worksheet_line(conn):
    worksheet.apply(conn, "box.m = 2 kg\nbox.w [N] = box.m * 9.81 m/s^2")
    run_simulation(conn)
    assert named(conn, "box").properties["w"] == pytest.approx(19.62)
    assert check_garden(conn).problems == []


def test_a_bad_unit_is_refused_by_the_worksheet_line(conn):
    applied = worksheet.apply(conn, "m.y = 2 * 51 cmm")
    assert applied.problems and "not a recognized unit" in applied.problems[0]


def test_an_affine_quantity_inside_arithmetic_is_a_degree():
    # Alone it is a temperature; added or multiplied it is a step, so
    # `T + 10 degF` is ten degrees warmer, not T + 260.93 K.
    assert value("T + 10 degF", {"T": 300.0}) == pytest.approx(300 + 10 / 1.8)
    assert value("T - 10 degC", {"T": 300.0}) == pytest.approx(290)
    assert value("2 * 10 degF") == pytest.approx(20 / 1.8)
    assert value("(400 degC)^2") == pytest.approx(673.15 ** 2)


def test_an_affine_quantity_as_an_argument_is_a_temperature(conn):
    worksheet.apply(conn, "k(T [degF]) [degF] = T\nm.y [degF] = k(500 degF)\nm.z [degF] = max(500 degF, 400 degF)")
    run_simulation(conn)
    m = named(conn, "m").properties
    assert m["y"] == pytest.approx(500) and m["z"] == pytest.approx(500)


def test_a_book_answer_checks_against_a_temperature(conn):
    worksheet.apply(conn, "p.T = 3940 degF\np.T ?= 3940 degF\np.T + 10 degF ?= 3950 degF\np.L = -40 degF\np.L ?= -40 degC")
    run_simulation(conn)
    assert check_garden(conn).problems == []

"""core/calc.py -- what a document says about a finished calculation.

The thing worth testing hardest is the substitution line, because it is the
one place a number is rewritten rather than read: values come out of the
provenance record in base SI and have to go back into the reader's declared
units, bracketed so an exponent binds to the whole quantity, and reconciled
where one line mixes two units of the same dimension.
"""
import pytest

from core import calc
from core import worksheet
from core.motes import create_mote, list_motes, update_mote
from core.relationships import create_relationship
from core.rules import APPLIES_TO, EQUATION_KEY
from core.simulation import FAIL, NOT_CHECKED, PASS, run_simulation
from core.units import UNITS_KEY

PYTHAGORAS = "bd.x = 80 in\nbd.y = 60 in\n\nbd.l = sqrt(bd.x^2 + bd.y^2)"


def named(conn, name: str):
    return next(m for m in list_motes(conn) if m.properties.get("name") == name)


def steps_of(work) -> dict[str, str]:
    return {step.kind: step.text for step in work.steps}


# ---- significant figures ----

@pytest.mark.parametrize("value, figures, expected", [
    (0.00123456, 3, 0.00123),
    (123456, 3, 123000.0),
    (2.5, 3, 2.5),
    (0, 3, 0),
    (100.0, None, 100.0),
])
def test_round_to_rounds_to_significant_figures(value, figures, expected):
    assert calc.round_to(value, figures) == expected


def test_round_to_leaves_a_non_number_alone():
    assert calc.round_to("soon", 3) == "soon"
    assert calc.round_to(True, 3) is True


def test_trailing_zeros_are_kept_when_figures_were_asked_for():
    """`2.50` quoted to three figures must not print as `2.5` -- the third
    digit is the claim being made."""
    assert calc.Quantity(2.5).text(3) == "2.50"
    assert calc.Quantity(2.5).text() == "2.5"


def test_a_quantity_prints_its_unit_after_the_number():
    assert calc.Quantity(80, "in").text() == "80 in"
    assert calc.Quantity(80, None).text() == "80"
    assert calc.Quantity(None, "in").text() == ""


def test_a_quantity_converts_and_leaves_an_impossible_conversion_alone():
    assert calc.Quantity(100, "in").converted("m").text(3) == "2.54 m"
    assert calc.Quantity(100, "in").converted("kg").text() == "100 in"
    assert calc.Quantity(100, None).converted("m").text() == "100"


# ---- the `source` convention ----

def test_source_reads_a_bare_word_and_a_kind_with_a_note():
    parsed = calc.parse_source_property({
        "x": "assumed",
        "s": {"kind": "external", "note": "FEA probe at the root"},
    })
    assert parsed["x"] == calc.Source("assumed")
    assert parsed["s"] == calc.Source("external", "FEA probe at the root")


def test_a_malformed_source_leaves_the_mote_unannotated():
    """Same discipline core/units.py's own map follows -- a broken map must
    not break the document that reads it."""
    for bad in ("assumed", None, 3, {"x": 3}, {3: "assumed"}, {"x": {}}):
        assert calc.parse_source_property(bad) == {}


def test_an_unrecognized_kind_is_kept_rather_than_dropped():
    """Somebody writing `handbook` means something by it; \\given can show
    it even though \\assumptions does not claim it."""
    assert calc.parse_source_property({"x": "handbook"})["x"].kind == "handbook"


# ---- the sections ----

def test_givens_are_the_numbers_the_equations_read(conn):
    worksheet.apply(conn, PYTHAGORAS)
    assert [(r.label, r.text()) for r in calc.givens(conn)] == [
        ("bd.x", "80 in"), ("bd.y", "60 in"),
    ]


def test_a_mote_no_equation_mentions_contributes_no_givens(conn):
    """A homework mote's `estimate: 3` is a real property and not an input
    to anything."""
    create_mote(conn, {"name": "HW 4", "estimate": 3})
    worksheet.apply(conn, PYTHAGORAS)
    assert "HW 4" not in {r.mote for r in calc.givens(conn)}


def test_answers_are_the_freedoms_with_what_they_hold(conn):
    worksheet.apply(conn, PYTHAGORAS)
    run_simulation(conn)
    assert [(r.label, r.text(3)) for r in calc.answers(conn)] == [("bd.l", "2.54 m")]


def test_an_answer_nobody_has_computed_still_appears(conn):
    """An empty answer is exactly what a reader needs to notice."""
    worksheet.apply(conn, PYTHAGORAS)
    assert [(r.label, r.text()) for r in calc.answers(conn)] == [("bd.l", "")]


def test_a_given_and_an_answer_never_overlap(conn):
    worksheet.apply(conn, PYTHAGORAS)
    run_simulation(conn)
    assert not {r.label for r in calc.givens(conn)} & {r.label for r in calc.answers(conn)}


def test_assumptions_are_what_source_says_are_assumed(conn):
    worksheet.apply(conn, PYTHAGORAS)
    update_mote(conn, named(conn, "bd").id, {
        calc.SOURCE_KEY: {"y": {"kind": "assumed", "note": "neglect the fillet"}},
    })
    rows = calc.assumptions(conn)
    assert [r.label for r in rows] == ["bd.y"]
    assert rows[0].source.note == "neglect the fillet"


def test_an_assumption_no_equation_touches_is_still_listed(conn):
    """"Neglect self-weight" is about something no equation mentions, and
    leaving it out would defeat the point of writing it down."""
    worksheet.apply(conn, PYTHAGORAS)
    create_mote(conn, {
        "name": "model", "self_weight": 0,
        calc.SOURCE_KEY: {"self_weight": {"kind": "assumed", "note": "neglected"}},
    })
    assert [r.label for r in calc.assumptions(conn)] == ["model.self_weight"]


def test_external_values_are_their_own_section(conn):
    worksheet.apply(conn, PYTHAGORAS)
    create_mote(conn, {
        "name": "fea", "sigma": 214.3,
        calc.SOURCE_KEY: {"sigma": "external"},
    })
    assert [r.label for r in calc.external(conn)] == ["fea.sigma"]
    assert [r.label for r in calc.assumptions(conn)] == []


def test_the_machinery_is_never_a_quantity(conn):
    """A reserved property holds a map or a string, so the numeric filter
    already keeps `units`, `equation` and `freedoms` out of every table."""
    worksheet.apply(conn, PYTHAGORAS)
    labels = {r.label for r in calc.givens(conn)} | {r.key for r in calc.answers(conn)}
    assert not {"units", "equation", "freedoms", "name", "source"} & labels


# ---- checks ----

def test_checks_report_pass_fail_and_not_checked(conn):
    worksheet.apply(conn, """bd.x = 80
bd.l = bd.x * 2
bd.q = ?
bd.l <= 300
bd.l >= 200
bd.q <= 5""")
    run_simulation(conn)
    results = {c.equation: c for c in calc.checks(conn)}
    assert results["bd.l <= 300"].status == PASS
    assert results["bd.l >= 200"].status == FAIL
    assert results["bd.l >= 200"].off_by == pytest.approx(40)
    assert results["bd.q <= 5"].status == NOT_CHECKED


def test_a_check_is_read_against_the_answer_that_was_written(conn):
    """Freedoms are cleared, so `bd.l <= 300` is checked against the 160 the
    last solve wrote rather than treated as an unknown."""
    worksheet.apply(conn, "bd.x = 80\nbd.l = bd.x * 2\nbd.l <= 300")
    assert calc.checks(conn)[0].status == NOT_CHECKED   # nothing solved yet
    run_simulation(conn)
    assert calc.checks(conn)[0].status == PASS


def test_an_equation_is_not_a_check(conn):
    worksheet.apply(conn, PYTHAGORAS)
    assert calc.checks(conn) == ()


# ---- shown work ----

def test_work_shows_the_equation_then_the_numbers_then_the_result(conn):
    worksheet.apply(conn, PYTHAGORAS)
    run_simulation(conn)
    shown = calc.work(conn, named(conn, "bd").id, "l", unit="in", figures=4)
    assert steps_of(shown) == {
        calc.SYMBOLIC: "bd.l == sqrt(bd.x^2 + bd.y^2)",
        calc.SUBSTITUTED: "bd.l == sqrt((80 in)^2 + (60 in)^2)",
        calc.RESULT: "100.0 in",
    }


def test_a_substituted_quantity_is_bracketed_so_an_exponent_binds_to_it(conn):
    """`sqrt(80 in^2)` would read as eighty square inches."""
    worksheet.apply(conn, PYTHAGORAS)
    run_simulation(conn)
    shown = calc.work(conn, named(conn, "bd").id, "l")
    assert "(80 in)^2" in steps_of(shown)[calc.SUBSTITUTED]


def test_values_are_substituted_in_their_declared_units_not_base_si(conn):
    """Somebody who wrote `80 in` has to see 80."""
    worksheet.apply(conn, PYTHAGORAS)
    run_simulation(conn)
    substituted = steps_of(calc.work(conn, named(conn, "bd").id, "l"))[calc.SUBSTITUTED]
    assert "80" in substituted and "2.032" not in substituted


def test_one_line_mixing_two_units_of_a_dimension_is_converted_and_says_so(conn):
    worksheet.apply(conn, "R.r1 = 4.7\nR.r2 = 300\n\nR.total = R.r1 + R.r2")
    update_mote(conn, named(conn, "R").id, {
        UNITS_KEY: {"r1": "kohm", "r2": "ohm", "total": "ohm"},
    })
    run_simulation(conn)
    shown = calc.work(conn, named(conn, "R").id, "total")
    substituted = next(s for s in shown.steps if s.kind == calc.SUBSTITUTED)
    assert substituted.text == "R.total == (4700 ohm) + (300 ohm)"
    assert substituted.note == "converted to ohm"


def test_one_unit_throughout_needs_no_conversion_note(conn):
    worksheet.apply(conn, PYTHAGORAS)
    run_simulation(conn)
    shown = calc.work(conn, named(conn, "bd").id, "l")
    assert all(not step.note for step in shown.steps)


def test_the_freedom_being_solved_for_is_left_standing(conn):
    """`l` on the left of `l == sqrt(...)` is not a value the solve read, so
    nothing is substituted for it."""
    worksheet.apply(conn, PYTHAGORAS)
    run_simulation(conn)
    assert steps_of(calc.work(conn, named(conn, "bd").id, "l"))[
        calc.SUBSTITUTED].startswith("bd.l ==")


def test_a_bare_property_name_is_substituted_too(conn):
    """An equation written elsewhere carries bare names, and the work has to
    show its numbers just the same."""
    mote = create_mote(conn, {"name": "R1", "resistance": 100, "current": 3,
                              "freedoms": "voltage"})
    rule = create_mote(conn, {EQUATION_KEY: "voltage == current * resistance"})
    create_relationship(conn, rule, mote, APPLIES_TO)
    run_simulation(conn)
    assert steps_of(calc.work(conn, mote, "voltage"))[calc.SUBSTITUTED] == \
        "voltage == 3 * 100"


def test_a_stored_value_has_no_work_and_says_so(conn):
    worksheet.apply(conn, PYTHAGORAS)
    run_simulation(conn)
    shown = calc.work(conn, named(conn, "bd").id, "x")
    assert not shown.shown
    assert shown.note
    assert shown.quantity.text() == "80 in"


def test_a_garden_nobody_solved_has_no_work_to_show(conn):
    worksheet.apply(conn, PYTHAGORAS)
    shown = calc.work(conn, named(conn, "bd").id, "l")
    assert not shown.shown
    assert shown.note


def test_work_on_a_mote_that_does_not_exist_is_an_error_not_a_crash(conn):
    shown = calc.work(conn, "no-such-mote", "l")
    assert shown.error
    assert not shown.shown


def test_a_solver_block_shows_every_equation_and_substitutes_into_none(conn):
    """Several equations determined several values together, and inventing
    one expression to put numbers into would imply an attribution the solve
    never made."""
    worksheet.apply(conn, """A.x = ?
A.y = ?
A.x + A.y == 10
A.x - A.y == 2""")
    run_simulation(conn)
    shown = calc.work(conn, named(conn, "A").id, "x")
    kinds = [step.kind for step in shown.steps]
    assert kinds.count(calc.SYMBOLIC) == 2
    assert calc.SUBSTITUTED not in kinds
    assert "no one expression" in shown.note
    assert shown.also_determined == ("A.y",)


# ---- verification (hand vs. external) ----

def fea(conn, **extra):
    return create_mote(conn, {
        "name": "bd", "sigma": 210.0, "sigma_fea": 30000.0, "load": 5,
        UNITS_KEY: {"sigma": "MPa", "sigma_fea": "psi"},
        "source": {"sigma_fea": {"kind": "external", "note": "FEA probe", "checks": "sigma"},
                   "load": "external"},
        **extra,
    })


def test_source_reads_what_an_external_value_checks():
    sources = calc.parse_source_property({"a": {"kind": "external", "checks": " b "}, "c": "external"})
    assert sources["a"].checks == "b" and sources["c"].checks == ""


def test_verification_pairs_an_external_value_with_the_hand_value_it_names(conn):
    fea(conn)
    (row,) = calc.verification(conn)
    assert (row.label, row.external_key, row.note) == ("bd.sigma", "sigma_fea", "FEA probe")
    # 30000 psi is 206.84 MPa, shown in the hand value's unit.
    assert row.external.unit == "MPa"
    assert row.external.value == pytest.approx(206.8427, rel=1e-5)
    assert row.error == pytest.approx((210 - 206.8427) / 206.8427 * 100, rel=1e-4)
    assert row.problem == ""


@pytest.mark.parametrize("extra, problem", [
    ({"sigma": None}, "holds no number yet"),
    ({"sigma_fea": 0, UNITS_KEY: {"sigma": "MPa", "sigma_fea": "MPa"}}, "is zero"),
    ({UNITS_KEY: {"sigma": "MPa", "sigma_fea": "m"}}, "cannot convert"),
    ({UNITS_KEY: {"sigma": "MPa"}}, "has a unit and the other does not"),
])
def test_a_comparison_that_cannot_be_made_says_why(conn, extra, problem):
    fea(conn, **extra)
    (row,) = calc.verification(conn)
    assert row.error is None and problem in row.problem

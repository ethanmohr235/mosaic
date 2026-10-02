"""core/worksheet.py -- a garden's calculation as lines of text.

Three things earn a test. That each line shape writes the motes, rules,
`applies_to` and freedoms it says; that `read()` is a *complete* projection,
so applying what it returns is a no-op rather than a deletion; and that
applying replaces -- a rule whose line is gone is deleted, which is the one
destructive thing here.
"""
import pytest

from core import worksheet
from core.motes import create_mote, get_mote, list_motes
from core.relationships import create_relationship, list_relationships
from core.rules import APPLIES_TO, EQUATION_KEY, FREEDOMS_KEY, list_rules, parse_freedoms
from core.simulation import run_simulation
from core.species import INSTANCE_OF
from core.units import UNITS_KEY, convert

FEA = """bd.x = 80 in
bd.y = 60 in
bd.l = sqrt(bd.x^2 + bd.y^2)"""


def named(conn, name: str):
    return next(m for m in list_motes(conn) if m.properties.get("name") == name)


def freedoms_of(conn, name: str) -> list[str]:
    return parse_freedoms(named(conn, name).properties.get(FREEDOMS_KEY))


def equations_of(conn) -> list[str]:
    return sorted(rule.equation for rule in list_rules(conn))


# ---- reading a line ----

def test_a_number_with_a_unit_is_a_given(conn):
    line = worksheet.parse("bd.x = 80 in").lines[0]
    assert (line.kind, line.mote, line.key, line.value, line.unit) == (
        worksheet.GIVEN, "bd", "x", 80, "in")


def test_a_bare_number_is_a_given_with_no_unit(conn):
    line = worksheet.parse("bd.x = 80").lines[0]
    assert (line.kind, line.value, line.unit) == (worksheet.GIVEN, 80, None)


def test_a_percent_is_a_given_the_way_the_command_bar_reads_one(conn):
    line = worksheet.parse("P.efficiency = 22.5%").lines[0]
    assert (line.kind, line.value, line.unit) == (worksheet.GIVEN, 22.5, "%")


def test_an_expression_is_an_equation(conn):
    line = worksheet.parse("bd.l = sqrt(bd.x^2 + bd.y^2)").lines[0]
    assert line.kind == worksheet.EQUATION
    assert line.relation == "bd.l == sqrt(bd.x^2 + bd.y^2)"
    assert line.free


def test_a_question_mark_is_an_unknown(conn):
    line = worksheet.parse("bd.Ax = ?").lines[0]
    assert (line.kind, line.mote, line.key, line.free) == (worksheet.UNKNOWN, "bd", "Ax", True)


def test_a_residual_declares_no_freedom(conn):
    """It names two unknowns and says nothing about which to solve for --
    which is exactly the guess the engine refuses to make."""
    line = worksheet.parse("bd.Ax + bd.Bx == 0").lines[0]
    assert line.kind == worksheet.EQUATION
    assert not line.free


def test_a_comparison_declares_no_freedom(conn):
    """An inequality can never determine a value."""
    line = worksheet.parse("bd.sigma <= 250").lines[0]
    assert line.kind == worksheet.EQUATION
    assert not line.free


def test_a_trailing_bang_names_the_rule(conn):
    line = worksheet.parse("bd.Ax + bd.Bx == 0  ! ΣFx").lines[0]
    assert line.name == "ΣFx"
    assert line.relation == "bd.Ax + bd.Bx == 0"


def test_a_blank_line_is_structure_only(conn):
    assert [line.kind for line in worksheet.parse("\n   \n").lines] == [
        worksheet.BLANK, worksheet.BLANK]


def test_a_line_that_does_not_parse_is_reported_not_raised(conn):
    sheet = worksheet.parse("bd.x = 80\nnonsense((\nbd.y = 60")
    assert len(sheet.problems) == 1
    assert "line 2" in sheet.problems[0]
    assert len(sheet.givens) == 2   # the other two lines still read


def test_a_given_to_something_that_is_not_a_property_is_reported(conn):
    sheet = worksheet.parse("80 = 80")
    assert sheet.problems and "property" in sheet.problems[0]


# ---- writing it ----

def test_applying_writes_motes_rules_applies_to_and_freedoms(conn):
    applied = worksheet.apply(conn, FEA)
    assert applied.motes_created == ("bd",)
    assert applied.givens_written == 2
    assert applied.rules_written == 1
    assert not applied.problems

    bd = named(conn, "bd")
    assert bd.properties["x"] == 80
    assert bd.properties[UNITS_KEY] == {"x": "in", "y": "in"}
    assert freedoms_of(conn, "bd") == ["l"]
    rule = list_rules(conn)[0]
    assert rule.equation == "bd.l == sqrt(bd.x^2 + bd.y^2)"
    assert rule.target_id == bd.id


def test_eight_lines_need_no_relationships_but_the_applies_to(conn):
    """The phase-2 point. FEA-Homework-4 had 10 relationships; this has
    one per rule, and every one of them says something."""
    worksheet.apply(conn, FEA)
    assert {r.type for r in list_relationships(conn)} == {APPLIES_TO}
    assert len(list_relationships(conn)) == 1


def test_an_applied_worksheet_solves(conn):
    """The answer is in base SI, because `l` declares no unit of its own --
    and the solve labels it `m`, so a surface can show the 100 inches the
    person typing in inches was after (see `core/units.py::convert`)."""
    worksheet.apply(conn, FEA)
    run_simulation(conn)
    bd = named(conn, "bd")
    assert bd.properties["l"] == pytest.approx(2.54)
    assert bd.properties[UNITS_KEY]["l"] == "m"
    assert convert(bd.properties["l"], "m", "in") == pytest.approx(100.0)


def test_a_rule_is_left_unnamed_so_its_equation_is_its_label(conn):
    worksheet.apply(conn, FEA)
    rule = list_rules(conn)[0]
    assert "name" not in rule.mote.properties
    assert rule.name == "bd.l == sqrt(bd.x^2 + bd.y^2)"


def test_a_trailing_comment_writes_a_real_name(conn):
    worksheet.apply(conn, "bd.Ax = ?\nbd.Bx = ?\nbd.Ax + bd.Bx == 0  ! ΣFx")
    assert list_rules(conn)[0].mote.properties["name"] == "ΣFx"


def test_removing_a_comment_removes_the_name(conn):
    worksheet.apply(conn, "bd.Ax + bd.Bx == 0  ! ΣFx")
    worksheet.apply(conn, "bd.Ax + bd.Bx == 0")
    assert "name" not in list_rules(conn)[0].mote.properties


def test_an_unknown_line_frees_the_key_without_a_value(conn):
    worksheet.apply(conn, "bd.Ax = ?")
    assert freedoms_of(conn, "bd") == ["Ax"]
    assert "Ax" not in named(conn, "bd").properties


def test_reapplying_an_unchanged_worksheet_writes_nothing_new(conn):
    worksheet.apply(conn, FEA)
    before = {m.id for m in list_motes(conn)}
    applied = worksheet.apply(conn, FEA)
    assert {m.id for m in list_motes(conn)} == before
    assert applied.deleted == ()
    assert len(list_rules(conn)) == 1


def test_an_existing_mote_is_used_rather_than_a_second_one_made(conn):
    existing = create_mote(conn, {"name": "bd", "note": "already here"})
    worksheet.apply(conn, FEA)
    assert named(conn, "bd").id == existing
    assert named(conn, "bd").properties["note"] == "already here"


def test_a_name_matching_two_motes_is_refused_rather_than_guessed(conn):
    create_mote(conn, {"name": "bd"})
    create_mote(conn, {"name": "bd"})
    applied = worksheet.apply(conn, "bd.x = 80")
    assert applied.givens_written == 0
    assert applied.problems and "names 2 motes" in applied.problems[0]


def test_a_relation_naming_no_mote_is_reported(conn):
    applied = worksheet.apply(conn, "x + y == 0")
    assert applied.problems and "which mote" in applied.problems[0]


# ---- applying replaces ----

def test_a_line_that_is_gone_deletes_its_rule(conn):
    worksheet.apply(conn, FEA + "\nbd.area = bd.x * bd.y")
    assert len(list_rules(conn)) == 2

    applied = worksheet.apply(conn, FEA)
    assert applied.deleted == ("bd.area == bd.x * bd.y",)
    assert equations_of(conn) == ["bd.l == sqrt(bd.x^2 + bd.y^2)"]


def test_editing_a_line_replaces_its_rule_rather_than_adding_one(conn):
    worksheet.apply(conn, "bd.x = 80\nbd.l = bd.x * 2")
    worksheet.apply(conn, "bd.x = 80\nbd.l = bd.x * 3")
    assert equations_of(conn) == ["bd.l == bd.x * 3"]


def test_a_species_scoped_rule_is_never_rendered_or_deleted(conn):
    """A rule about a species is a reusable model asserted once per
    instance -- not a line, and not a worksheet's to remove."""
    species = create_mote(conn, {"name": "Resistor", "freedoms": "current"})
    instance = create_mote(conn, {"name": "R1", "resistance": 100, "voltage": 12})
    create_relationship(conn, instance, species, INSTANCE_OF)
    rule = create_mote(conn, {"name": "Ohm", EQUATION_KEY: "voltage == current * resistance"})
    create_relationship(conn, rule, species, APPLIES_TO)

    applied = worksheet.apply(conn, FEA)
    assert applied.untouched == 1
    assert applied.deleted == ()
    assert get_mote(conn, rule) is not None
    assert "Ohm" not in worksheet.read(conn)


def test_nothing_is_deleted_while_a_line_is_broken(conn):
    """A half-written worksheet must not take the rules out from under the
    lines that are fine."""
    worksheet.apply(conn, FEA)
    applied = worksheet.apply(conn, "nonsense((")
    assert applied.problems
    assert applied.deleted == ()
    assert len(list_rules(conn)) == 1


# ---- reading it back ----

def test_a_worksheet_round_trips_verbatim(conn):
    worksheet.apply(conn, FEA)
    assert worksheet.read(conn) == FEA


def test_a_named_rule_round_trips_with_its_comment(conn):
    text = "bd.Ax = ?\nbd.Bx = ?\nbd.Ax + bd.Bx == 0  ! ΣFx"
    worksheet.apply(conn, text)
    assert worksheet.read(conn) == text


def test_a_solved_value_is_not_printed_as_a_given(conn):
    """It is the answer, not an input. Printing `bd.l = 100` would pin the
    very value the worksheet solves for."""
    worksheet.apply(conn, FEA)
    run_simulation(conn)
    assert worksheet.read(conn) == FEA


def test_applying_what_read_returned_deletes_nothing(conn):
    """What makes the round trip safe: the projection is complete, so
    re-applying it is a no-op rather than a deletion."""
    worksheet.apply(conn, "bd.x = 80\nbd.Ax = ?\nbd.Bx = ?\n\nbd.l = bd.x * 2\nbd.Ax + bd.Bx == 0")
    applied = worksheet.apply(conn, worksheet.read(conn))
    assert applied.deleted == ()
    assert applied.problems == ()
    assert len(list_rules(conn)) == 2


def test_an_equation_written_elsewhere_comes_back_as_a_line(conn):
    """A rule using bare names (`voltage == current * resistance`) has to
    come back saying which mote it is about, or the line would not know its
    own anchor.

    It keeps `==` rather than becoming an assignment, because `voltage` is
    not the freedom here -- `current` is, and it comes back as its own `= ?`
    line. A `=` would make `voltage` free on the next apply."""
    mote = create_mote(conn, {"name": "R1", "resistance": 100, "voltage": 12,
                              FREEDOMS_KEY: "current"})
    rule = create_mote(conn, {EQUATION_KEY: "voltage == current * resistance"})
    create_relationship(conn, rule, mote, APPLIES_TO)

    assert worksheet.read(conn) == (
        "R1.resistance = 100\n"
        "R1.voltage = 12\n"
        "R1.current = ?\n"
        "R1.voltage == R1.current * R1.resistance"
    )


def test_independent_calculations_read_as_separate_blocks(conn):
    """A big garden is many small calculations; each one's inputs sit with
    its equations rather than in one list of every given in the garden."""
    worksheet.apply(conn, "R1.v = 5\nR1.r = 100\nbd.x = 80\n\nR1.i = R1.v / R1.r\nbd.l = bd.x * 2")
    assert worksheet.read(conn) == (
        "R1.r = 100\n"
        "R1.v = 5\n"
        "R1.i = R1.v / R1.r\n"
        "\n"
        "bd.x = 80\n"
        "bd.l = bd.x * 2"
    )


def test_equations_sharing_a_mote_are_one_calculation(conn):
    worksheet.apply(conn, "a.x = 1\nb.y = 2\n\na.z = a.x * 2\nb.w = a.z + b.y")
    assert "\n\n" not in worksheet.read(conn)


def test_an_equation_reads_after_the_one_that_works_out_its_input(conn):
    """Top to bottom like a hand calculation, whatever order the rules were
    made in."""
    worksheet.apply(conn, "bd.x = 80\n\nbd.F = bd.l * 3\nbd.l = bd.x * 2")
    assert worksheet.read(conn).splitlines()[1:] == ["bd.l = bd.x * 2", "bd.F = bd.l * 3"]


def test_a_simultaneous_system_keeps_the_order_its_rules_were_made_in(conn):
    text = "s.a = ?\ns.b = ?\ns.a + s.b == 4\ns.a - s.b == 2"
    worksheet.apply(conn, text)
    assert worksheet.read(conn) == text


def test_a_rendered_assignment_only_ever_heads_a_real_freedom(conn):
    """The round-trip soundness rule: `=` makes its left side free when
    applied, so rendering one for a property that is not free would widen
    the model behind the reader's back."""
    mote = create_mote(conn, {"name": "R1", "resistance": 100, "voltage": 12,
                              FREEDOMS_KEY: "current"})
    rule = create_mote(conn, {EQUATION_KEY: "voltage == current * resistance"})
    create_relationship(conn, rule, mote, APPLIES_TO)

    worksheet.apply(conn, worksheet.read(conn))
    assert freedoms_of(conn, "R1") == ["current"]


def test_an_unnamed_anchor_leaves_its_equation_unqualified(conn):
    """Nothing to prefix with, so the relation comes back as written rather
    than mangled."""
    mote = create_mote(conn, {FREEDOMS_KEY: "current"})
    rule = create_mote(conn, {EQUATION_KEY: "voltage == current * 2"})
    create_relationship(conn, rule, mote, APPLIES_TO)
    assert worksheet.read(conn) == "voltage == current * 2"


def test_an_empty_garden_reads_as_an_empty_worksheet(conn):
    assert worksheet.read(conn) == ""


def test_a_mote_no_equation_mentions_contributes_no_givens(conn):
    """A homework mote with a due date is not worksheet material."""
    create_mote(conn, {"name": "HW 4", "estimate": 3})
    worksheet.apply(conn, FEA)
    assert "HW 4" not in worksheet.read(conn)


# ---- equality checks ----

BEAM = """\
b.W = 1000
b.a = 2
b.L = 5
b.R2 = b.W * b.a / b.L
b.R1 = b.W * (b.L - b.a) / b.L
b.R1 + b.R2 ?= b.W  ! ΣFy
"""


def test_a_question_equals_line_is_an_equality_check():
    (line,) = [l for l in worksheet.parse(BEAM).lines if l.check]
    assert line.kind == worksheet.EQUATION
    assert line.relation == "b.R1 + b.R2 == b.W"
    assert line.name == "ΣFy" and not line.free


def test_a_check_is_stored_as_equals_with_check_true_and_round_trips(conn):
    applied = worksheet.apply(conn, BEAM)
    assert applied.problems == ()
    (check,) = [r for r in list_rules(conn) if r.is_check]
    assert check.equation == "b.R1 + b.R2 == b.W"
    assert check.mote.properties["check"] is True

    run_simulation(conn)
    assert get_mote(conn, named(conn, "b").id).properties["R1"] == pytest.approx(600)

    text = worksheet.read(conn)
    assert "b.R1 + b.R2 ?= b.W  ! ΣFy" in text
    again = worksheet.apply(conn, text)
    assert again.deleted == () and again.motes_created == ()
    assert [r.is_check for r in list_rules(conn)].count(True) == 1


def test_retyping_a_check_with_double_equals_makes_it_an_equation_again(conn):
    worksheet.apply(conn, BEAM)
    worksheet.apply(conn, BEAM.replace("?=", "=="))
    assert not any(r.is_check for r in list_rules(conn))


# ---- a given's ± ----

@pytest.mark.parametrize("rhs, value, unit, spread", [
    ("80 ± 0.5 in", 80, "in", 0.5),
    ("80 +- 0.5 in", 80, "in", 0.5),
    ("80 +/- 0.5", 80, None, 0.5),
    ("80 ± 2% in", 80, "in", "2%"),
    ("80 in ± 0.5", 80, "in", 0.5),
])
def test_a_given_line_reads_its_spread(rhs, value, unit, spread):
    (line,) = worksheet.parse(f"bd.x = {rhs}").lines
    assert (line.kind, line.value, line.unit, line.spread) == (worksheet.GIVEN, value, unit, spread)


def test_an_equation_cannot_carry_a_spread():
    (line,) = worksheet.parse("bd.l = bd.x * 2 ± 1").lines
    assert "only a given can carry a ±" in line.problem


def test_a_spread_round_trips_and_a_removed_one_is_cleared(conn):
    text = "bd.x = 80 ± 0.5 in\nbd.y = 60 ± 2% in\nbd.l = sqrt(bd.x^2 + bd.y^2)\n"
    worksheet.apply(conn, text)
    assert named(conn, "bd").properties["uncertainty"] == {"x": 0.5, "y": "2%"}
    read = worksheet.read(conn)
    assert "bd.x = 80 ± 0.5 in" in read and "bd.y = 60 ± 2% in" in read
    worksheet.apply(conn, read)
    assert named(conn, "bd").properties["uncertainty"] == {"x": 0.5, "y": "2%"}

    worksheet.apply(conn, read.replace(" ± 0.5", "").replace(" ± 2%", ""))
    assert "uncertainty" not in named(conn, "bd").properties

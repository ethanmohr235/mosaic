"""core/health.py -- is this model solvable, and which line is wrong?

The case this module exists for is the last one here: a garden whose
equation and unknown totals balance perfectly while two of its blocks are
wrong. A report reading off the totals would call that healthy, which is
the most misleading thing it could say.
"""
import pytest

from core import health
from core import worksheet
from core.motes import create_mote, list_motes
from core.relationships import create_relationship
from core.rules import APPLIES_TO, EQUATION_KEY


def add_rule(conn, anchor_id: str, equation: str, name: str | None = None) -> str:
    properties = {EQUATION_KEY: equation}
    if name:
        properties["name"] = name
    rule_id = create_mote(conn, properties)
    create_relationship(conn, rule_id, anchor_id, APPLIES_TO)
    return rule_id


# ---- the counts ----

def test_a_complete_model_is_balanced(conn):
    worksheet.apply(conn, "bd.x = 80\nbd.l = bd.x * 2")
    report = health.check(conn)
    assert (report.equations, report.unknowns, report.checks) == (1, 1, 0)
    assert report.verdict == health.BALANCED
    assert report.solvable
    assert report.groups == ()


def test_a_check_is_counted_apart_from_the_equations(conn):
    """A comparison is checked rather than solved, so counting one among
    the equations would make a model look balanced because of a line that
    cannot determine anything."""
    worksheet.apply(conn, "bd.x = 80\nbd.l = bd.x * 2\nbd.l <= 300")
    report = health.check(conn)
    assert (report.equations, report.checks) == (1, 1)
    assert report.verdict == health.BALANCED


def test_a_check_is_reported_as_inert_rather_than_as_an_equation(conn):
    worksheet.apply(conn, "bd.x = 80\nbd.l = bd.x * 2\nbd.l <= 300")
    assert [i.equation for i in health.check(conn).inert] == ["bd.l <= 300"]


def test_a_relation_that_does_not_parse_counts_as_neither(conn):
    mote = create_mote(conn, {"name": "bd", "x": 80})
    add_rule(conn, mote, "this is not a relation")
    report = health.check(conn)
    assert (report.equations, report.checks) == (0, 0)


def test_the_summary_says_the_counts_and_the_verdict(conn):
    worksheet.apply(conn, "bd.x = 80\nbd.l = bd.x * 2\nbd.l <= 300")
    assert health.check(conn).summary == "1 equation, 1 unknown, 1 check -- balanced"


# ---- naming the culprits ----

def test_an_unconstrained_freedom_is_named(conn):
    worksheet.apply(conn, "bd.x = 80\nbd.q = ?\nbd.l = bd.x * 2")
    report = health.check(conn)
    assert [c.label for c in report.culprits] == ["bd.q"]
    assert "nothing can determine" in report.culprits[0].reason
    assert report.verdict == health.UNDERDETERMINED
    assert not report.solvable


def test_a_value_nothing_holds_is_named(conn):
    """The other half of what `requires` reports: a prerequisite that is
    neither stored nor derivable is the thing to go fix."""
    worksheet.apply(conn, "bd.l = ?\nbd.l == bd.x * 2")
    assert [c.label for c in health.check(conn).culprits] == ["bd.x"]


def test_a_deficient_block_names_its_unknowns_and_what_it_needs(conn):
    worksheet.apply(conn, """bd.x = 80
bd.Ax = ?
bd.Bx = ?
bd.l = bd.x * 2
bd.Ax + bd.Bx == 0""")
    report = health.check(conn)
    assert report.verdict == health.UNDERDETERMINED
    assert [g.labels for g in report.groups] == [("bd.Ax", "bd.Bx")]
    assert report.groups[0].shortfall == 1
    assert "another equation needed" in report.groups[0].message


def test_a_block_that_balances_is_not_reported(conn):
    """This list is meant to be short enough to read and act on, so a block
    with nothing wrong with it is left out."""
    worksheet.apply(conn, """bd.x = 80
bd.Ax = ?
bd.Bx = ?
bd.l = bd.x * 2
bd.Ax + bd.Bx == 0
bd.Ax - 10 == 0""")
    report = health.check(conn)
    assert report.groups == ()
    assert report.verdict == health.BALANCED


def test_a_surplus_block_is_reported_but_is_not_a_failure(conn):
    """Overdetermined is normal and fine as long as the extra equations
    agree -- in any circuit one Kirchhoff equation is implied by the
    others."""
    worksheet.apply(conn, "bd.x = 80\nbd.l = ?\nbd.l == bd.x * 2\nbd.l == 160")
    report = health.check(conn)
    assert report.verdict == health.OVERDETERMINED
    assert report.solvable
    assert report.groups[0].shortfall == -1
    assert "fine as long as they agree" in report.groups[0].message


def test_balanced_totals_can_still_be_an_underdetermined_model(conn):
    """The case this module exists for. Three equations, three unknowns --
    and two of the blocks are wrong."""
    worksheet.apply(conn, """bd.x = 80
bd.Ax = ?
bd.Bx = ?
bd.l = ?
bd.Ax + bd.Bx == 0
bd.l == bd.x * 2
bd.l == 160""")
    report = health.check(conn)
    assert report.equations == report.unknowns == 3
    assert report.verdict == health.UNDERDETERMINED
    assert not report.solvable
    assert [g.shortfall for g in report.groups] == [1, -1]


def test_a_check_cannot_make_a_deficient_block_solvable(conn):
    """An inequality determines nothing, so it is in no block at all."""
    worksheet.apply(conn, "bd.Ax = ?\nbd.Bx = ?\nbd.Ax + bd.Bx == 0\nbd.Ax <= 10")
    report = health.check(conn)
    assert report.verdict == health.UNDERDETERMINED
    assert report.groups[0].shortfall == 1


def test_an_empty_garden_is_balanced_and_says_nothing(conn):
    report = health.check(conn)
    assert (report.equations, report.unknowns) == (0, 0)
    assert report.verdict == health.BALANCED
    assert report.summary == "0 equations, 0 unknowns -- balanced"


# ---- units, per line ----

def test_a_dimensional_mismatch_is_reported_against_the_line(conn):
    """A worksheet rule is labelled by its own equation
    (core/rules.py::label_of), so the warning reads back as the line that
    caused it without this module arranging anything."""
    worksheet.apply(conn, "bd.x = 80 in\nbd.t = 3 s\nbd.l = bd.x + bd.t")
    problems = health.check(conn).unit_problems
    assert len(problems) == 1
    assert problems[0].startswith("bd.l == bd.x + bd.t on bd:")
    assert "different dimensions" in problems[0]


def test_a_consistent_garden_reports_no_unit_problems(conn):
    worksheet.apply(conn, "bd.x = 80 in\nbd.y = 60 in\nbd.l = bd.x + bd.y")
    assert health.check(conn).unit_problems == ()


def test_a_units_problem_alone_does_not_make_a_model_unsolvable(conn):
    """It is a statement about the model, not about whether the solver can
    find an answer -- and a mismatch is advisory by default everywhere else
    too (docs/simulation.md, *Checking equations*)."""
    worksheet.apply(conn, "bd.x = 80 in\nbd.t = 3 s\nbd.l = bd.x + bd.t")
    report = health.check(conn)
    assert report.unit_problems
    assert report.solvable


# ---- display vs stored units ----

def test_stored_unit_says_what_a_value_is_recorded_in(conn):
    """The half of display-vs-stored that lives in the garden. The other
    half is a surface's own state, and converting is core/units.py's."""
    worksheet.apply(conn, "bd.x = 80 in")
    properties = {"x": 80, "units": {"x": "in"}}
    assert health.stored_unit(properties, "x") == "in"
    assert health.stored_unit(properties, "y") is None


def test_stored_unit_survives_a_malformed_units_property(conn):
    assert health.stored_unit({"units": "nonsense"}, "x") is None


# ---- a scenario asks the same question of a hypothetical ----

def test_a_scenario_that_changes_freedoms_changes_the_health(conn):
    from core import scenarios as scenarios_repo

    worksheet.apply(conn, "R1.voltage = 12\nR1.current = ?\nR1.resistance = 100")
    anchor = next(m for m in list_motes(conn) if m.properties.get("name") == "R1")
    add_rule(conn, anchor.id, "R1.voltage == R1.current * R1.resistance", "Ohm")
    assert health.check(conn).verdict == health.BALANCED

    # Rate instead of size: the same equation, asked the other way.
    override = create_mote(conn, {"freedoms": "resistance"})
    create_relationship(conn, override, anchor.id, scenarios_repo.OVERRIDES)
    scenario = create_mote(conn, {"name": "Rating"})
    create_relationship(conn, scenario, override, scenarios_repo.VARIES)

    report = health.check(conn, scenario=scenario)
    assert report.unknowns == 1
    assert report.verdict == health.BALANCED

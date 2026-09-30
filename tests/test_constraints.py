"""Comparison relations: limits the solver checks but never satisfies.

`efficiency <= 1` is the same object as `voltage == current * resistance`
all the way through evaluation -- both become `lhs - rhs <op> 0` from one
walk of one grammar. The operator decides only what is done with the
result afterwards: solved for zero, or read for a sign. That is why this
costs a parser change and no second expression language.
"""
import pytest

from core.formula import EQUALS, FormulaError, parse_equation, relation_operator
from core.motes import create_mote, get_mote
from core.relationships import create_relationship
from core.rules import APPLIES_TO
from core import simulation
from core.simulation import (
    SimulationError,
    check_constraints,
    incidence_index,
    run_simulation,
    run_time_series,
)
from core.species import set_species


def add_rule(conn, target_id, name, equation):
    rule_id = create_mote(conn, {"name": name, "equation": equation})
    create_relationship(conn, rule_id, target_id, APPLIES_TO)
    return rule_id


@pytest.fixture
def turbine(conn):
    """A turbine whose efficiency comes out above 1 -- physically
    impossible, and exactly what a constraint is for."""
    species = create_mote(conn, {"name": "Turbine", "freedoms": "efficiency,work"})
    add_rule(conn, species, "Work", "work == efficiency * drop")
    add_rule(conn, species, "Efficiency", "efficiency == 1.2")
    mote = create_mote(conn, {"name": "T1", "drop": 100})
    set_species(conn, mote, species)
    return {"species": species, "mote": mote}


### parsing

def test_every_allowed_comparison_parses():
    for text, expected in (
        ("a == b", "=="), ("a <= b", "<="), ("a >= b", ">="),
        ("a < b", "<"), ("a > b", ">"),
    ):
        assert parse_equation(text).operator == expected


def test_an_equation_is_still_the_default_reading():
    assert relation_operator("voltage == current * resistance") == EQUALS
    assert parse_equation("a == b").is_equation


def test_a_disallowed_comparison_is_rejected():
    with pytest.raises(FormulaError, match="not allowed"):
        parse_equation("a != b")


def test_a_chained_comparison_is_rejected_with_advice():
    with pytest.raises(FormulaError, match="two relations"):
        parse_equation("0 <= x <= 1")


### checking

def test_a_violated_constraint_is_reported_not_fatal(conn, turbine):
    add_rule(conn, turbine["species"], "Efficiency limit", "efficiency <= 1")

    result = run_simulation(conn)
    assert len(result.constraint_violations) == 1
    assert "Efficiency limit on T1" in result.constraint_violations[0]
    assert "off by 0.2" in result.constraint_violations[0]
    # ...and the run still solved and wrote, which is the point of advisory.
    assert get_mote(conn, turbine["mote"]).properties["work"] == 120


def test_a_satisfied_constraint_says_nothing(conn, turbine):
    add_rule(conn, turbine["species"], "Efficiency limit", "efficiency <= 2")
    assert run_simulation(conn).constraint_violations == []


def test_strict_constraints_promotes_a_violation_to_a_failure(conn, turbine):
    add_rule(conn, turbine["species"], "Efficiency limit", "efficiency <= 1")

    with pytest.raises(SimulationError, match="constraint check failed"):
        run_simulation(conn, strict_constraints=True)


def test_a_non_strict_comparison_tolerates_the_boundary(conn):
    """`<= 1` landing on exactly 1.0 through floating point must pass."""
    species = create_mote(conn, {"name": "Thing", "freedoms": "x"})
    add_rule(conn, species, "Value", "x == 0.1 + 0.2")
    add_rule(conn, species, "Limit", "x <= 0.3")
    mote = create_mote(conn, {"name": "T1"})
    set_species(conn, mote, species)

    assert 0.1 + 0.2 != 0.3   # the float this guards against
    assert run_simulation(conn).constraint_violations == []


def test_a_strict_comparison_excludes_the_boundary(conn):
    species = create_mote(conn, {"name": "Thing", "freedoms": "x"})
    add_rule(conn, species, "Value", "x == 1")
    add_rule(conn, species, "Limit", "x < 1")
    mote = create_mote(conn, {"name": "T1"})
    set_species(conn, mote, species)

    assert run_simulation(conn).constraint_violations != []


def test_every_direction_works(conn):
    species = create_mote(conn, {"name": "Thing", "freedoms": "x"})
    add_rule(conn, species, "Value", "x == 5")
    for name, equation in (("ge", "x >= 9"), ("gt", "x > 9")):
        add_rule(conn, species, name, equation)
    mote = create_mote(conn, {"name": "T1"})
    set_species(conn, mote, species)

    violations = run_simulation(conn).constraint_violations
    assert len(violations) == 2


### unchecked rather than assumed

def test_a_constraint_on_an_undetermined_value_is_reported_as_unchecked(conn):
    """Silence would be indistinguishable from passing, and a limit
    nobody could evaluate is exactly the one worth mentioning."""
    species = create_mote(conn, {"name": "Thing", "freedoms": "x"})
    add_rule(conn, species, "Limit", "x <= 1")
    mote = create_mote(conn, {"name": "T1"})
    set_species(conn, mote, species)

    result = run_simulation(conn)
    assert len(result.constraint_violations) == 1
    assert "not checked" in result.constraint_violations[0]


def test_an_unchecked_constraint_is_strict_worthy_too(conn):
    species = create_mote(conn, {"name": "Thing", "freedoms": "x"})
    add_rule(conn, species, "Limit", "x <= 1")
    mote = create_mote(conn, {"name": "T1"})
    set_species(conn, mote, species)

    with pytest.raises(SimulationError, match="constraint check failed"):
        run_simulation(conn, strict_constraints=True)


### a constraint can never determine anything

def test_a_constraint_never_reaches_the_solver(conn):
    """`x <= 1` alone leaves x unsolved: an inequality asserts something
    about a value, it does not find one."""
    species = create_mote(conn, {"name": "Thing", "freedoms": "x"})
    add_rule(conn, species, "Limit", "x <= 1")
    mote = create_mote(conn, {"name": "T1"})
    set_species(conn, mote, species)

    result = run_simulation(conn)
    assert result.values == {}
    assert result.unsolved == [(mote, "x")]


def test_a_constraint_is_not_offered_as_a_producer(conn, turbine):
    add_rule(conn, turbine["species"], "Efficiency limit", "efficiency <= 1")
    index = incidence_index(conn)
    target = (turbine["mote"], "efficiency")

    assert "Efficiency limit" not in {t.rule_name for t in index.producers(target)}
    assert "Efficiency limit" in {t.rule_name for t in index.consumers(target)}


def test_a_constraint_still_counts_as_reading_what_it_touches(conn, turbine):
    add_rule(conn, turbine["species"], "Efficiency limit", "efficiency <= 1")
    index = incidence_index(conn)

    touch = next(t for t in index.touches if t.rule_name == "Efficiency limit")
    assert touch.determines == frozenset()
    assert (turbine["mote"], "efficiency") in touch.reads


### time series

def test_each_step_checks_its_own_constraints(conn):
    species = create_mote(conn, {"name": "Population", "freedoms": "size"})
    add_rule(conn, species, "Growth", "size == prev(size) * 2")
    add_rule(conn, species, "Capacity", "size <= 5")
    mote = create_mote(conn, {"name": "P1", "size": 1})
    set_species(conn, mote, species)

    result = run_time_series(conn, step_count=3)
    assert [s.constraint_violations == [] for s in result.steps] == [True, True, False]
    assert result.constraint_violations == [
        v for v in result.constraint_violations if v.startswith("step 2:")
    ]
    assert len(result.constraint_violations) == 1


### the checker understands them

def test_a_constraint_is_dimension_checked_like_any_relation(conn):
    """Both sides of a comparison must still agree dimensionally -- the
    dimension walk cares about the sides, not the operator."""
    from core.simulation import check_garden

    species = create_mote(conn, {"name": "Thing", "freedoms": "x"})
    add_rule(conn, species, "Limit", "length <= duration")
    mote = create_mote(conn, {
        "name": "T1", "length": 1, "duration": 2,
        "units": {"length": "m", "duration": "s"},
    })
    set_species(conn, mote, species)

    assert check_garden(conn).problems


# ---- check_constraints: the same limits, read off the garden ----
#
# A checklist needs every constraint including the passing ones, in a shape
# it can draw green or red -- and it must read the answers the last solve
# wrote rather than re-running it.

def test_check_constraints_reports_every_constraint_not_just_the_broken_ones(conn):
    species = create_mote(conn, {"name": "T", "freedoms": "efficiency"})
    add_rule(conn, species, "Physical", "efficiency <= 1")
    add_rule(conn, species, "Worthwhile", "efficiency >= 0.9")
    add_rule(conn, species, "Value", "efficiency == 0.95")
    instance = create_mote(conn, {"name": "T1"})
    set_species(conn, instance, species)
    run_simulation(conn)

    results = {r.equation: r for r in check_constraints(conn)}
    assert set(results) == {"efficiency <= 1", "efficiency >= 0.9"}
    assert all(r.status == simulation.PASS for r in results.values())
    assert all(r.passed for r in results.values())


def test_an_equation_is_never_in_the_constraint_list(conn):
    species = create_mote(conn, {"name": "T", "freedoms": "x"})
    add_rule(conn, species, "Value", "x == 4")
    set_species(conn, create_mote(conn, {"name": "T1"}), species)
    assert check_constraints(conn) == []


def test_a_broken_constraint_carries_how_far_off_it_is(conn):
    species = create_mote(conn, {"name": "T", "freedoms": "x"})
    add_rule(conn, species, "Value", "x == 5")
    add_rule(conn, species, "Limit", "x <= 3")
    set_species(conn, create_mote(conn, {"name": "T1"}), species)
    run_simulation(conn)

    (result,) = check_constraints(conn)
    assert result.status == simulation.FAIL
    assert result.off_by == pytest.approx(2)


def test_a_constraint_is_checked_against_the_value_the_solve_wrote(conn):
    """Freedoms are cleared here, so `x <= 3` reads the 5 on the mote rather
    than treating x as an unknown the way a solve would."""
    species = create_mote(conn, {"name": "T", "freedoms": "x"})
    add_rule(conn, species, "Value", "x == 5")
    add_rule(conn, species, "Limit", "x <= 3")
    set_species(conn, create_mote(conn, {"name": "T1"}), species)

    assert check_constraints(conn)[0].status == simulation.NOT_CHECKED
    run_simulation(conn)
    assert check_constraints(conn)[0].status == simulation.FAIL


def test_a_constraint_that_cannot_be_evaluated_is_not_checked_rather_than_raising(conn):
    """A note must render, so this path reports where a run would raise."""
    mote = create_mote(conn, {"name": "M"})
    rule = create_mote(conn, {"name": "Limit", "equation": "missing <= 3"})
    create_relationship(conn, rule, mote, APPLIES_TO)

    (result,) = check_constraints(conn)
    assert result.status == simulation.NOT_CHECKED
    assert result.error is None       # not a broken model to this caller
    assert "cannot be evaluated yet" in result.detail


def test_a_rule_that_does_not_parse_is_not_a_constraint(conn):
    mote = create_mote(conn, {"name": "M"})
    rule = create_mote(conn, {"name": "Broken", "equation": "this is not a relation"})
    create_relationship(conn, rule, mote, APPLIES_TO)
    assert check_constraints(conn) == []


### equality checks (`check: true`)

def add_check(conn, target_id, name, equation, **extra):
    rule_id = create_mote(conn, {"name": name, "equation": equation, "check": True, **extra})
    create_relationship(conn, rule_id, target_id, APPLIES_TO)
    return rule_id


@pytest.fixture
def beam(conn):
    """Two reactions found from moments, then vertical equilibrium checked
    rather than solved -- the textbook use of a check."""
    b = create_mote(conn, {"name": "b", "W": 1000, "a": 2, "L": 5, "freedoms": "R1, R2"})
    add_rule(conn, b, "Moment about 1", "R2 * L == W * a")
    add_rule(conn, b, "Moment about 2", "R1 * L == W * (L - a)")
    return b


def test_a_marked_equality_is_checked_and_never_solved(conn, beam):
    add_check(conn, beam, "ΣFy", "R1 + R2 == W")
    result = run_simulation(conn)
    assert result.constraint_violations == []
    assert get_mote(conn, beam).properties["R1"] == pytest.approx(600)
    (check,) = check_constraints(conn)
    assert check.status == simulation.PASS and check.equation == "R1 + R2 == W"


def test_a_check_cannot_stand_in_for_a_missing_equation(conn):
    b = create_mote(conn, {"name": "b", "W": 10, "freedoms": "R"})
    add_check(conn, b, "Check", "R == W")
    result = run_simulation(conn)
    assert result.unsolved == [(b, "R")]


def test_an_equality_check_fails_beyond_its_relative_tolerance(conn, beam):
    add_check(conn, beam, "ΣFy", "R1 + R2 == W * 1.01")
    run_simulation(conn)
    (check,) = check_constraints(conn)
    assert check.status == simulation.FAIL
    assert "off by 10" in check.detail and "0.001 relative" in check.detail


def test_a_wider_tolerance_passes_the_same_check(conn, beam):
    add_check(conn, beam, "ΣFy", "R1 + R2 == W * 1.01", tolerance=0.02)
    run_simulation(conn)
    (check,) = check_constraints(conn)
    assert check.status == simulation.PASS


def test_rounding_within_the_default_tolerance_passes(conn, beam):
    add_check(conn, beam, "ΣFy", "R1 + R2 == 1000.4")
    run_simulation(conn)
    assert check_constraints(conn)[0].passed


def test_a_check_determines_nothing_in_the_incidence_index(conn, beam):
    check_id = add_check(conn, beam, "ΣFy", "R1 + R2 == W")
    touch = next(t for t in incidence_index(conn).touches if t.rule_mote_id == check_id)
    assert touch.check and touch.determines == frozenset()
    assert check_id not in {e.rule_mote_id for e in incidence_index(conn).producers((beam, "R1"))}


def test_only_a_real_true_marks_a_check():
    from core.rules import is_check, tolerance_of, DEFAULT_TOLERANCE
    assert is_check({"check": True}) and is_check({"check": "True "})
    assert not is_check({"check": 1}) and not is_check({"check": "yes"}) and not is_check({})
    assert tolerance_of({"tolerance": 0.05}) == 0.05
    for bad in (0, -1, "x", True, None):
        assert tolerance_of({"tolerance": bad}) == DEFAULT_TOLERANCE


def test_solve_health_counts_a_marked_equality_as_a_check(conn, beam):
    from core import health
    add_check(conn, beam, "ΣFy", "R1 + R2 == W")
    report = health.check(conn)
    assert (report.equations, report.unknowns, report.checks) == (2, 2, 1)
    assert report.solvable

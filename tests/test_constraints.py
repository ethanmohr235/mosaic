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
from core.simulation import (
    SimulationError,
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

    result = run_time_series(conn, step_count=3, commit=False)
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

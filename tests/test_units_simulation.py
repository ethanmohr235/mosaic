"""Units end-to-end through the solver.

The unit tests in test_units.py and test_dimensions.py cover conversion
and checking in isolation; these prove the two guarantees that only show
up once a real garden is solved: mixed units combine correctly, and a
solve never changes the unit a property is recorded in.
"""
import pytest

from core.motes import create_mote, get_mote, update_mote
from core.relationships import create_relationship
from core.rules import APPLIES_TO
from core.scenarios import OVERRIDES, VARIES
from core.simulation import SimulationError, list_runs, run_simulation, run_time_series
from core.species import set_species


def add_rule(conn, species_id, name, equation):
    rule_id = create_mote(conn, {"name": name, "equation": equation})
    create_relationship(conn, rule_id, species_id, APPLIES_TO)
    return rule_id


def make_scenario(conn, name, target_id, properties):
    override = create_mote(conn, properties)
    create_relationship(conn, override, target_id, OVERRIDES)
    scenario = create_mote(conn, {"name": name})
    create_relationship(conn, scenario, override, VARIES)
    return scenario


def prop(conn, mote_id, key):
    return get_mote(conn, mote_id).properties[key]


@pytest.fixture
def totals(conn):
    """A Tally species whose `total` is the sum of its parts' `amount`.

    Deliberately the simplest graph that can expose a unit mistake: if
    the two parts' differing units aren't reconciled, the sum is wrong by
    a factor of a thousand rather than subtly off.
    """
    part = create_mote(conn, {"name": "Part"})
    tally = create_mote(conn, {"name": "Tally", "freedoms": "total"})
    add_rule(conn, tally, "Total", "total == sum(targets('part').amount)")
    return {"part": part, "tally": tally}


# ---- mixed units combine correctly ----

def test_properties_in_different_units_are_reconciled(conn, totals):
    # 1 kohm + 500 ohm is 1500 ohm, not 501 of anything.
    big = create_mote(conn, {"name": "Big", "amount": 1, "units": {"amount": "kohm"}})
    small = create_mote(conn, {"name": "Small", "amount": 500, "units": {"amount": "ohm"}})
    set_species(conn, big, totals["part"])
    set_species(conn, small, totals["part"])

    tally = create_mote(conn, {"name": "T", "units": {"total": "ohm"}})
    set_species(conn, tally, totals["tally"])
    create_relationship(conn, tally, big, "part")
    create_relationship(conn, tally, small, "part")

    run_simulation(conn)
    assert prop(conn, tally, "total") == pytest.approx(1500.0)


def test_the_result_is_written_in_the_unit_the_mote_declares(conn, totals):
    # Same inputs as above, but the tally keeps its total in kohm -- the
    # answer must follow the mote's own declaration, not the solver's
    # internal base units.
    big = create_mote(conn, {"name": "Big", "amount": 1, "units": {"amount": "kohm"}})
    small = create_mote(conn, {"name": "Small", "amount": 500, "units": {"amount": "ohm"}})
    set_species(conn, big, totals["part"])
    set_species(conn, small, totals["part"])

    tally = create_mote(conn, {"name": "T", "units": {"total": "kohm"}})
    set_species(conn, tally, totals["tally"])
    create_relationship(conn, tally, big, "part")
    create_relationship(conn, tally, small, "part")

    run_simulation(conn)
    assert prop(conn, tally, "total") == pytest.approx(1.5)


def test_an_undeclared_result_is_written_in_base_units(conn, totals):
    big = create_mote(conn, {"name": "Big", "amount": 1, "units": {"amount": "kohm"}})
    set_species(conn, big, totals["part"])

    tally = create_mote(conn, {"name": "T"})   # no units declared
    set_species(conn, tally, totals["tally"])
    create_relationship(conn, tally, big, "part")

    run_simulation(conn)
    assert prop(conn, tally, "total") == pytest.approx(1000.0)


def test_an_affine_unit_is_converted_with_its_offset(conn):
    # 25 degC is 298.15 K, which is the whole reason Unit carries an
    # offset -- a plain scale factor would make it 25 K.
    species = create_mote(conn, {"name": "Reading", "freedoms": "kelvin"})
    add_rule(conn, species, "Convert", "kelvin == celsius")
    reading = create_mote(conn, {
        "name": "R", "celsius": 25, "units": {"celsius": "degC", "kelvin": "K"},
    })
    set_species(conn, reading, species)

    run_simulation(conn)
    assert prop(conn, reading, "kelvin") == pytest.approx(298.15)


def test_a_compound_unit_is_converted(conn):
    species = create_mote(conn, {"name": "Trip", "freedoms": "metres_per_second"})
    add_rule(conn, species, "Convert", "metres_per_second == speed")
    trip = create_mote(conn, {
        "name": "T", "speed": 36,
        "units": {"speed": "km/hr", "metres_per_second": "m/s"},
    })
    set_species(conn, trip, species)

    run_simulation(conn)
    assert prop(conn, trip, "metres_per_second") == pytest.approx(10.0)


# ---- the declared unit survives ----

def test_solving_does_not_rewrite_a_property_into_base_units(conn):
    """The guarantee that makes units safe to adopt: a value the user
    keeps in kohm is still in kohm after the solver touches it."""
    species = create_mote(conn, {"name": "Element", "freedoms": "resistance"})
    add_rule(conn, species, "Series", "resistance == first + second")
    element = create_mote(conn, {
        "name": "E", "first": 1, "second": 2,
        "units": {"first": "kohm", "second": "kohm", "resistance": "kohm"},
    })
    set_species(conn, element, species)

    run_simulation(conn)
    # 3 kohm, recorded as 3 -- not 3000.
    assert prop(conn, element, "resistance") == pytest.approx(3.0)


def test_a_run_is_idempotent_with_units_declared(conn):
    """Re-running must not drift. This is the test that would fail if any
    path converted a value twice."""
    species = create_mote(conn, {"name": "Element", "freedoms": "resistance"})
    add_rule(conn, species, "Series", "resistance == first + second")
    element = create_mote(conn, {
        "name": "E", "first": 1, "second": 2,
        "units": {"first": "kohm", "second": "kohm", "resistance": "kohm"},
    })
    set_species(conn, element, species)

    run_simulation(conn)
    first = prop(conn, element, "resistance")
    run_simulation(conn)
    assert prop(conn, element, "resistance") == pytest.approx(first)


def test_a_recurrence_does_not_drift_across_steps(conn):
    """prev(x) reads the running state, which is kept in the mote's own
    units -- if a step stored base-unit values there, step 2 would
    convert them again and the series would explode."""
    species = create_mote(conn, {"name": "Account", "freedoms": "balance"})
    add_rule(conn, species, "Growth", "balance == prev(balance) * 2")
    account = create_mote(conn, {
        "name": "A", "balance": 1, "units": {"balance": "kohm"},
    })
    set_species(conn, account, species)

    result = run_time_series(conn, step_count=3, commit=True)
    assert result.failed_at is None
    # Doubling three times from 1 kohm, still expressed in kohm.
    assert prop(conn, account, "balance") == pytest.approx(8.0)


def test_time_series_changes_are_logged_in_the_declared_unit(conn):
    species = create_mote(conn, {"name": "Account", "freedoms": "balance"})
    add_rule(conn, species, "Growth", "balance == prev(balance) * 2")
    account = create_mote(conn, {
        "name": "A", "balance": 1, "units": {"balance": "kohm"},
    })
    set_species(conn, account, species)

    result = run_time_series(conn, step_count=2, commit=False)
    values = [c.new_value for step in result.steps for c in step.changes]
    assert values == pytest.approx([2.0, 4.0])


# ---- bad input ----

def test_an_unrecognized_unit_fails_the_run_rather_than_being_ignored(conn):
    # Silently treating 'kilohms' as base ohms would be a factor-of-1000
    # error in the answer, which is worse than a clear failure.
    species = create_mote(conn, {"name": "Element", "freedoms": "total"})
    add_rule(conn, species, "Copy", "total == source")
    element = create_mote(conn, {
        "name": "E", "source": 5, "units": {"source": "kilohms"},
    })
    set_species(conn, element, species)

    with pytest.raises(SimulationError, match="kilohms"):
        run_simulation(conn)


def test_a_malformed_units_property_leaves_the_mote_unannotated(conn):
    # A half-typed `units` value shouldn't break a simulation.
    species = create_mote(conn, {"name": "Element", "freedoms": "total"})
    add_rule(conn, species, "Copy", "total == source")
    element = create_mote(conn, {"name": "E", "source": 5, "units": "ohm"})
    set_species(conn, element, species)

    run_simulation(conn)
    assert prop(conn, element, "total") == pytest.approx(5.0)


# ---- dimensional checking during a run ----

@pytest.fixture
def mismatched(conn):
    """A rule that adds a resistance to a current -- dimensionally wrong,
    but perfectly solvable as bare numbers."""
    species = create_mote(conn, {"name": "Element", "freedoms": "total"})
    add_rule(conn, species, "Bad sum", "total == resistance + current")
    element = create_mote(conn, {
        "name": "E", "resistance": 1, "current": 2,
        "units": {"resistance": "ohm", "current": "A", "total": "ohm"},
    })
    set_species(conn, element, species)
    return element


def test_a_mismatch_is_reported_but_does_not_fail_the_run(conn, mismatched):
    result = run_simulation(conn)
    assert len(result.dimension_warnings) == 1
    assert "Bad sum on E" in result.dimension_warnings[0]
    # Still solved, still written -- the check is advisory.
    assert prop(conn, mismatched, "total") == pytest.approx(3.0)


def test_the_summary_mentions_dimensional_warnings(conn, mismatched):
    assert "1 dimensional warning." in run_simulation(conn).summary


def test_strict_units_turns_a_mismatch_into_a_failure(conn, mismatched):
    with pytest.raises(SimulationError, match="dimensional check failed"):
        run_simulation(conn, strict_units=True)


def test_strict_units_does_not_write_anything(conn, mismatched):
    with pytest.raises(SimulationError):
        run_simulation(conn, strict_units=True)
    assert "total" not in get_mote(conn, mismatched).properties


def test_a_consistent_garden_passes_strict_units(conn):
    species = create_mote(conn, {"name": "Element", "freedoms": "total"})
    add_rule(conn, species, "Series", "total == first + second")
    element = create_mote(conn, {
        "name": "E", "first": 1, "second": 2,
        "units": {"first": "ohm", "second": "kohm", "total": "ohm"},
    })
    set_species(conn, element, species)

    result = run_simulation(conn, strict_units=True)
    assert result.dimension_warnings == []
    assert prop(conn, element, "total") == pytest.approx(2001.0)


def test_time_series_reports_warnings_once_not_per_step(conn, mismatched):
    result = run_time_series(conn, step_count=3, commit=False)
    assert len(result.dimension_warnings) == 1


def test_time_series_honours_strict_units(conn, mismatched):
    with pytest.raises(SimulationError, match="dimensional check failed"):
        run_time_series(conn, step_count=2, commit=False, strict_units=True)


# ---- a solved freedom with no declared unit gets a reasonable one ----

def test_a_solved_freedom_with_no_unit_is_given_one(conn):
    # resistance == voltage / current is the entire equation on both
    # sides, so the solver can name resistance's dimension outright.
    species = create_mote(conn, {"name": "Element", "freedoms": "resistance"})
    add_rule(conn, species, "Ohm", "resistance == voltage / current")
    element = create_mote(conn, {
        "name": "E", "voltage": 10, "current": 2,
        "units": {"voltage": "V", "current": "A"},
    })
    set_species(conn, element, species)

    run_simulation(conn)
    assert prop(conn, element, "resistance") == pytest.approx(5.0)
    assert get_mote(conn, element).properties["units"]["resistance"] == "ohm"


def test_the_inferred_unit_survives_a_second_run(conn):
    species = create_mote(conn, {"name": "Element", "freedoms": "resistance"})
    add_rule(conn, species, "Ohm", "resistance == voltage / current")
    element = create_mote(conn, {
        "name": "E", "voltage": 10, "current": 2,
        "units": {"voltage": "V", "current": "A"},
    })
    set_species(conn, element, species)

    run_simulation(conn)
    run_simulation(conn)
    assert prop(conn, element, "resistance") == pytest.approx(5.0)
    assert get_mote(conn, element).properties["units"]["resistance"] == "ohm"


def test_an_unnamed_dimension_falls_back_to_a_base_unit_product(conn):
    species = create_mote(conn, {"name": "Trip", "freedoms": "speed"})
    add_rule(conn, species, "Rate", "speed == distance / time")
    trip = create_mote(conn, {
        "name": "T", "distance": 100, "time": 10,
        "units": {"distance": "m", "time": "s"},
    })
    set_species(conn, trip, species)

    run_simulation(conn)
    assert prop(conn, trip, "speed") == pytest.approx(10.0)
    assert get_mote(conn, trip).properties["units"]["speed"] == "m*s^-1"


def test_a_dimensionless_result_gets_no_unit(conn):
    species = create_mote(conn, {"name": "Ratio", "freedoms": "fraction"})
    add_rule(conn, species, "Divide", "fraction == part / whole")
    element = create_mote(conn, {
        "name": "R", "part": 1, "whole": 4,
        "units": {"part": "kg", "whole": "kg"},
    })
    set_species(conn, element, species)

    run_simulation(conn)
    assert prop(conn, element, "fraction") == pytest.approx(0.25)
    assert "fraction" not in get_mote(conn, element).properties.get("units", {})


def test_a_freedom_buried_inside_a_larger_expression_is_not_guessed_at(conn):
    # resistance never appears alone on either side here, so there is
    # nothing safe to name it from -- the solve still works.
    species = create_mote(conn, {"name": "Element", "freedoms": "resistance"})
    add_rule(conn, species, "Doubled", "voltage == 2 * resistance")
    element = create_mote(conn, {"name": "E", "voltage": 10, "units": {"voltage": "V"}})
    set_species(conn, element, species)

    run_simulation(conn)
    assert prop(conn, element, "resistance") == pytest.approx(5.0)
    assert "resistance" not in get_mote(conn, element).properties.get("units", {})


def test_a_declared_unit_is_never_overridden_by_inference(conn):
    species = create_mote(conn, {"name": "Element", "freedoms": "resistance"})
    add_rule(conn, species, "Ohm", "resistance == voltage / current")
    element = create_mote(conn, {
        "name": "E", "voltage": 10, "current": 2,
        "units": {"voltage": "V", "current": "A", "resistance": "kohm"},
    })
    set_species(conn, element, species)

    run_simulation(conn)
    assert prop(conn, element, "resistance") == pytest.approx(0.005)
    assert get_mote(conn, element).properties["units"]["resistance"] == "kohm"


def test_a_scenario_run_never_persists_an_inferred_unit(conn):
    species = create_mote(conn, {"name": "Element", "freedoms": "resistance"})
    add_rule(conn, species, "Ohm", "resistance == voltage / current")
    element = create_mote(conn, {
        "name": "E", "voltage": 10, "current": 2,
        "units": {"voltage": "V", "current": "A"},
    })
    set_species(conn, element, species)
    scenario = make_scenario(conn, "Hypothetical", element, {"current": 5})

    run_simulation(conn, scenario=scenario)
    assert "resistance" not in get_mote(conn, element).properties.get("units", {})
    assert "resistance" not in get_mote(conn, element).properties


def test_an_inferred_unit_is_also_applied_by_a_committed_time_series(conn):
    species = create_mote(conn, {"name": "Element", "freedoms": "resistance"})
    add_rule(conn, species, "Ohm", "resistance == voltage / current")
    element = create_mote(conn, {
        "name": "E", "voltage": 10, "current": 2,
        "units": {"voltage": "V", "current": "A"},
    })
    set_species(conn, element, species)

    run_time_series(conn, step_count=1, commit=True)
    assert prop(conn, element, "resistance") == pytest.approx(5.0)
    assert get_mote(conn, element).properties["units"]["resistance"] == "ohm"


# ---- backward compatibility ----

def test_an_unannotated_garden_is_untouched(conn, totals):
    part_a = create_mote(conn, {"name": "A", "amount": 1})
    part_b = create_mote(conn, {"name": "B", "amount": 500})
    set_species(conn, part_a, totals["part"])
    set_species(conn, part_b, totals["part"])

    tally = create_mote(conn, {"name": "T"})
    set_species(conn, tally, totals["tally"])
    create_relationship(conn, tally, part_a, "part")
    create_relationship(conn, tally, part_b, "part")

    run_simulation(conn)
    assert prop(conn, tally, "total") == pytest.approx(501.0)


def test_removing_the_units_property_restores_raw_behaviour(conn):
    species = create_mote(conn, {"name": "Element", "freedoms": "total"})
    add_rule(conn, species, "Copy", "total == source")
    element = create_mote(conn, {
        "name": "E", "source": 2, "units": {"source": "kohm", "total": "ohm"},
    })
    set_species(conn, element, species)

    run_simulation(conn)
    assert prop(conn, element, "total") == pytest.approx(2000.0)

    update_mote(conn, element, {"units": {}})
    run_simulation(conn)
    assert prop(conn, element, "total") == pytest.approx(2.0)


def test_a_bad_unit_on_a_solved_unknown_is_caught_and_logged(conn):
    """The write-back conversion happens after a run's error handling, so
    an unrecognized unit there has to be caught before solving starts --
    otherwise the failure escapes unlogged."""
    species = create_mote(conn, {"name": "S", "freedoms": "total"})
    add_rule(conn, species, "Assign", "total == 5")
    element = create_mote(conn, {"name": "E", "units": {"total": "kilohms"}})
    set_species(conn, element, species)

    with pytest.raises(SimulationError, match="kilohms"):
        run_simulation(conn)
    # Logged like any other failed run.
    assert [r.status for r in list_runs(conn)] == ["error"]


def test_a_bad_unit_on_an_unrelated_mote_does_not_fail_the_run(conn, totals):
    """Units are opt-in; a typo somewhere the run never touches must not
    break a simulation of a different part of the graph."""
    part = create_mote(conn, {"name": "P", "amount": 2, "units": {"amount": "ohm"}})
    set_species(conn, part, totals["part"])
    tally = create_mote(conn, {"name": "T", "units": {"total": "ohm"}})
    set_species(conn, tally, totals["tally"])
    create_relationship(conn, tally, part, "part")

    create_mote(conn, {"name": "Unrelated", "units": {"whatever": "kilohms"}})

    run_simulation(conn)
    assert prop(conn, tally, "total") == pytest.approx(2.0)

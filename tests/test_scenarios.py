"""Scenarios: asking a garden more than one question without editing it.

The shape mirrors templates one arrow over -- an override holds
replacement properties for one mote the way a template holds placeholder
properties for a species, and is excluded from runs the same way. What is
under test is mostly that the graph does not move: a scenario run answers
what *would* happen, and the answer to a hypothetical is not state.
"""
import pytest

from core.motes import create_mote, get_mote
from core.relationships import create_relationship
from core.rules import APPLIES_TO
from core.scenarios import (
    OVERRIDES,
    VARIES,
    Override,
    Scenario,
    apply,
    get_scenario,
    list_scenarios,
)
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


def make_scenario(conn, name, target_id, properties):
    override = create_mote(conn, properties)
    create_relationship(conn, override, target_id, OVERRIDES)
    scenario = create_mote(conn, {"name": name})
    create_relationship(conn, scenario, override, VARIES)
    return scenario


@pytest.fixture
def resistor(conn):
    species = create_mote(conn, {"name": "Resistor", "freedoms": "voltage"})
    add_rule(conn, species, "Ohm's law", "voltage == current * resistance")
    r1 = create_mote(conn, {"name": "R1", "current": 2, "resistance": 3})
    set_species(conn, r1, species)
    return r1


### the graph does not move

def test_a_scenario_run_reports_changes_without_making_them(conn, resistor):
    scenario = make_scenario(conn, "High Load", resistor, {"current": 10})

    result = run_simulation(conn, scenario=scenario)
    assert [(c.key, c.new_value) for c in result.changes] == [("voltage", 30)]
    assert "voltage" not in get_mote(conn, resistor).properties


def test_a_scenario_run_does_not_disturb_a_previous_real_answer(conn, resistor):
    run_simulation(conn)
    assert get_mote(conn, resistor).properties["voltage"] == 6

    run_simulation(conn, scenario=make_scenario(conn, "High", resistor, {"current": 10}))
    assert get_mote(conn, resistor).properties["voltage"] == 6


def test_a_scenario_run_names_its_scenario(conn, resistor):
    scenario = make_scenario(conn, "High Load", resistor, {"current": 10})
    assert run_simulation(conn, scenario=scenario).scenario_id == scenario
    assert run_simulation(conn).scenario_id is None


def test_a_time_series_scenario_leaves_the_graph_alone(conn, resistor):
    scenario = make_scenario(conn, "High", resistor, {"current": 10})
    result = run_time_series(conn, step_count=1, scenario=scenario)

    assert result.steps[0].changes[0].new_value == 30
    assert "voltage" not in get_mote(conn, resistor).properties


### what a scenario can change

def test_a_scenario_changes_only_the_properties_it_names(conn, resistor):
    scenario = make_scenario(conn, "High", resistor, {"current": 10})
    result = run_simulation(conn, scenario=scenario)

    # resistance was never overridden, so 10 * 3 rather than 10 * anything else
    assert result.changes[0].new_value == 30


def test_a_scenario_can_change_the_question_not_just_the_inputs(conn, resistor):
    """An override carrying `freedoms` turns sizing into rating. This is
    the reason freedoms had to move off the species first."""
    scenario = make_scenario(
        conn, "Rating", resistor, {"freedoms": "resistance", "voltage": 24}
    )

    result = run_simulation(conn, scenario=scenario)
    assert [(c.key, c.new_value) for c in result.changes] == [("resistance", 12)]
    assert get_mote(conn, resistor).properties["resistance"] == 3


def test_two_scenarios_give_different_answers_from_one_garden(conn, resistor):
    low = make_scenario(conn, "Low", resistor, {"current": 1})
    high = make_scenario(conn, "High", resistor, {"current": 10})

    assert run_simulation(conn, scenario=low).changes[0].new_value == 3
    assert run_simulation(conn, scenario=high).changes[0].new_value == 30
    assert "voltage" not in get_mote(conn, resistor).properties


def test_a_scenario_composes_with_a_time_series(conn):
    species = create_mote(conn, {"name": "Population", "freedoms": "size"})
    add_rule(conn, species, "Growth", "size == prev(size) * rate")
    mote = create_mote(conn, {"name": "P1", "size": 1, "rate": 2})
    set_species(conn, mote, species)
    scenario = make_scenario(conn, "Fast", mote, {"rate": 3})

    result = run_time_series(conn, step_count=3, scenario=scenario)
    assert [list(step.values.values())[0] for step in result.steps] == [3, 9, 27]


### override motes are not part of the graph being solved

def test_an_override_mote_is_never_solved_in_its_own_right(conn, resistor):
    """It holds replacement values, not state. Left in, an override
    declaring `freedoms` would become solvable itself and its values
    would be read as some mote's real data."""
    make_scenario(conn, "Rating", resistor, {"freedoms": "resistance", "voltage": 24})

    result = run_simulation(conn)
    assert result.unsolved == []
    assert [(c.key, c.new_value) for c in result.changes] == [("voltage", 6)]


def test_an_override_is_excluded_even_with_no_scenario_applied(conn, resistor):
    override = create_mote(conn, {"freedoms": "anything"})
    create_relationship(conn, override, resistor, OVERRIDES)

    assert all(mote_id != override for mote_id, _ in run_simulation(conn).unsolved)


### resolution

def test_an_unknown_scenario_id_fails_rather_than_running_the_garden(conn, resistor):
    """Silently answering a different question than the one asked is the
    worst possible response to a mistyped id."""
    plain = create_mote(conn, {"name": "Not a scenario"})
    with pytest.raises(SimulationError, match="no scenario"):
        run_simulation(conn, scenario=plain)


def test_an_override_pointing_at_nothing_is_skipped_not_fatal(conn, resistor):
    scenario = create_mote(conn, {"name": "Half built"})
    orphan = create_mote(conn, {"current": 99})
    create_relationship(conn, scenario, orphan, VARIES)

    assert get_scenario(conn, scenario).overrides == ()
    assert run_simulation(conn, scenario=scenario).changes[0].new_value == 6


def test_apply_is_pure():
    """No connection, no motes -- apply() takes a properties dict and
    returns a new one, which is exactly why scenarios cost so little:
    the run machinery was already driven from a dict."""
    base = {"m1": {"x": 1, "y": 2}}
    scenario = Scenario("s1", "S", (Override("o1", "m1", {"x": 9}),))

    perturbed = apply(base, scenario)

    assert base == {"m1": {"x": 1, "y": 2}}
    assert perturbed == {"m1": {"x": 9, "y": 2}}   # merged per property, not wholesale
    assert base["m1"] is not perturbed["m1"]


def test_apply_with_no_scenario_returns_the_properties_unchanged():
    base = {"a": {"x": 1}}
    assert apply(base, None) is base


### listing

def test_list_scenarios_finds_every_mote_that_varies_something(conn, resistor):
    make_scenario(conn, "Beta", resistor, {"current": 1})
    make_scenario(conn, "Alpha", resistor, {"current": 2})

    assert [s.name for s in list_scenarios(conn)] == ["Alpha", "Beta"]


def test_a_mote_that_varies_nothing_is_not_a_scenario(conn, resistor):
    plain = create_mote(conn, {"name": "Just a mote"})
    assert get_scenario(conn, plain) is None
    assert list_scenarios(conn) == []


### the structural view follows the scenario too

def test_the_incidence_index_can_be_read_under_a_scenario(conn, resistor):
    """A scenario that changes `freedoms` changes what is determinable,
    which is a structural question worth asking before solving."""
    scenario = make_scenario(
        conn, "Rating", resistor, {"freedoms": "resistance", "voltage": 24}
    )

    assert incidence_index(conn).producers((resistor, "resistance")) == ()
    assert incidence_index(conn, scenario=scenario).producers((resistor, "resistance"))

"""Freedoms: which properties the solver may determine, and for whom.

`freedoms` used to be `unknowns` and used to live only on a species,
which asserted that every instance of a type has the same things
determined for it, forever. That is false of most real modelling: the
same turbine is *sized* in one problem (given power, find mass flow) and
*rated* in another, from the same equations. So the declaration moved to
the mote, defaulting to its species.
"""
import pytest

from core.motes import create_mote, get_mote, update_mote
from core.relationships import create_relationship
from core.rules import FREEDOMS_KEY, APPLIES_TO, parse_freedoms
from core.simulation import SimulationError, run_simulation
from core.species import set_species


def add_rule(conn, target_id, name, equation):
    rule_id = create_mote(conn, {"name": name, "equation": equation})
    create_relationship(conn, rule_id, target_id, APPLIES_TO)
    return rule_id


@pytest.fixture
def ohm(conn):
    """One equation, `voltage == current * resistance`, over two
    resistors of the same species. Which value it determines is the only
    thing that changes between the tests below."""
    species = create_mote(conn, {"name": "Resistor", FREEDOMS_KEY: "voltage"})
    add_rule(conn, species, "Ohm's law", "voltage == current * resistance")
    r1 = create_mote(conn, {"name": "R1", "current": 2, "resistance": 3})
    r2 = create_mote(conn, {"name": "R2", "current": 5, "resistance": 4})
    set_species(conn, r1, species)
    set_species(conn, r2, species)
    return {"species": species, "r1": r1, "r2": r2}


### resolution order

def test_a_species_declaration_still_applies_to_every_instance(conn, ohm):
    run_simulation(conn)
    assert get_mote(conn, ohm["r1"]).properties["voltage"] == 6
    assert get_mote(conn, ohm["r2"]).properties["voltage"] == 20


def test_a_mote_replaces_its_species_declaration_rather_than_adding_to_it(conn, ohm):
    """Replacing, not merging, is what lets a mote *narrow* its freedoms.
    Here R1 is asked the inverse question -- given the voltage, what is
    the current? -- while R2 is left alone."""
    update_mote(conn, ohm["r1"], {FREEDOMS_KEY: "current", "voltage": 12})

    run_simulation(conn)
    assert get_mote(conn, ohm["r1"]).properties["current"] == 4
    assert get_mote(conn, ohm["r1"]).properties["voltage"] == 12
    assert get_mote(conn, ohm["r2"]).properties["voltage"] == 20


def test_two_instances_of_one_species_can_be_asked_opposite_questions(conn, ohm):
    """The headline: sizing and rating from the same equation, in the
    same garden, without editing what a Resistor *is*."""
    update_mote(conn, ohm["r2"], {FREEDOMS_KEY: "resistance", "voltage": 24})

    run_simulation(conn)
    assert get_mote(conn, ohm["r1"]).properties["voltage"] == 6       # sized
    assert get_mote(conn, ohm["r2"]).properties["resistance"] == pytest.approx(4.8)


def test_narrowing_can_leave_an_equation_with_nothing_left_to_move(conn, ohm):
    """Replacing rather than merging means a mote can be pinned on every
    property its equations mention. The equation is then a claim about
    values that are all already fixed -- and it fails loudly rather than
    quietly finding something else to change."""
    update_mote(conn, ohm["r1"], {FREEDOMS_KEY: "unrelated", "voltage": 99})

    with pytest.raises(SimulationError, match="contradictory|inconsistent"):
        run_simulation(conn)


def test_narrowing_to_a_different_property_asks_the_inverse_question(conn, ohm):
    update_mote(conn, ohm["r1"], {FREEDOMS_KEY: "resistance", "voltage": 99})

    run_simulation(conn)
    assert get_mote(conn, ohm["r1"]).properties["resistance"] == pytest.approx(49.5)


def test_several_species_merge_their_freedoms(conn):
    """Nothing orders two species a mote is an instance of, so neither
    can sensibly replace the other."""
    a = create_mote(conn, {"name": "A", FREEDOMS_KEY: "x"})
    b = create_mote(conn, {"name": "B", FREEDOMS_KEY: "y"})
    add_rule(conn, a, "X rule", "x == 1")
    add_rule(conn, b, "Y rule", "y == 2")
    mote = create_mote(conn, {"name": "M1"})
    set_species(conn, mote, a)
    set_species(conn, mote, b)

    run_simulation(conn)
    properties = get_mote(conn, mote).properties
    assert (properties["x"], properties["y"]) == (1, 2)


def test_a_species_own_freedoms_are_for_its_instances_not_itself(conn, ohm):
    """Otherwise every species mote in every garden would suddenly become
    solvable for the keys it declares, and show up as an unsolved
    freedom on every run."""
    result = run_simulation(conn)
    assert all(mote_id != ohm["species"] for mote_id, _key in result.unsolved)
    assert "voltage" not in get_mote(conn, ohm["species"]).properties


### a mote with no species at all

def test_a_mote_with_no_species_can_declare_its_own_freedoms(conn):
    """Before this phase, freedoms came only from a species, so a mote
    that was an instance of nothing could never be solved."""
    mote = create_mote(conn, {"name": "Solo", FREEDOMS_KEY: "x", "y": 4})
    add_rule(conn, mote, "One-off", "x == y * 2")

    run_simulation(conn)
    assert get_mote(conn, mote).properties["x"] == 8


def test_a_once_scoped_rule_can_now_write_to_a_species_less_mote(conn):
    """This is the Phase 3 limitation lifted: scope inference gave a rule
    asserted about a single mote, but that mote had no way to declare
    what the rule was allowed to determine."""
    part = create_mote(conn, {"name": "Part", "value": 6})
    plant = create_mote(conn, {
        "name": "Plant", FREEDOMS_KEY: "total", "roles": ["part"], "count": 3,
    })
    create_relationship(conn, plant, part, "part")
    add_rule(conn, plant, "Total", "total == part.value * count")

    run_simulation(conn)
    assert get_mote(conn, plant).properties["total"] == 18


### parsing

def test_freedoms_accept_the_same_shapes_they_always_did():
    assert parse_freedoms("voltage, current") == ["voltage", "current"]
    assert parse_freedoms(["voltage"]) == ["voltage"]
    assert parse_freedoms(None) == []
    assert parse_freedoms(7) == []


### nothing regresses

def test_a_garden_declaring_only_on_species_is_unchanged(conn, ohm):
    """The whole point of a default: an existing garden takes exactly the
    path it always took."""
    result = run_simulation(conn)
    assert result.unsolved == []
    assert {(c.key, c.new_value) for c in result.changes} == {("voltage", 6), ("voltage", 20)}

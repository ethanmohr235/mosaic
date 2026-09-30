"""Provenance: what a solve recorded, and how a value is explained.

The distinction under test throughout is between the two shapes a
derivation can take. A value the substitution loop determined has exactly
one equation behind it and expands into a chain; a value a solver backend
returned was found jointly with others and must say so rather than
pretending to a single cause.
"""
import pytest

from core.derivations import (
    DEFAULT_DEPTH,
    NO_SOLVE_NOTE,
    UNDETERMINED_NOTE,
    explain,
)
from core.motes import create_mote, get_mote, update_mote
from core.provenance import (
    DERIVED_READ,
    MODEL_READ,
    PROPERTY_READ,
    SOLVER,
    SUBSTITUTION,
)
from core.relationships import create_relationship
from core.rules import APPLIES_TO
from core.scenarios import OVERRIDES, VARIES
from core.simulation import SimulationError, run_simulation, run_time_series
from core.species import set_species


def add_rule(conn, species_id, name, equation):
    rule_id = create_mote(conn, {"name": name, "equation": equation})
    create_relationship(conn, rule_id, species_id, APPLIES_TO)
    return rule_id


@pytest.fixture
def chain(conn):
    """A three-link substitution chain: c depends on b depends on a.

    Deliberately all assignments, so every value is resolved by the
    substitution loop and no solver backend is involved at all.
    """
    species = create_mote(conn, {"name": "Link", "freedoms": "b,c"})
    add_rule(conn, species, "B rule", "b == a * 2")
    add_rule(conn, species, "C rule", "c == b + 1")
    mote = create_mote(conn, {"name": "L1", "a": 5})
    set_species(conn, mote, species)
    return mote


@pytest.fixture
def divider(conn):
    """The voltage divider from tests/test_simulation.py -- node voltages
    and branch currents are genuinely coupled and are found together."""
    node = create_mote(conn, {"name": "Node", "freedoms": "voltage"})
    resistor = create_mote(conn, {"name": "Resistor", "freedoms": "current"})
    source = create_mote(conn, {"name": "VoltageSource", "freedoms": "current"})
    ground = create_mote(conn, {"name": "Ground"})

    add_rule(conn, resistor, "Ohm's law",
             "targets('a').voltage - targets('b').voltage == current * resistance")
    add_rule(conn, node, "KCL",
             "sum(sources('b').current) - sum(sources('a').current) == 0")
    add_rule(conn, source, "Source",
             "targets('a').voltage - targets('b').voltage == voltage")
    add_rule(conn, ground, "Ground", "voltage == 0")

    nodes = {}
    for name in ("N0", "N1", "N2"):
        nodes[name] = create_mote(conn, {"name": name})
        set_species(conn, nodes[name], node)
    set_species(conn, nodes["N0"], ground)

    v1 = create_mote(conn, {"name": "V1", "voltage": 10})
    set_species(conn, v1, source)
    r1 = create_mote(conn, {"name": "R1", "resistance": 100})
    set_species(conn, r1, resistor)
    r2 = create_mote(conn, {"name": "R2", "resistance": 200})
    set_species(conn, r2, resistor)
    for element, a, b in ((v1, nodes["N1"], nodes["N0"]),
                          (r1, nodes["N1"], nodes["N2"]),
                          (r2, nodes["N2"], nodes["N0"])):
        create_relationship(conn, element, a, "a")
        create_relationship(conn, element, b, "b")
    return {"nodes": nodes, "v1": v1, "r1": r1, "r2": r2}


### Substitution chains

def test_a_substitution_chain_explains_link_by_link(conn, chain):
    run_simulation(conn)
    c = explain(conn, chain, "c")

    assert c.method == SUBSTITUTION
    assert c.value == 11
    assert [e.rule_name for e in c.equations] == ["C rule"]

    b = next(i for i in c.inputs if i.key == "b")
    assert b.kind == DERIVED_READ
    assert b.derivation is not None
    assert b.derivation.method == SUBSTITUTION
    assert [e.rule_name for e in b.derivation.equations] == ["B rule"]

    a = next(i for i in b.derivation.inputs if i.key == "a")
    assert a.kind == PROPERTY_READ
    assert a.value == 5
    # `a` is stored, not solved, so the chain stops here rather than
    # inventing a reason for it.
    assert a.derivation is None


def test_a_substitution_step_determines_exactly_one_value(conn, chain):
    run_simulation(conn)
    assert explain(conn, chain, "c").also_determined == ()


def test_a_value_the_run_did_not_derive_is_reported_not_guessed(conn, chain):
    run_simulation(conn)
    explanation = explain(conn, chain, "a")

    assert not explanation.derived
    assert explanation.equations == ()
    assert explanation.note is not None


def test_depth_limit_stops_the_recursion_and_says_so(conn, chain):
    run_simulation(conn)
    c = explain(conn, chain, "c", depth=1)

    b = next(i for i in c.inputs if i.key == "b")
    assert b.derivation is not None
    assert b.derivation.note == "beyond the requested depth"
    assert b.derivation.inputs == ()


### Solver blocks

def test_a_solver_block_names_every_value_it_found_together(conn, divider):
    run_simulation(conn)
    n2 = explain(conn, divider["nodes"]["N2"], "voltage")

    assert n2.method == SOLVER
    assert n2.value == pytest.approx(20 / 3)
    # The three branch currents were found in the same solve; a
    # tree-shaped explanation would silently drop them.
    assert set(n2.also_determined) == {
        (divider["v1"], "current"), (divider["r1"], "current"), (divider["r2"], "current")
    }


def test_a_solver_block_names_every_equation_that_constrained_it(conn, divider):
    run_simulation(conn)
    n2 = explain(conn, divider["nodes"]["N2"], "voltage")

    assert sorted(e.rule_name for e in n2.equations) == [
        "KCL", "KCL", "KCL", "Ohm's law", "Ohm's law"
    ]


def test_an_equation_the_substitution_loop_used_up_is_not_blamed(conn, divider):
    """`voltage == 0` determines the ground node by substitution. On the
    next pass it re-evaluates to the trivially true `0 == 0`, which stays
    in the open set but constrains nothing -- so it must not appear among
    the reasons for a value it took no part in."""
    run_simulation(conn)
    n2 = explain(conn, divider["nodes"]["N2"], "voltage")

    assert "Ground" not in {e.rule_name for e in n2.equations}
    assert explain(conn, divider["nodes"]["N0"], "voltage").method == SUBSTITUTION


def test_a_solver_block_still_expands_its_derived_inputs(conn, divider):
    run_simulation(conn)
    n2 = explain(conn, divider["nodes"]["N2"], "voltage")

    n1 = next(i for i in n2.inputs if i.mote_id == divider["nodes"]["N1"])
    assert n1.kind == DERIVED_READ
    assert n1.derivation is not None
    assert n1.derivation.method == SUBSTITUTION
    assert [e.rule_name for e in n1.derivation.equations] == ["Source"]


### Units

def test_recorded_values_are_in_base_units(conn):
    """A resistance kept in kohm enters the solver as ohms, and that is
    what the explanation must show -- otherwise the equation printed
    beside it would not multiply out."""
    species = create_mote(conn, {"name": "Resistor", "freedoms": "voltage"})
    add_rule(conn, species, "Ohm's law", "voltage == current * resistance")
    mote = create_mote(conn, {
        "name": "R1", "resistance": 4.7, "current": 0.001,
        "units": {"resistance": "kohm"},
    })
    set_species(conn, mote, species)
    run_simulation(conn)

    resistance = next(
        i for i in explain(conn, mote, "voltage").inputs if i.key == "resistance"
    )
    assert resistance.value == pytest.approx(4700)


def test_an_unrecognized_unit_does_not_break_recording(conn):
    """A bad unit name fails the run through _validate_units. Recording
    must not be the thing that raises first, or the real error would be
    replaced by a provenance crash."""
    species = create_mote(conn, {"name": "Thing", "freedoms": "b"})
    add_rule(conn, species, "Rule", "b == a * 2")
    mote = create_mote(conn, {"name": "T1", "a": 1, "units": {"a": "furlongs"}})
    set_species(conn, mote, species)

    with pytest.raises(SimulationError, match="unrecognized unit"):
        run_simulation(conn)


### What gets recorded at all

def _derivation_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM simulation_derivations").fetchone()[0]


def test_a_preview_records_nothing(conn, chain):
    run_time_series(conn, step_count=2)
    assert explain(conn, chain, "c").note == NO_SOLVE_NOTE


def test_a_preview_leaves_the_last_solve_explained(conn, chain):
    run_simulation(conn)
    update_mote(conn, chain, {"a": 10})
    run_time_series(conn, step_count=1)

    assert explain(conn, chain, "c").value == 11


def test_a_failed_run_records_no_derivations(conn):
    species = create_mote(conn, {"name": "Thing", "freedoms": "x,y"})
    add_rule(conn, species, "Only rule", "x + y == 1")
    mote = create_mote(conn, {"name": "T1"})
    set_species(conn, mote, species)

    with pytest.raises(SimulationError):
        run_simulation(conn)
    assert explain(conn, mote, "x").equations == ()


def test_a_failed_solve_keeps_the_last_good_solve_explained(conn, chain):
    """The motes still hold the last good solve's values, so its
    provenance is still the right explanation of them."""
    run_simulation(conn)
    breaker = create_mote(conn, {"name": "Broken", "freedoms": "x,y"})
    add_rule(conn, breaker, "Only rule", "x + y == 1")
    set_species(conn, create_mote(conn, {"name": "T1"}), breaker)
    before = _derivation_count(conn)

    with pytest.raises(SimulationError):
        run_simulation(conn)

    assert _derivation_count(conn) == before
    assert explain(conn, chain, "c").value == 11


def test_a_scenario_solve_keeps_the_last_real_solve_explained(conn, chain):
    run_simulation(conn)
    override = create_mote(conn, {"a": 100})
    create_relationship(conn, override, chain, OVERRIDES)
    scenario = create_mote(conn, {"name": "Big"})
    create_relationship(conn, scenario, override, VARIES)

    result = run_simulation(conn, scenario=scenario)

    assert any(c.key == "c" and c.new_value == 201 for c in result.changes)
    assert explain(conn, chain, "c").value == 11
    assert get_mote(conn, chain).properties["c"] == 11


def test_explaining_a_garden_with_no_solve_says_so(conn, chain):
    explanation = explain(conn, chain, "c")
    assert explanation.note == NO_SOLVE_NOTE
    assert explanation.value is None


def test_a_value_the_last_solve_did_not_determine_says_so(conn, chain):
    run_simulation(conn)
    explanation = explain(conn, chain, "a")
    assert not explanation.derived
    assert explanation.note == UNDETERMINED_NOTE


### Property models

def test_a_property_model_evaluation_is_recorded_as_an_input(conn):
    """A provider is opaque -- there is no property on any mote holding
    its answer -- so the operation name stands in for a property key."""
    gas = create_mote(conn, {"name": "Air"})
    model = create_mote(conn, {
        "name": "Ideal Air", "provider": "ideal_gas",
        "configuration": {"gas_constant": 287.0},
    })
    create_relationship(conn, gas, model, "uses_model")

    species = create_mote(conn, {"name": "Tank", "freedoms": "rho"})
    add_rule(conn, species, "Density",
             "rho == property('working_fluid', 'density', pressure, temperature)")
    tank = create_mote(conn, {"name": "T1", "pressure": 101325.0, "temperature": 300.0})
    set_species(conn, tank, species)
    create_relationship(conn, tank, gas, "working_fluid")

    run_simulation(conn)
    model_input = next(
        i for i in explain(conn, tank, "rho").inputs if i.kind == MODEL_READ
    )
    assert model_input.key == "density"
    assert model_input.value == pytest.approx(101325.0 / (287.0 * 300.0))


### Re-running

def test_a_second_solve_replaces_the_first(conn, chain):
    run_simulation(conn)
    first = _derivation_count(conn)
    update_mote(conn, chain, {"a": 10})
    run_simulation(conn)

    assert _derivation_count(conn) == first
    explanation = explain(conn, chain, "c")
    assert explanation.value == 21
    assert explanation.inputs[0].derivation.value == 20


def test_a_second_solve_drops_values_it_no_longer_determines(conn, chain):
    run_simulation(conn)
    other = create_mote(conn, {"name": "Unrelated", "freedoms": "y"})
    add_rule(conn, other, "Y rule", "y == 1")
    loner = create_mote(conn, {"name": "Y1"})
    set_species(conn, loner, other)
    run_simulation(conn)
    assert explain(conn, loner, "y").derived

    from core.motes import delete_mote
    delete_mote(conn, loner)
    run_simulation(conn)
    assert explain(conn, loner, "y").note == UNDETERMINED_NOTE


def test_default_depth_is_generous_enough_for_an_ordinary_chain(conn, chain):
    run_simulation(conn)
    assert DEFAULT_DEPTH > 2
    assert explain(conn, chain, "c").inputs[0].derivation is not None

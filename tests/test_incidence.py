"""The derived incidence index: which equations touch which values.

The thing under test that is easy to get wrong is that this is a
constraint system, not a dependency DAG. An equation has no inputs and
outputs; which of its values it can determine depends on which are free.
So `impact` walks a bipartite graph rather than following arrows, and
`producers` can be empty for a perfectly well-formed equation simply
because nothing declared that value free.
"""
import ast
import inspect

import pytest

from core import formula, incidence, simulation
from core.incidence import DERIVED, MISSING, STORED
from core.motes import create_mote
from core.relationships import create_relationship
from core.rules import APPLIES_TO
from core.simulation import incidence_index
from core.species import set_species


def add_rule(conn, species_id, name, equation):
    rule_id = create_mote(conn, {"name": name, "equation": equation})
    create_relationship(conn, rule_id, species_id, APPLIES_TO)
    return rule_id


@pytest.fixture
def chain(conn):
    """b depends on a; c depends on b. Two links, one instance."""
    species = create_mote(conn, {"name": "Link", "freedoms": "b,c"})
    add_rule(conn, species, "B rule", "b == a * 2")
    add_rule(conn, species, "C rule", "c == b + 1")
    mote = create_mote(conn, {"name": "L1", "a": 5})
    set_species(conn, mote, species)
    return mote


@pytest.fixture
def divider(conn):
    """The voltage divider, whose node voltages and branch currents are
    genuinely coupled -- no topological order over it exists."""
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


### What an equation touches

def test_an_equation_splits_its_values_into_free_and_read(conn, chain):
    index = incidence_index(conn)
    b_rule = next(t for t in index.touches if t.rule_name == "B rule")

    assert b_rule.determines == frozenset({(chain, "b")})
    assert b_rule.reads == frozenset({(chain, "a")})


def test_freedom_decides_which_side_a_value_lands_on(conn):
    """The same equation text splits differently in two gardens. That is
    the whole difference between a constraint system and a DAG."""
    species = create_mote(conn, {"name": "Ohm", "freedoms": "voltage"})
    add_rule(conn, species, "Ohm", "voltage == current * resistance")
    mote = create_mote(conn, {"name": "R1", "current": 2, "resistance": 3})
    set_species(conn, mote, species)
    forward = next(iter(incidence_index(conn).touches))
    assert forward.determines == frozenset({(mote, "voltage")})

    from core.motes import update_mote
    update_mote(conn, species, {"freedoms": "current"})
    update_mote(conn, mote, {"voltage": 6})
    backward = next(iter(incidence_index(conn).touches))
    assert backward.determines == frozenset({(mote, "current")})
    assert (mote, "voltage") in backward.reads


def test_the_index_needs_no_run(conn, chain):
    """Nothing has been solved; the structure is still fully known."""
    index = incidence_index(conn)
    assert index.producers((chain, "c"))


### impact

def test_impact_walks_forward_through_the_chain(conn, chain):
    effects = incidence_index(conn).impact((chain, "a"))
    reached = {(e.variable[1], e.distance) for e in effects}
    assert reached == {("b", 1), ("c", 2)}


def test_impact_of_a_value_nothing_reads_is_empty(conn, chain):
    assert incidence_index(conn).impact((chain, "unrelated")) == ()


def test_impact_reaches_every_value_in_a_coupled_block(conn, divider):
    """Changing one resistance moves node voltages the resistor never
    mentions, through equations it shares no variable with directly."""
    effects = incidence_index(conn).impact((divider["r1"], "resistance"))
    moved = {e.variable for e in effects}

    assert (divider["nodes"]["N2"], "voltage") in moved
    assert (divider["r2"], "current") in moved
    assert (divider["v1"], "current") in moved


def test_a_value_nothing_can_determine_is_never_an_effect(conn, divider):
    """V1.voltage is read by the source law but is not free, so no change
    anywhere can move it."""
    for effect in incidence_index(conn).impact((divider["r1"], "resistance")):
        assert effect.variable != (divider["v1"], "voltage")


def test_impact_respects_a_distance_limit(conn, chain):
    effects = incidence_index(conn).impact((chain, "a"), max_distance=1)
    assert {e.variable[1] for e in effects} == {"b"}


def test_impact_does_not_revisit_a_value(conn, divider):
    effects = incidence_index(conn).impact((divider["r1"], "resistance"))
    variables = [e.variable for e in effects]
    assert len(variables) == len(set(variables))


### producers and consumers

def test_every_equation_touching_a_free_value_could_determine_it(conn, chain):
    """`c == b + 1` can determine b just as readily as c -- which one it
    determines depends on what else is known when the solve runs, not on
    how the equation was written. So both rules produce b, and neither
    merely consumes it.

    This is where the constraint reading departs hardest from a
    dependency DAG, and reporting it any other way would be inventing a
    direction the system does not have."""
    index = incidence_index(conn)

    assert sorted(t.rule_name for t in index.producers((chain, "b"))) == ["B rule", "C rule"]
    assert index.consumers((chain, "b")) == ()


def test_a_value_that_is_not_free_is_only_ever_consumed(conn, chain):
    index = incidence_index(conn)

    assert index.producers((chain, "a")) == ()
    assert [t.rule_name for t in index.consumers((chain, "a"))] == ["B rule"]


def test_producers_is_empty_for_a_value_that_is_not_free(conn, chain):
    index = incidence_index(conn)
    assert index.producers((chain, "a")) == ()
    assert not index.is_free((chain, "a"))
    # ...but it is still read, which is what tells the two cases apart.
    assert index.consumers((chain, "a"))


def test_one_rule_over_many_instances_produces_one_touch_each(conn, divider):
    index = incidence_index(conn)
    kcl = [t for t in index.touches if t.rule_name == "KCL"]
    assert len(kcl) == 3
    assert {t.anchor_mote_id for t in kcl} == set(divider["nodes"].values())


### requires

def test_requires_lists_transitive_prerequisites(conn, chain):
    requirement = incidence_index(conn).requires((chain, "c"))

    assert requirement.determinable
    statuses = {need.variable[1]: need.status for need in requirement.needs}
    assert statuses == {"b": DERIVED, "a": STORED}


def test_requires_reports_a_property_no_mote_holds(conn):
    """The headline use. A missing property is invisible to an ordinary
    read -- core/formula.py raises before recording it -- so this is the
    case the permissive probe exists for."""
    species = create_mote(conn, {"name": "Link", "freedoms": "b"})
    add_rule(conn, species, "B rule", "b == a * 2")
    mote = create_mote(conn, {"name": "L1"})
    set_species(conn, mote, species)

    requirement = incidence_index(conn).requires((mote, "b"))
    assert [n.variable for n in requirement.missing] == [(mote, "a")]
    assert requirement.determinable


def test_requires_reports_a_value_that_is_not_free(conn, chain):
    requirement = incidence_index(conn).requires((chain, "a"))
    assert not requirement.free
    assert not requirement.determinable


def test_requires_reports_a_free_value_nothing_constrains(conn):
    species = create_mote(conn, {"name": "Lonely", "freedoms": "x"})
    mote = create_mote(conn, {"name": "L1"})
    set_species(conn, mote, species)

    requirement = incidence_index(conn).requires((mote, "x"))
    assert requirement.free
    assert not requirement.determinable
    assert requirement.producers == ()


def test_a_stale_value_on_a_free_property_is_still_derived(conn, chain):
    """A previous run leaves its answer on the mote. That number is about
    to be replaced, so calling it a stored input would describe the
    garden rather than the model."""
    from core.motes import update_mote
    update_mote(conn, chain, {"b": 999})

    statuses = {
        n.variable[1]: n.status for n in incidence_index(conn).requires((chain, "c")).needs
    }
    assert statuses["b"] == DERIVED


def test_requires_of_a_coupled_value_names_the_whole_block(conn, divider):
    requirement = incidence_index(conn).requires((divider["nodes"]["N2"], "voltage"))
    needed = {n.variable for n in requirement.needs}

    assert (divider["r1"], "current") in needed
    assert (divider["v1"], "voltage") in needed
    assert all(n.status != MISSING for n in requirement.needs)


### the harder formula shapes

def test_a_recurrence_depends_on_its_own_previous_value(conn):
    species = create_mote(conn, {"name": "Population", "freedoms": "size"})
    add_rule(conn, species, "Growth", "size == prev(size) * rate")
    mote = create_mote(conn, {"name": "P1", "size": 1, "rate": 2})
    set_species(conn, mote, species)

    index = incidence_index(conn)
    growth = next(iter(index.touches))
    assert (mote, "size") in growth.determines
    assert (mote, "rate") in growth.reads


def test_a_property_model_call_is_not_invented_as_a_variable(conn):
    """`property('fluid', 'density', ...)` names an operation, not a value
    any mote holds."""
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

    touch = next(iter(incidence_index(conn).touches))
    assert touch.determines == frozenset({(tank, "rho")})
    assert touch.reads == frozenset({(tank, "pressure"), (tank, "temperature")})
    assert not any(key == "density" for _mote, key in touch.reads)


def test_an_equation_nonlinear_in_its_unknowns_still_indexes(conn):
    species = create_mote(conn, {"name": "Square", "freedoms": "x"})
    add_rule(conn, species, "Square", "x * x == area")
    mote = create_mote(conn, {"name": "S1", "area": 4})
    set_species(conn, mote, species)

    touch = next(iter(incidence_index(conn).touches))
    assert touch.determines == frozenset({(mote, "x")})
    assert touch.reads == frozenset({(mote, "area")})


### drift

def test_the_index_holds_no_parser_of_its_own():
    """core/incidence.py is graph math only. The touch sets it works on
    come from core/simulation.py using the real evaluator, so there is no
    second interpretation of an equation here to fall out of step with
    the first. A parser appearing in this module is the drift this guards
    against."""
    source = inspect.getsource(incidence)
    tree = ast.parse(source)

    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert "ast" not in imported, "core/incidence.py must not parse expressions"

    called = {
        node.func.id for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert not called & {"evaluate_equation", "evaluate_expression", "parse_equation"}


def test_the_touch_sets_come_from_the_real_evaluator():
    """The other half of the same guarantee: what the index is built from
    must be produced by the same walk that solves, not a lookalike."""
    probe = ast.parse(inspect.getsource(simulation._probe))
    called = {
        node.func.id for node in ast.walk(probe)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "evaluate_equation" in called
    assert simulation.evaluate_equation is formula.evaluate_equation

    touch = ast.parse(inspect.getsource(simulation._touch))
    assert "_probe" in {
        node.func.id for node in ast.walk(touch)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }


# ---- blocks: the smallest group whose counts have to balance ----
#
# Pure graph math over plain EquationTouch values, the way the rest of this
# module is tested -- no database, because there is no parser here either.

def touch(name: str, determines=(), reads=()) -> incidence.EquationTouch:
    return incidence.EquationTouch(
        rule_mote_id=name, rule_name=name, anchor_mote_id="m", equation=name,
        determines=frozenset(determines), reads=frozenset(reads),
    )


def index_of(touches, freedoms) -> incidence.Index:
    return incidence.Index(touches=tuple(touches), freedoms=frozenset(freedoms))


def test_two_values_sharing_an_equation_are_one_block():
    ax, bx = ("m", "Ax"), ("m", "Bx")
    blocks = index_of([touch("sum", determines=[ax, bx])], [ax, bx]).blocks()
    assert len(blocks) == 1
    assert blocks[0].variables == frozenset({ax, bx})
    assert blocks[0].shortfall == 1


def test_values_in_unrelated_equations_are_separate_blocks():
    a, b = ("m", "a"), ("m", "b")
    blocks = index_of(
        [touch("one", determines=[a]), touch("two", determines=[b])], [a, b]
    ).blocks()
    assert {block.variables for block in blocks} == {frozenset({a}), frozenset({b})}
    assert all(block.shortfall == 0 for block in blocks)


def test_a_block_is_transitive():
    a, b, c = ("m", "a"), ("m", "b"), ("m", "c")
    blocks = index_of(
        [touch("ab", determines=[a, b]), touch("bc", determines=[b, c])], [a, b, c]
    ).blocks()
    assert len(blocks) == 1
    assert blocks[0].variables == frozenset({a, b, c})
    assert blocks[0].shortfall == 1


def test_a_free_value_no_equation_determines_is_a_block_of_one():
    """So "nothing constrains this" and "these three share two equations"
    are the same answer read at two sizes."""
    q = ("m", "q")
    blocks = index_of([], [q]).blocks()
    assert blocks == (incidence.Block(frozenset({q}), ()),)
    assert blocks[0].shortfall == 1


def test_an_equation_that_determines_nothing_is_in_no_block():
    """A constraint cannot make a deficient group solvable, and this is the
    structural form of saying so."""
    a = ("m", "a")
    blocks = index_of(
        [touch("limit", reads=[a]), touch("real", determines=[a])], [a]
    ).blocks()
    assert len(blocks) == 1
    assert [t.equation for t in blocks[0].equations] == ["real"]
    assert blocks[0].shortfall == 0


def test_a_surplus_block_has_a_negative_shortfall():
    a = ("m", "a")
    blocks = index_of(
        [touch("one", determines=[a]), touch("two", determines=[a])], [a]
    ).blocks()
    assert blocks[0].shortfall == -1


def test_blocks_come_back_largest_first():
    a, b, c = ("m", "a"), ("m", "b"), ("m", "c")
    blocks = index_of(
        [touch("bc", determines=[b, c]), touch("a", determines=[a])], [a, b, c]
    ).blocks()
    assert [len(block.variables) for block in blocks] == [2, 1]


def test_a_garden_with_no_freedoms_has_no_blocks():
    assert index_of([touch("x", reads=[("m", "a")])], []).blocks() == ()

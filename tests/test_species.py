from core.motes import create_mote, get_mote
from core.species import (
    create_instance,
    instances_of,
    list_species,
    set_species,
    set_template,
    species_of,
    template_for,
)


def _count_queries(conn, fn):
    """Runs fn() with SQL tracing on, returns (result, query_count)."""
    count = 0

    def trace(_sql):
        nonlocal count
        count += 1

    conn.set_trace_callback(trace)
    try:
        result = fn()
    finally:
        conn.set_trace_callback(None)
    return result, count


def test_set_species_creates_instance_of_relationship(conn):
    resistor = create_mote(conn, {"name": "Resistor"})
    r1 = create_mote(conn, {"resistance_ohms": 100})
    set_species(conn, r1, resistor)

    assert [m.id for m in species_of(conn, r1)] == [resistor]
    assert [m.id for m in instances_of(conn, resistor)] == [r1]


def test_mote_with_no_species_returns_empty(conn):
    lonely = create_mote(conn, {"name": "Lonely"})
    assert species_of(conn, lonely) == []


def test_species_with_no_instances_returns_empty(conn):
    species = create_mote(conn, {"name": "Empty"})
    assert instances_of(conn, species) == []


def test_mote_can_belong_to_multiple_species(conn):
    person = create_mote(conn, {"name": "Person"})
    employee = create_mote(conn, {"name": "Employee"})
    alice = create_mote(conn, {"name": "Alice"})
    set_species(conn, alice, person)
    set_species(conn, alice, employee)

    assert {m.id for m in species_of(conn, alice)} == {person, employee}


def test_list_species_returns_every_mote_with_instances(conn):
    resistor = create_mote(conn, {"name": "Resistor"})
    node = create_mote(conn, {"name": "Node"})
    r1 = create_mote(conn, {"resistance_ohms": 100})
    create_mote(conn, {"name": "Unrelated"})
    set_species(conn, r1, resistor)

    assert {m.id for m in list_species(conn)} == {resistor}
    assert node not in {m.id for m in list_species(conn)}


def test_list_species_empty_when_no_instance_of_relationships(conn):
    create_mote(conn, {"name": "Alice"})
    assert list_species(conn) == []


def test_species_of_and_instances_of_issue_a_constant_number_of_queries(conn):
    """Regression guard: species_of()/instances_of() used to issue one
    properties query per related mote (N+1)."""
    resistor = create_mote(conn, {"name": "Resistor"})
    multi_species_mote = create_mote(conn, {"name": "belongs to many"})
    for i in range(50):
        instance = create_mote(conn, {"name": f"r{i}"})
        set_species(conn, instance, resistor)
        species = create_mote(conn, {"name": f"s{i}"})
        set_species(conn, multi_species_mote, species)

    _, species_of_count = _count_queries(conn, lambda: species_of(conn, multi_species_mote))
    assert species_of_count <= 3, f"species_of() should not scale with species count, issued {species_of_count} queries"

    _, instances_of_count = _count_queries(conn, lambda: instances_of(conn, resistor))
    assert instances_of_count <= 3, f"instances_of() should not scale with instance count, issued {instances_of_count} queries"


# ---- templating ----

def test_create_instance_copies_template_properties(conn):
    resistor = create_mote(conn, {"name": "Resistor", "unknowns": "current"})
    template = create_mote(conn, {"name": "R", "resistance": 100})
    set_template(conn, template, resistor)

    instance = create_instance(conn, resistor)
    assert get_mote(conn, instance).properties == {"name": "R", "resistance": 100}
    assert [m.id for m in species_of(conn, instance)] == [resistor]


def test_create_instance_applies_overrides_over_the_template(conn):
    resistor = create_mote(conn, {"name": "Resistor"})
    template = create_mote(conn, {"name": "R", "resistance": 100})
    set_template(conn, template, resistor)

    instance = create_instance(conn, resistor, {"name": "R7", "resistance": 220})
    assert get_mote(conn, instance).properties == {"name": "R7", "resistance": 220}


def test_create_instance_without_a_template_makes_a_bare_mote(conn):
    species = create_mote(conn, {"name": "Plain"})
    instance = create_instance(conn, species)
    assert get_mote(conn, instance).properties == {}
    assert [m.id for m in species_of(conn, instance)] == [species]


def test_editing_an_instance_does_not_affect_the_template(conn):
    from core.motes import update_mote

    species = create_mote(conn, {"name": "Resistor"})
    template = create_mote(conn, {"name": "R", "resistance": 100})
    set_template(conn, template, species)

    instance = create_instance(conn, species)
    update_mote(conn, instance, {"resistance": 999})
    assert get_mote(conn, template).properties["resistance"] == 100


def test_template_is_not_an_instance_of_its_own_species(conn):
    species = create_mote(conn, {"name": "Resistor"})
    template = create_mote(conn, {"name": "R", "resistance": 100})
    set_template(conn, template, species)

    # The whole reason template_of is a distinct relationship type: the
    # simulation engine iterates instances_of, so a template must never
    # show up there or its placeholder values would be solved against.
    assert instances_of(conn, species) == []
    assert species_of(conn, template) == []


def test_template_for_returns_none_without_one(conn):
    species = create_mote(conn, {"name": "Plain"})
    assert template_for(conn, species) is None


def test_species_with_only_a_template_still_lists_as_a_species(conn):
    species = create_mote(conn, {"name": "Resistor"})
    template = create_mote(conn, {"name": "R"})
    set_template(conn, template, species)

    assert [m.id for m in list_species(conn)] == [species]


def test_simulation_ignores_template_motes(conn):
    """End-to-end guard for the reason template_of exists at all."""
    from core.relationships import create_relationship
    from core.rules import APPLIES_TO
    from core.simulation import run_simulation

    species = create_mote(conn, {"name": "Box", "unknowns": "area"})
    rule = create_mote(conn, {"name": "Area", "equation": "area == width * height"})
    create_relationship(conn, rule, species, APPLIES_TO)

    # The template has no width/height -- if the engine treated it as an
    # instance, evaluating the rule against it would fail the whole run.
    template = create_mote(conn, {"name": "box template"})
    set_template(conn, template, species)

    instance = create_instance(conn, species, {"width": 3, "height": 4})
    result = run_simulation(conn)

    assert get_mote(conn, instance).properties["area"] == 12.0
    assert "area" not in get_mote(conn, template).properties
    assert len(result.values) == 1

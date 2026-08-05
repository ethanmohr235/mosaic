from core.motes import create_mote
from core.species import instances_of, list_species, set_species, species_of


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

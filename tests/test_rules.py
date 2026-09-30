from core.motes import create_mote
from core.relationships import create_relationship
from core.rules import APPLIES_TO, list_rules, parse_freedoms


def make_rule(conn, species_id, **properties):
    rule_id = create_mote(conn, properties)
    create_relationship(conn, rule_id, species_id, APPLIES_TO)
    return rule_id


def test_parse_freedoms_accepts_comma_separated_string():
    assert parse_freedoms("voltage, current") == ["voltage", "current"]


def test_parse_freedoms_accepts_a_json_list():
    assert parse_freedoms(["voltage", "current"]) == ["voltage", "current"]


def test_parse_freedoms_handles_missing_or_junk_values():
    assert parse_freedoms(None) == []
    assert parse_freedoms("") == []
    assert parse_freedoms("  ,  ") == []
    assert parse_freedoms(42) == []


def test_list_rules_finds_rules_by_applies_to(conn):
    species = create_mote(conn, {"name": "Resistor"})
    make_rule(conn, species, name="Ohm's law", equation="voltage == current * resistance")

    rules = list_rules(conn)
    assert len(rules) == 1
    assert rules[0].name == "Ohm's law"
    assert rules[0].target_id == species
    assert rules[0].equation == "voltage == current * resistance"


def test_rule_without_an_equation_is_skipped(conn):
    species = create_mote(conn, {"name": "Resistor"})
    make_rule(conn, species, name="half-built")
    make_rule(conn, species, name="blank", equation="   ")
    assert list_rules(conn) == []


def test_motes_without_applies_to_are_not_rules(conn):
    create_mote(conn, {"name": "just a mote", "equation": "x == 1"})
    assert list_rules(conn) == []


def test_an_unnamed_rule_is_labelled_by_its_equation(conn):
    """Unnamed by default, with the equation as the label -- a name that
    copies its equation is a second account of it that drifts the moment
    the equation is edited."""
    species = create_mote(conn, {"name": "S"})
    make_rule(conn, species, equation="x == 1")
    assert list_rules(conn)[0].name == "x == 1"


def test_a_named_rule_keeps_its_name(conn):
    species = create_mote(conn, {"name": "S"})
    make_rule(conn, species, equation="x == 1", name="ΣFy")
    assert list_rules(conn)[0].name == "ΣFy"

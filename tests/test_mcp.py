import pytest

from core.db import get_connection
from core.rules import APPLIES_TO
from core.simulation import list_runs as core_list_runs
from core.species import INSTANCE_OF, TEMPLATE_OF
from mcp_server.server import (
    create_garden,
    create_instance,
    create_mote,
    create_relationship,
    delete_mote,
    delete_property,
    delete_relationship,
    export_garden,
    get_mote,
    get_run,
    instances_of,
    list_gardens,
    list_motes,
    list_relationships,
    list_runs,
    list_species,
    mote_relationships,
    neighborhood,
    preview_simulation,
    reserved_names,
    search_motes,
    set_species,
    set_template,
    species_of,
    template_for,
    update_mote,
    update_relationship,
    whole_garden,
)

# Every tool takes `garden` last and opens its own connection, so tests
# name a garden explicitly rather than sharing conftest's `conn` fixture.
GARDEN = "test"


@pytest.fixture(autouse=True)
def garden():
    create_garden(GARDEN)
    return GARDEN


# ---- motes ----


def test_mote_round_trip():
    created = create_mote({"name": "Alice"}, GARDEN)
    fetched = get_mote(created["id"], GARDEN)
    assert fetched == created

    updated = update_mote(created["id"], {"role": "engineer"}, GARDEN)
    assert updated["properties"] == {"name": "Alice", "role": "engineer"}

    assert delete_mote(created["id"], GARDEN) is True
    assert get_mote(created["id"], GARDEN) is None


def test_property_values_keep_their_json_types():
    # ui/api.py coerces incoming strings through json.loads because GUI
    # text fields deliver everything as text. MCP arguments arrive already
    # typed, so no coercion happens here -- "21" must stay a string.
    mote = create_mote(
        {"age": 21, "zip": "02138", "active": True, "tags": ["a", "b"], "note": None},
        GARDEN,
    )
    props = get_mote(mote["id"], GARDEN)["properties"]
    assert props["age"] == 21 and isinstance(props["age"], int)
    assert props["zip"] == "02138" and isinstance(props["zip"], str)
    assert props["active"] is True
    assert props["tags"] == ["a", "b"]
    assert props["note"] is None


def test_delete_property_removes_one_key():
    mote = create_mote({"name": "Alice", "role": "engineer"}, GARDEN)
    assert delete_property(mote["id"], "role", GARDEN) is True
    assert get_mote(mote["id"], GARDEN)["properties"] == {"name": "Alice"}


def test_missing_ids_return_empty_rather_than_raising():
    assert get_mote("does-not-exist", GARDEN) is None
    assert delete_mote("does-not-exist", GARDEN) is False
    assert delete_property("does-not-exist", "name", GARDEN) is False
    assert delete_relationship("does-not-exist", GARDEN) is False
    assert get_run("does-not-exist", GARDEN) is None
    assert update_relationship("does-not-exist", to_id="x", garden=GARDEN) is None


def test_search_motes_is_scoped_to_its_garden():
    create_garden("other")
    create_mote({"name": "Turbine A"}, GARDEN)
    create_mote({"name": "Turbine B"}, "other")

    assert [m["properties"]["name"] for m in search_motes("turbine", garden=GARDEN)] == ["Turbine A"]


def test_search_motes_key_value_and_limit():
    create_mote({"status": "open"}, GARDEN)
    create_mote({"status": "closed"}, GARDEN)
    create_mote({"status": "open"}, GARDEN)

    assert len(search_motes("status = open", garden=GARDEN)) == 2
    assert len(search_motes("status = open", limit=1, garden=GARDEN)) == 1


# ---- gardens ----


def test_writes_land_in_the_named_garden_only():
    create_garden("other")
    create_mote({"name": "Alice"}, GARDEN)

    assert [m["properties"]["name"] for m in list_motes(GARDEN)] == ["Alice"]
    assert list_motes("other") == []
    assert set(list_gardens()) >= {GARDEN, "other"}


def test_garden_defaults_to_default_when_omitted():
    create_mote({"name": "Alice"})
    assert [m["properties"]["name"] for m in list_motes()] == ["Alice"]
    assert list_motes(GARDEN) == []


def test_export_garden_returns_the_whole_graph():
    a = create_mote({"name": "A"}, GARDEN)
    b = create_mote({"name": "B"}, GARDEN)
    create_relationship(a["id"], b["id"], "next", GARDEN)

    data = export_garden(GARDEN)
    assert len(data["motes"]) == 2
    assert len(data["relationships"]) == 1


# ---- relationships ----


def test_relationship_round_trip():
    a = create_mote({"name": "A"}, GARDEN)
    b = create_mote({"name": "B"}, GARDEN)
    c = create_mote({"name": "C"}, GARDEN)

    rel = create_relationship(a["id"], b["id"], "next", GARDEN)
    assert rel["from_id"] == a["id"] and rel["type"] == "next"

    moved = update_relationship(rel["id"], to_id=c["id"], garden=GARDEN)
    assert moved["to_id"] == c["id"]

    assert delete_relationship(rel["id"], GARDEN) is True
    assert list_relationships(garden=GARDEN) == []


def test_list_relationships_filters():
    a = create_mote({"name": "A"}, GARDEN)
    b = create_mote({"name": "B"}, GARDEN)
    create_relationship(a["id"], b["id"], "next", GARDEN)
    create_relationship(b["id"], a["id"], "back", GARDEN)

    assert len(list_relationships(from_id=a["id"], garden=GARDEN)) == 1
    assert len(list_relationships(relationship_type="back", garden=GARDEN)) == 1
    assert len(list_relationships(garden=GARDEN)) == 2


def test_mote_relationships_includes_incoming_edges():
    a = create_mote({"name": "A"}, GARDEN)
    b = create_mote({"name": "B"}, GARDEN)
    create_relationship(a["id"], b["id"], "next", GARDEN)

    # list_relationships is outgoing-only; this is the difference.
    assert list_relationships(from_id=b["id"], garden=GARDEN) == []
    assert len(mote_relationships(b["id"], GARDEN)) == 1


def test_deleting_a_mote_takes_its_relationships_with_it():
    a = create_mote({"name": "A"}, GARDEN)
    b = create_mote({"name": "B"}, GARDEN)
    create_relationship(a["id"], b["id"], "next", GARDEN)

    delete_mote(a["id"], GARDEN)
    assert list_relationships(garden=GARDEN) == []


# ---- species ----


def test_species_and_instances():
    species = create_mote({"name": "Person"}, GARDEN)
    alice = create_mote({"name": "Alice"}, GARDEN)

    rel = set_species(alice["id"], species["id"], GARDEN)
    assert rel["type"] == INSTANCE_OF

    assert [m["id"] for m in species_of(alice["id"], GARDEN)] == [species["id"]]
    assert [m["id"] for m in instances_of(species["id"], GARDEN)] == [alice["id"]]
    assert [m["id"] for m in list_species(GARDEN)] == [species["id"]]


def test_create_instance_seeds_from_the_template():
    species = create_mote({"name": "Resistor"}, GARDEN)
    template = create_mote({"name": "R", "resistance": 100}, GARDEN)
    rel = set_template(template["id"], species["id"], GARDEN)
    assert rel["type"] == TEMPLATE_OF
    assert template_for(species["id"], GARDEN)["id"] == template["id"]

    instance = create_instance(species["id"], {"name": "R1"}, GARDEN)
    assert instance["properties"] == {"name": "R1", "resistance": 100}

    # A template holds placeholder values, so it is not an instance.
    assert [m["id"] for m in instances_of(species["id"], GARDEN)] == [instance["id"]]


# ---- graph ----


def test_neighborhood_respects_the_hop_limit():
    a = create_mote({"name": "A"}, GARDEN)
    b = create_mote({"name": "B"}, GARDEN)
    c = create_mote({"name": "C"}, GARDEN)
    create_relationship(a["id"], b["id"], "next", GARDEN)
    create_relationship(b["id"], c["id"], "next", GARDEN)

    assert len(neighborhood(a["id"], 1, GARDEN)["motes"]) == 2
    assert len(neighborhood(a["id"], 2, GARDEN)["motes"]) == 3


def test_whole_garden_reaches_unconnected_motes():
    a = create_mote({"name": "A"}, GARDEN)
    b = create_mote({"name": "B"}, GARDEN)
    create_relationship(a["id"], b["id"], "next", GARDEN)
    create_mote({"name": "Island"}, GARDEN)

    assert len(neighborhood(a["id"], 5, GARDEN)["motes"]) == 2
    assert len(whole_garden(GARDEN)["motes"]) == 3


# ---- simulation ----


@pytest.fixture
def box():
    """A species whose instances solve area == width * height."""
    species = create_mote({"name": "Box", "unknowns": "area"}, GARDEN)
    rule = create_mote({"name": "Area", "equation": "area == width * height"}, GARDEN)
    create_relationship(rule["id"], species["id"], APPLIES_TO, GARDEN)

    instance = create_mote({"name": "B1", "width": 3, "height": 4}, GARDEN)
    set_species(instance["id"], species["id"], GARDEN)
    return instance


def test_preview_simulation_reports_changes(box):
    result = preview_simulation(1, GARDEN)
    assert result["error"] is None
    assert len(result["steps"]) == 1

    changes = result["steps"][0]["changes"]
    assert [(c["key"], c["new_value"]) for c in changes] == [("area", 12.0)]


def test_preview_simulation_writes_nothing(box):
    preview_simulation(3, GARDEN)

    # Neither the graph nor the audit log moves: a preview is in-memory only.
    assert "area" not in get_mote(box["id"], GARDEN)["properties"]
    assert list_runs(garden=GARDEN) == []
    conn = get_connection(GARDEN)
    try:
        assert core_list_runs(conn) == []
    finally:
        conn.close()


def test_preview_simulation_rejects_a_zero_step_count(box):
    with pytest.raises(ValueError):
        preview_simulation(0, GARDEN)


# ---- conventions ----


def test_reserved_names_lists_the_interpreted_keys():
    names = reserved_names()
    assert "equation" in names["properties"]
    assert INSTANCE_OF in names["relationshipTypes"]

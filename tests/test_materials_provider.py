import pytest

from core import materials_provider as materials
from core.motes import create_mote, get_mote
from core.providers import CapabilityNotSupported, InvalidModelConfiguration, describe_providers
from core.relationships import create_relationship
from core.rules import APPLIES_TO
from core.simulation import check_constraints, run_simulation, PASS, FAIL


def test_every_bundled_material_has_the_four_properties_in_plausible_si():
    for entry in materials.list_materials():
        assert 1e9 < entry["youngs_modulus"] < 1e12, entry["name"]
        assert 0 < entry["poisson_ratio"] < 0.5, entry["name"]
        assert 1e6 < entry["yield_strength"] < 5e9, entry["name"]
        assert 1000 < entry["density"] < 25000, entry["name"]


def test_a_material_is_found_by_name_or_alias_ignoring_case_and_punctuation():
    assert materials.find_material("6061-T6")[0] == "6061-T6"
    assert materials.find_material("al 6061 t6")[0] == "6061-T6"
    assert materials.find_material("ss304")[0] == "304-annealed"
    assert materials.find_material("unobtainium") is None


def test_the_provider_answers_each_operation():
    p = materials.MaterialsProvider({"material": "A36"})
    assert p.evaluate("youngs_modulus", {}) == 200e9
    assert p.evaluate("yield_strength", {}) == 250e6
    assert p.evaluate("shear_modulus", {}) == pytest.approx(200e9 / (2 * 1.26))
    with pytest.raises(CapabilityNotSupported):
        p.evaluate("enthalpy_pt", {})


def test_values_override_a_listed_material_or_define_a_custom_one():
    p = materials.MaterialsProvider({"material": "6061-T6", "values": {"yield_strength": 241e6}})
    assert p.evaluate("yield_strength", {}) == 241e6
    assert p.evaluate("density", {}) == 2700
    custom = materials.MaterialsProvider({"values": {"youngs_modulus": 3e9}})
    assert custom.evaluate("youngs_modulus", {}) == 3e9
    with pytest.raises(InvalidModelConfiguration, match="has no yield strength"):
        custom.evaluate("yield_strength", {})


@pytest.mark.parametrize("configuration, message", [
    ({}, "needs a 'material'"),
    ({"material": "6016-T6"}, "did you mean '6061-T6'"),
    ({"material": ""}, "must be a material name"),
    ({"values": {"hardness": 3}}, "has no property 'hardness'"),
    ({"values": {"density": "heavy"}}, "must be a number"),
    ({"values": [1]}, "must be a map"),
])
def test_a_bad_configuration_says_what_to_write(configuration, message):
    with pytest.raises(InvalidModelConfiguration, match=message):
        materials.MaterialsProvider(configuration)


def test_the_provider_is_registered_and_describes_itself():
    described = describe_providers()["materials"]
    assert "material" in described["configuration"]
    assert {c["operation"] for c in described["capabilities"]} >= {
        "youngs_modulus", "poisson_ratio", "yield_strength", "density", "shear_modulus"}


def _bar(conn, material="6061-T6", stress=200e6):
    """A bar whose allowable is read off its material, never copied."""
    model = create_mote(conn, {"name": "Al model", "provider": "materials",
                               "configuration": {"material": material}})
    alloy = create_mote(conn, {"name": "Alloy"})
    create_relationship(conn, alloy, model, "uses_model")
    bar = create_mote(conn, {"name": "bar", "stress": stress, "freedoms": "E, margin"})
    create_relationship(conn, bar, alloy, "material")
    for equation in ("E == property('material', 'youngs_modulus')",
                     "margin == property('material', 'yield_strength') / stress - 1"):
        rule = create_mote(conn, {"equation": equation})
        create_relationship(conn, rule, bar, APPLIES_TO)
    check = create_mote(conn, {"equation": "stress <= property('material', 'yield_strength')"})
    create_relationship(conn, check, bar, APPLIES_TO)
    return bar


def test_a_rule_reads_a_property_through_the_material(conn):
    bar = _bar(conn)
    run_simulation(conn)
    props = get_mote(conn, bar).properties
    assert props["E"] == pytest.approx(68.9e9)
    assert props["margin"] == pytest.approx(276 / 200 - 1)
    assert [c.status for c in check_constraints(conn)] == [PASS]


def test_changing_the_material_changes_every_answer(conn):
    bar = _bar(conn, material="6061-T6", stress=300e6)
    run_simulation(conn)
    assert [c.status for c in check_constraints(conn)] == [FAIL]
    from core.motes import list_motes, update_mote
    model_id = next(m.id for m in list_motes(conn) if m.properties.get("provider") == "materials")
    update_mote(conn, model_id, {"configuration": {"material": "7075-T6"}})
    run_simulation(conn)
    assert get_mote(conn, bar).properties["E"] == pytest.approx(71.7e9)
    assert [c.status for c in check_constraints(conn)] == [PASS]

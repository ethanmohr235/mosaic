"""The role table -- what a mote is, derived from what points at it.

Dict-and-dataclass based, no DB: every role's detection is a pure
function over motes and relationships, which is what lets the same
answer serve a whole-garden query and an already-fetched subgraph.
"""
from core.entities import Mote, Relationship
from core.roles import (
    FOLDER,
    PORTAL,
    PROPERTY_MODEL,
    ROLE_LABELS,
    ROLE_NAMES,
    ROLES,
    RULE,
    SPECIES,
    TEMPLATE,
    ids_with_role,
    roles_in,
    roles_of,
)


def mote(id, **properties):
    return Mote(id=id, properties=properties)


def rel(from_id, to_id, type):
    return Relationship(from_id=from_id, to_id=to_id, type=type)


# ---- each role ----

def test_a_species_is_the_target_of_instance_of():
    motes = [mote("resistor"), mote("r1")]
    rels = [rel("r1", "resistor", "instance_of")]
    assert roles_in(motes, rels) == {"resistor": [SPECIES]}


def test_a_species_with_only_a_template_still_counts():
    # Matches species_ids_in: otherwise a species you set up but haven't
    # instantiated wouldn't render as one.
    motes = [mote("resistor"), mote("r-template")]
    rels = [rel("r-template", "resistor", "template_of")]
    roles = roles_in(motes, rels)
    assert roles["resistor"] == [SPECIES]
    assert roles["r-template"] == [TEMPLATE]


def test_a_folder_is_the_target_of_in_folder():
    motes = [mote("thermo"), mote("entropy")]
    rels = [rel("entropy", "thermo", "in_folder")]
    assert roles_in(motes, rels) == {"thermo": [FOLDER]}


def test_a_property_model_is_the_target_of_uses_model():
    motes = [mote("ideal-air"), mote("air")]
    rels = [rel("air", "ideal-air", "uses_model")]
    assert roles_in(motes, rels) == {"ideal-air": [PROPERTY_MODEL]}


def test_a_rule_needs_both_applies_to_and_an_equation():
    motes = [mote("rule", equation="x == 1"), mote("species")]
    rels = [rel("rule", "species", "applies_to")]
    assert roles_of(motes[0], rels) == [RULE]


def test_a_half_built_rule_is_not_a_rule():
    """applies_to but no equation -- core/rules.py::list_rules() skips it,
    so nothing else should call it a rule either."""
    motes = [mote("rule"), mote("species")]
    rels = [rel("rule", "species", "applies_to")]
    assert roles_of(motes[0], rels) == []


def test_an_equation_alone_is_not_a_rule():
    lonely = mote("draft", equation="x == 1")
    assert roles_of(lonely, []) == []


def test_a_portal_is_recognized_from_its_properties_alone():
    portal = mote("p", portal_garden="other", portal_mote="abc")
    assert roles_of(portal, []) == [PORTAL]


def test_an_incomplete_portal_is_not_a_portal():
    assert roles_of(mote("p", portal_garden="other"), []) == []
    assert roles_of(mote("p", portal_garden="  ", portal_mote="abc"), []) == []


# ---- combinations ----

def test_a_mote_can_play_several_roles_at_once():
    """Roles aren't exclusive: organizing a species into a folder doesn't
    stop it being a species."""
    motes = [mote("resistor"), mote("r1"), mote("components")]
    rels = [
        rel("r1", "resistor", "instance_of"),        # resistor is a species
        rel("resistor", "components", "in_folder"),  # ...filed under components
        rel("datasheet", "resistor", "in_folder"),   # ...and itself a folder
    ]
    roles = roles_in(motes, rels)
    assert roles["resistor"] == [SPECIES, FOLDER]
    assert roles["components"] == [FOLDER]


def test_a_template_of_one_species_can_be_an_instance_of_another():
    motes = [mote("t"), mote("a"), mote("b")]
    rels = [rel("t", "a", "template_of"), rel("t", "b", "instance_of")]
    assert roles_of(motes[0], rels) == [TEMPLATE]


def test_roles_are_returned_in_declaration_order():
    both = mote("x", equation="y == 1")
    rels = [rel("y", "x", "instance_of"), rel("x", "z", "applies_to")]
    assert roles_of(both, rels) == [SPECIES, RULE]


def test_a_mote_with_no_role_is_left_out_entirely():
    """Absence says "ordinary mote" without every payload carrying an
    empty list for most of the graph."""
    assert roles_in([mote("plain", name="Alice")], []) == {}


# ---- helpers ----

def test_ids_with_role_filters_and_preserves_order():
    motes = [mote("a"), mote("s1"), mote("b")]
    rels = [rel("x", "a", "instance_of"), rel("y", "b", "instance_of")]
    assert ids_with_role(SPECIES, motes, rels) == ["a", "b"]


def test_every_role_has_a_label_and_at_least_one_test():
    assert set(ROLE_NAMES) == set(ROLE_LABELS)
    assert all(role.ids_in or role.matches for role in ROLES), (
        "a role nothing can detect is not a role"
    )

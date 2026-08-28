"""The kind table -- what a mote is, derived from what points at it.

Dict-and-dataclass based, no DB: every kind's detection is a pure
function over motes and relationships, which is what lets the same
answer serve a whole-garden query and an already-fetched subgraph.

Not to be confused with core/roles.py, which answers what an assembly's
parts are *called*. A kind is what a mote is; a role is what something
else calls it.
"""
from core.entities import Mote, Relationship
from core.kinds import (
    PROPERTY_MODEL,
    KIND_LABELS,
    KIND_NAMES,
    KINDS,
    RULE,
    SPECIES,
    TEMPLATE,
    ids_with_kind,
    kinds_in,
    kinds_of,
)


def mote(id, **properties):
    return Mote(id=id, properties=properties)


def rel(from_id, to_id, type):
    return Relationship(from_id=from_id, to_id=to_id, type=type)


# ---- each kind ----

def test_a_species_is_the_target_of_instance_of():
    motes = [mote("resistor"), mote("r1")]
    rels = [rel("r1", "resistor", "instance_of")]
    assert kinds_in(motes, rels) == {"resistor": [SPECIES]}


def test_a_species_with_only_a_template_still_counts():
    # Matches species_ids_in: otherwise a species you set up but haven't
    # instantiated wouldn't render as one.
    motes = [mote("resistor"), mote("r-template")]
    rels = [rel("r-template", "resistor", "template_of")]
    kinds = kinds_in(motes, rels)
    assert kinds["resistor"] == [SPECIES]
    assert kinds["r-template"] == [TEMPLATE]


def test_a_property_model_is_the_target_of_uses_model():
    motes = [mote("ideal-air"), mote("air")]
    rels = [rel("air", "ideal-air", "uses_model")]
    assert kinds_in(motes, rels) == {"ideal-air": [PROPERTY_MODEL]}


def test_a_rule_needs_both_applies_to_and_an_equation():
    motes = [mote("rule", equation="x == 1"), mote("species")]
    rels = [rel("rule", "species", "applies_to")]
    assert kinds_of(motes[0], rels) == [RULE]


def test_a_half_built_rule_is_not_a_rule():
    """applies_to but no equation -- core/rules.py::list_rules() skips it,
    so nothing else should call it a rule either."""
    motes = [mote("rule"), mote("species")]
    rels = [rel("rule", "species", "applies_to")]
    assert kinds_of(motes[0], rels) == []


def test_an_equation_alone_is_not_a_rule():
    lonely = mote("draft", equation="x == 1")
    assert kinds_of(lonely, []) == []


# ---- combinations ----

def test_a_mote_can_be_several_kinds_at_once():
    """Kinds aren't exclusive: a species being the target of a property
    model's uses_model doesn't stop it being a species."""
    motes = [mote("resistor"), mote("r1")]
    rels = [
        rel("r1", "resistor", "instance_of"),  # resistor is a species
        rel("r1", "resistor", "uses_model"),   # ...and also a property model
    ]
    kinds = kinds_in(motes, rels)
    assert kinds["resistor"] == [SPECIES, PROPERTY_MODEL]


def test_a_template_of_one_species_can_be_an_instance_of_another():
    motes = [mote("t"), mote("a"), mote("b")]
    rels = [rel("t", "a", "template_of"), rel("t", "b", "instance_of")]
    assert kinds_of(motes[0], rels) == [TEMPLATE]


def test_kinds_are_returned_in_declaration_order():
    both = mote("x", equation="y == 1")
    rels = [rel("y", "x", "instance_of"), rel("x", "z", "applies_to")]
    assert kinds_of(both, rels) == [SPECIES, RULE]


def test_a_mote_with_no_role_is_left_out_entirely():
    """Absence says "ordinary mote" without every payload carrying an
    empty list for most of the graph."""
    assert kinds_in([mote("plain", name="Alice")], []) == {}


# ---- helpers ----

def test_ids_with_kind_filters_and_preserves_order():
    motes = [mote("a"), mote("s1"), mote("b")]
    rels = [rel("x", "a", "instance_of"), rel("y", "b", "instance_of")]
    assert ids_with_kind(SPECIES, motes, rels) == ["a", "b"]


def test_every_kind_has_a_label_and_at_least_one_test():
    assert set(KIND_NAMES) == set(KIND_LABELS)
    assert all(kind.ids_in or kind.matches for kind in KINDS), (
        "a role nothing can detect is not a role"
    )

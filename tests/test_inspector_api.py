"""The bridge contract the shared floating inspector speaks.

Tested once, here, rather than five times over: api_common.InspectorApi is
mixed into every window's Api, so the same methods with the same shapes
are what Monarch, Motion, Monolith, Mora and Monastery all expose. The
last test in this file is the one that holds that claim up.
"""
import pytest

import monarch.api
import monastery.api
import monolith.api
import mora.api
import motion.api
from api_common import InspectorApi
from core.gardens import create_garden


@pytest.fixture
def api():
    create_garden("test")
    instance = monarch.api.Api("test")
    yield instance
    instance.shutdown()


@pytest.fixture
def garden(api):
    """A species with a template and three instances -- enough for the
    template hints and the inferred enums to have something to say."""
    species = api.create_mote({"name": "Task"})["id"]
    template = api.create_mote({"name": "Task template", "status": "todo",
                                "priority": "medium", "owner": ""})["id"]
    api.set_template(template, species)

    made = []
    for name, status, priority in [
        ("Recalibrate sensor", "in progress", "high"),
        ("Draft checklist", "todo", "medium"),
        ("Replace gasket", "blocked", "high"),
    ]:
        mote = api.create_mote({"name": name, "status": status, "priority": priority})["id"]
        api.set_species(mote, species)
        made.append(mote)
    return {"species": species, "template": template, "tasks": made}


# ---- reading ----

def test_payload_carries_label_keys_and_species(api, garden):
    payload = api.inspect_mote(garden["tasks"][0])
    assert payload["label"] == "Recalibrate sensor"
    assert payload["keys"] == ["priority", "status"]     # `name` is the title field
    assert payload["species"]["label"] == "Task"
    assert payload["species"]["isTemplate"] is False


def test_missing_mote_reads_as_none(api):
    assert api.inspect_mote("no-such-mote") is None


def test_name_is_not_repeated_in_the_property_list(api):
    mote = api.create_mote({"name": "Alone", "colour": "teal"})["id"]
    assert api.inspect_mote(mote)["keys"] == ["colour"]


def test_label_falls_back_to_id_without_a_name(api):
    mote = api.create_mote({"colour": "teal"})["id"]
    assert api.inspect_mote(mote)["label"] == mote


def test_template_supplies_the_hints_this_mote_is_missing(api, garden):
    payload = api.inspect_mote(garden["tasks"][0])
    # The template has status, priority and owner; this mote lacks owner.
    assert payload["hints"] == [{"key": "owner", "value": ""}]


def test_a_hint_carries_the_templates_actual_value(api, garden):
    """Picking a hint should reproduce what create_instance() would have
    seeded at creation, not leave the property empty."""
    species = api.create_mote({"name": "Widget"})["id"]
    template = api.create_mote({"name": "Widget template", "status": "todo"})["id"]
    api.set_template(template, species)
    mote = api.create_mote({"name": "Gizmo"})["id"]
    api.set_species(mote, species)

    payload = api.inspect_mote(mote)
    assert payload["hints"] == [{"key": "status", "value": "todo"}]

    hint = payload["hints"][0]
    payload = api.inspector_set_property(mote, hint["key"], hint["value"])
    assert payload["properties"]["status"] == "todo"


def test_a_species_without_a_template_offers_no_hints(api):
    species = api.create_mote({"name": "Loose"})["id"]
    mote = api.create_mote({"name": "One"})["id"]
    api.set_species(mote, species)
    assert api.inspect_mote(mote)["hints"] == []


def test_a_repeated_value_across_siblings_becomes_an_enum(api, garden):
    kinds = api.inspect_mote(garden["tasks"][0])["kinds"]
    assert kinds["priority"]["kind"] == "enum"
    assert kinds["priority"]["options"] == ["high", "medium"]


def test_the_template_counts_towards_the_vocabulary(api, garden):
    """`status` is a different string on all three instances, so it is an
    enum only because the template's own value repeats one of them."""
    kinds = api.inspect_mote(garden["tasks"][0])["kinds"]
    assert kinds["status"]["kind"] == "enum"
    assert kinds["status"]["options"] == ["blocked", "in progress", "todo"]


def test_a_value_every_sibling_spells_differently_stays_text(api, garden):
    for index, mote in enumerate(garden["tasks"]):
        api.inspector_set_property(mote, "note", f"note {index}")
    kinds = api.inspect_mote(garden["tasks"][0])["kinds"]
    assert kinds["note"]["kind"] == "text"


def test_editors_are_chosen_by_value_shape(api):
    other = api.create_mote({"name": "Amara Voss"})["id"]
    mote = api.create_mote({
        "name": "Shapes", "due": "2026-09-22", "hours": 6,
        "done": True, "owner": other, "note": "free text",
    })["id"]
    kinds = api.inspect_mote(mote)["kinds"]
    assert kinds["due"]["kind"] == "date"
    assert kinds["hours"]["kind"] == "number"
    assert kinds["done"]["kind"] == "boolean"
    assert kinds["note"]["kind"] == "text"
    assert kinds["owner"]["kind"] == "ref"
    assert kinds["owner"]["refLabel"] == "Amara Voss"


def test_a_string_that_is_not_a_mote_id_is_not_a_ref(api):
    mote = api.create_mote({"name": "Shapes", "owner": "somebody-else"})["id"]
    assert api.inspect_mote(mote)["kinds"]["owner"]["kind"] == "text"


def test_reserved_keys_sort_last_and_are_flagged(api):
    mote = api.create_mote({"name": "Rule", "equation": "a == b", "colour": "teal"})["id"]
    payload = api.inspect_mote(mote)
    assert payload["keys"] == ["colour", "equation"]
    assert payload["reserved"] == ["equation"]


def test_relationships_come_back_both_ways_with_labels(api, garden):
    person = api.create_mote({"name": "Amara Voss"})["id"]
    task = garden["tasks"][0]
    api.create_relationship(task, person, "assigned_to")
    api.create_relationship(garden["species"], task, "contains")

    rows = api.inspect_mote(task)["relationships"]
    assert [(r["direction"], r["type"], r["otherLabel"]) for r in rows] == [
        ("out", "assigned_to", "Amara Voss"),
        ("out", "instance_of", "Task"),
        ("in", "contains", "Task"),
    ]


def test_species_membership_is_listed_like_any_other_relationship(api, garden):
    """A species *is* a relationship, and hiding it made the one
    structural fact the whole graph is built on the only one you could
    not see in the list of what a mote is wired to."""
    rows = api.inspect_mote(garden["tasks"][0])["relationships"]
    species = [r for r in rows if r["type"] == "instance_of"]
    assert [(r["direction"], r["otherLabel"]) for r in species] == [("out", "Task")]


def test_relationship_types_offered_exclude_the_reserved_ones(api, garden):
    """...except `instance_of`, which is how a mote is given a species
    now that the species dropdown is gone, so the picker has to offer it
    even in a garden that has none yet."""
    api.create_relationship(garden["tasks"][0], garden["tasks"][1], "blocks")
    assert api.inspect_mote(garden["tasks"][0])["relTypes"] == ["blocks", "instance_of"]
    assert "template_of" not in api.inspect_mote(garden["tasks"][0])["relTypes"]


def test_search_finds_candidates_by_name(api, garden):
    found = api.inspector_search("gask")
    assert [row["label"] for row in found] == ["Replace gasket"]
    assert found[0]["speciesLabel"] == "Task"


def test_search_without_a_query_offers_a_starting_set(api, garden):
    assert len(api.inspector_search("", limit=3)) == 3


# ---- writing ----

def test_set_property_coerces_and_returns_fresh_state(api, garden):
    payload = api.inspector_set_property(garden["tasks"][0], "estimate", "6")
    assert payload["properties"]["estimate"] == 6     # a number, not "6"


def test_set_property_leaves_unparseable_text_alone(api, garden):
    payload = api.inspector_set_property(garden["tasks"][0], "estimate", "six")
    assert payload["properties"]["estimate"] == "six"


def test_delete_property(api, garden):
    payload = api.inspector_delete_property(garden["tasks"][0], "priority")
    assert "priority" not in payload["properties"]


def test_rename_property_keeps_the_value(api, garden):
    payload = api.inspector_rename_property(garden["tasks"][0], "priority", "urgency")
    assert payload["properties"]["urgency"] == "high"
    assert "priority" not in payload["properties"]


def test_rename_to_blank_changes_nothing(api, garden):
    payload = api.inspector_rename_property(garden["tasks"][0], "priority", "   ")
    assert payload["properties"]["priority"] == "high"


def test_rename_a_key_the_mote_lacks_changes_nothing(api, garden):
    payload = api.inspector_rename_property(garden["tasks"][0], "nope", "something")
    assert "something" not in payload["properties"]


def test_rename_property_carries_its_unit_along(api, garden):
    task = garden["tasks"][0]
    api.inspector_set_property(task, "units", {"priority": "kohm"})
    payload = api.inspector_rename_property(task, "priority", "urgency")
    assert payload["properties"]["units"] == {"urgency": "kohm"}


def test_rename_property_without_a_unit_leaves_units_map_alone(api, garden):
    task = garden["tasks"][0]
    api.inspector_set_property(task, "units", {"estimate": "hr"})
    payload = api.inspector_rename_property(task, "priority", "urgency")
    assert payload["properties"]["units"] == {"estimate": "hr"}


def test_delete_property_drops_its_unit_entry(api, garden):
    task = garden["tasks"][0]
    api.inspector_set_property(task, "units", {"priority": "kohm", "estimate": "hr"})
    payload = api.inspector_delete_property(task, "priority")
    assert payload["properties"]["units"] == {"estimate": "hr"}
    assert "priority" not in payload["properties"]


# ---- undo ----

def test_set_property_can_be_undone(api, garden):
    task = garden["tasks"][0]
    api.inspector_set_property(task, "priority", "low")
    assert api.undo()["undone"] is not None
    assert api.inspect_mote(task)["properties"]["priority"] == "high"


def test_deleting_a_property_with_a_unit_is_undone_in_one_step(api, garden):
    task = garden["tasks"][0]
    api.inspector_set_property(task, "units", {"priority": "kohm", "estimate": "hr"})
    api.inspector_delete_property(task, "priority")
    assert api.undo()["undone"] is not None
    payload = api.inspect_mote(task)
    assert payload["properties"]["priority"] == "high"
    assert payload["properties"]["units"] == {"priority": "kohm", "estimate": "hr"}


def test_renaming_a_property_with_a_unit_is_undone_in_one_step(api, garden):
    task = garden["tasks"][0]
    api.inspector_set_property(task, "units", {"priority": "kohm"})
    api.inspector_rename_property(task, "priority", "urgency")
    assert api.undo()["undone"] is not None
    payload = api.inspect_mote(task)
    assert payload["properties"]["priority"] == "high"
    assert "urgency" not in payload["properties"]
    assert payload["properties"]["units"] == {"priority": "kohm"}


def test_setting_species_is_undone_in_one_step_even_when_it_replaces_one(api, garden):
    task = garden["tasks"][0]
    other_species = api.create_mote({"name": "Other"})["id"]
    api.inspector_set_species(task, other_species)
    before = {s["otherId"] for s in api.inspect_mote(task)["relationships"] if s["type"] == "instance_of"}
    assert api.undo()["undone"] is not None
    after = {s["otherId"] for s in api.inspect_mote(task)["relationships"] if s["type"] == "instance_of"}
    assert after == {garden["species"]}
    assert before == {other_species}


def test_relate_can_be_undone(api, garden):
    person = api.create_mote({"name": "Amara Voss"})["id"]
    task = garden["tasks"][0]
    api.inspector_relate(task, person, "assigned_to")
    assert api.undo()["undone"] is not None
    payload = api.inspect_mote(task)
    assert not [r for r in payload["relationships"] if r["type"] == "assigned_to"]


def test_relate_and_unrelate(api, garden):
    person = api.create_mote({"name": "Amara Voss"})["id"]
    task = garden["tasks"][0]

    payload = api.inspector_relate(task, person, "assigned_to")
    added = [r for r in payload["relationships"] if r["type"] == "assigned_to"]
    assert [r["otherLabel"] for r in added] == ["Amara Voss"]

    payload = api.inspector_unrelate(task, added[0]["id"])
    assert not [r for r in payload["relationships"] if r["type"] == "assigned_to"]


def test_relate_with_a_blank_type_falls_back(api, garden):
    payload = api.inspector_relate(garden["tasks"][0], garden["tasks"][1], "   ")
    assert "related_to" in {r["type"] for r in payload["relationships"]}


def test_setting_a_species_replaces_rather_than_accumulates(api, garden):
    """The panel shows one species in one control, so picking a new one
    means replacing what that control was showing."""
    other = api.create_mote({"name": "Note"})["id"]
    task = garden["tasks"][0]

    payload = api.inspector_set_species(task, other)
    assert payload["species"]["label"] == "Note"
    assert len(api.species_of(task)) == 1


def test_a_species_can_be_cleared(api, garden):
    payload = api.inspector_set_species(garden["tasks"][0], "")
    assert payload["species"]["id"] is None


def test_set_template_marks_the_mote_as_its_species_template(api, garden):
    task = garden["tasks"][0]
    payload = api.inspector_set_template(task, garden["species"])
    assert payload["species"]["isTemplate"] is True


def test_why_is_none_where_there_is_no_solver(api, garden):
    """Only Motion can answer this; the other five say so rather than
    making the frontend sniff the bridge for the method."""
    assert api.inspector_why(garden["tasks"][0], "status") is None


# ---- the claim this file exists to hold up ----

CONTRACT = [
    "inspect_mote", "inspector_search", "inspector_set_property",
    "inspector_delete_property", "inspector_rename_property",
    "inspector_relate", "inspector_unrelate", "inspector_set_species",
    "inspector_set_template", "inspector_open_link", "inspector_why",
]


@pytest.mark.parametrize("module", [
    monarch.api, motion.api, monolith.api, mora.api, monastery.api,
])
def test_every_window_speaks_the_whole_contract(module):
    """The inspector is one surface in six windows, so it has to find the
    same methods on every bridge it is dropped into. A window that grew
    its own Api without the mixin would fail here rather than at the
    first click in a panel that silently does nothing."""
    assert issubclass(module.Api, InspectorApi)
    for name in CONTRACT:
        assert callable(getattr(module.Api, name, None)), f"{module.__name__} lacks {name}"


def test_motion_overrides_the_two_it_must(api):
    """Motion is the only window that answers `why`, and the only one
    whose writes can land on a scenario's override instead of the mote.
    Both are overrides of the shared contract, not additions beside it."""
    assert motion.api.Api.inspector_why is not InspectorApi.inspector_why
    assert motion.api.Api.inspector_set_property is not InspectorApi.inspector_set_property
    assert monarch.api.Api.inspector_why is InspectorApi.inspector_why


def test_replacing_a_template_removes_the_old_one(api, garden):
    """core.species.template_for uses only the first template it finds,
    so adding a second would leave the replacement written and the old
    one still answering."""
    api.inspector_set_template(garden["tasks"][0], garden["species"])
    assert api.template_for(garden["species"])["id"] == garden["tasks"][0]
    assert api.inspect_mote(garden["template"])["species"]["isTemplate"] is False

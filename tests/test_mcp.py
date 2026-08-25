import pytest

from core.db import get_connection
from core.rules import APPLIES_TO
from core.simulation import list_runs as core_list_runs
from core.simulation import run_simulation as core_run_simulation
from core.species import INSTANCE_OF, TEMPLATE_OF
from mcp_server.server import (
    check_roles,
    consumers,
    create_garden,
    create_instance,
    create_mote,
    create_portal,
    create_relationship,
    delete_mote,
    delete_property,
    delete_relationship,
    export_garden,
    folder_members,
    folders_of,
    get_mote,
    get_run,
    impact,
    instances_of,
    list_folders,
    list_gardens,
    list_library,
    list_motes,
    list_relationships,
    list_runs,
    list_scenarios,
    list_species,
    mote_relationships,
    neighborhood,
    preview_simulation,
    producers,
    requires,
    resolve_portal,
    reserved_names,
    resolve_modoc,
    reuse_from_library,
    save_to_library,
    set_folder,
    run_ticks,
    search_motes,
    set_species,
    set_template,
    species_of,
    sync_init,
    sync_pull,
    sync_push,
    sync_run,
    sync_status,
    template_for,
    update_mote,
    update_relationship,
    whole_garden,
    why,
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
    # monarch/api.py coerces incoming strings through json.loads because GUI
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


def test_create_portal_and_resolve_round_trip():
    create_garden("other")
    target = create_mote({"name": "Target"}, "other")

    portal = create_portal("other", target["id"], garden=GARDEN)
    assert portal["properties"]["portal_garden"] == "other"
    assert portal["properties"]["portal_mote"] == target["id"]

    result = resolve_portal(portal["id"], GARDEN)
    assert result["garden"] == "other"
    assert result["mote"]["id"] == target["id"]


def test_create_portal_rejects_missing_target_garden():
    with pytest.raises(LookupError):
        create_portal("nonexistent", "some-id", garden=GARDEN)


def test_resolve_portal_rejects_non_portal_mote():
    plain = create_mote({"name": "Plain"}, GARDEN)
    with pytest.raises(ValueError):
        resolve_portal(plain["id"], GARDEN)


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
    assert len(list_relationships(type="back", garden=GARDEN)) == 1
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
    species = create_mote({"name": "Box", "freedoms": "area"}, GARDEN)
    rule = create_mote({"name": "Area", "equation": "area == width * height"}, GARDEN)
    create_relationship(rule["id"], species["id"], APPLIES_TO, GARDEN)

    instance = create_mote({"name": "B1", "width": 3, "height": 4}, GARDEN)
    set_species(instance["id"], species["id"], GARDEN)
    return {**instance, "species_id": species["id"]}


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


# ---- notes ----


def test_resolve_modoc_returns_the_source_and_what_it_resolves_to():
    project = create_mote({"name": "Atlas", "due": "2026-09-30"}, GARDEN)
    task = create_mote({"name": "T0"}, GARDEN)
    create_relationship(project["id"], task["id"], "contains", GARDEN)
    update_mote(
        project["id"],
        {"modoc": "Due \\val{due} for \\mote{T0}, \\val{count(targets('contains'))} task."},
        GARDEN,
    )

    result = resolve_modoc(project["id"], garden=GARDEN)
    assert result["anchorId"] == project["id"]
    assert result["projected"] is False
    assert result["values"]["due"]["text"] == "2026-09-30"
    assert result["values"]["count(targets('contains'))"]["text"] == "1"
    assert result["links"]["T0"]["mote_id"] == task["id"]
    assert result["diagnostics"] == []


def test_resolve_modoc_reports_what_did_not_resolve():
    note = create_mote({"name": "N", "modoc": "\\mote{Nowhere} \\bogus"}, GARDEN)
    result = resolve_modoc(note["id"], garden=GARDEN)
    assert result["links"]["Nowhere"]["mote_id"] is None
    assert sorted(d["kind"] for d in result["diagnostics"]) == [
        "unknown_command", "unresolved_link",
    ]


def test_resolve_modoc_at_a_step_projects_without_writing(box):
    update_mote(box["id"], {"modoc": "Area is \\val{area}."}, GARDEN)

    result = resolve_modoc(box["id"], step=1, garden=GARDEN)
    assert result["projected"] is True
    assert result["values"]["area"]["text"] == "12"
    assert "area" not in get_mote(box["id"], GARDEN)["properties"]
    assert list_runs(garden=GARDEN) == []


def test_resolve_modoc_on_a_mote_with_no_note_is_empty_not_an_error():
    mote = create_mote({"name": "Plain"}, GARDEN)
    result = resolve_modoc(mote["id"], garden=GARDEN)
    assert result["source"] == "" and result["values"] == {}


# ---- conventions ----


def test_reserved_names_lists_the_interpreted_keys():
    names = reserved_names()
    assert "equation" in names["properties"]
    assert "portal_garden" in names["properties"]
    assert "portal_mote" in names["properties"]
    assert "modoc" in names["properties"]
    assert INSTANCE_OF in names["relationshipTypes"]
    assert "about" in names["relationshipTypes"]


def test_reserved_names_includes_the_equation_and_provider_reference():
    # This is what makes cross-mote references (targets()/sources()) and
    # property-model providers discoverable to an MCP-only client, which
    # can't read docs/simulation.md directly.
    names = reserved_names()
    assert names["propertyMeanings"]["equation"]
    assert names["relationshipTypeMeanings"][INSTANCE_OF]
    syntaxes = {entry["syntax"] for entry in names["equationLanguage"]}
    assert any("targets(" in s for s in syntaxes)
    assert any("prev(" in s for s in syntaxes)
    assert "ideal_gas" in names["propertyModelProviders"]
    assert "coolprop" in names["propertyModelProviders"]


# ---- run_ticks: the same steps, the same shape ----

def test_run_ticks_matches_preview_simulations_shape(box):
    """A run's steps are the same steps whether freshly solved or read
    back out of the log, so an MCP client shouldn't have to unpack them
    two different ways -- run_ticks used to return a bare list while
    preview_simulation returned {"steps": [...]}."""
    from core.db import initialize_database
    from core.simulation import run_simulation

    conn = get_connection(GARDEN)
    try:
        initialize_database(conn)
        result = run_simulation(conn)
    finally:
        conn.close()

    preview = preview_simulation(1, GARDEN)
    logged = run_ticks(result.run_id, GARDEN)

    assert set(logged) == {"steps"}
    assert len(logged["steps"]) == 1
    assert set(logged["steps"][0]) <= set(preview["steps"][0])
    assert {"step", "changes", "summary"} <= set(logged["steps"][0])


def test_run_ticks_for_an_unknown_run_is_empty(box):
    assert run_ticks("nonexistent", GARDEN) == {"steps": []}


# ---- folders ----
#
# Folders existed only in the app before this: the charter says MCP gets
# reads plus graph edits, and filing a mote is both.

def test_set_folder_then_read_it_back_from_either_end(box):
    folder = create_mote({"name": "Thermodynamics"}, GARDEN)
    note = create_mote({"name": "Entropy"}, GARDEN)

    rel = set_folder(note["id"], folder["id"], GARDEN)
    assert rel["type"] == "in_folder"

    assert [m["id"] for m in list_folders(GARDEN)] == [folder["id"]]
    assert [m["id"] for m in folders_of(note["id"], GARDEN)] == [folder["id"]]
    assert [m["id"] for m in folder_members(folder["id"], GARDEN)] == [note["id"]]


def test_set_folder_is_additive(box):
    a = create_mote({"name": "A"}, GARDEN)
    b = create_mote({"name": "B"}, GARDEN)
    note = create_mote({"name": "Note"}, GARDEN)

    set_folder(note["id"], a["id"], GARDEN)
    set_folder(note["id"], b["id"], GARDEN)

    assert {m["id"] for m in folders_of(note["id"], GARDEN)} == {a["id"], b["id"]}


def test_list_folders_is_empty_without_any(box):
    assert list_folders(GARDEN) == []


# ---- the library ----

def test_save_to_library_then_list_and_reuse(box):
    mote = create_mote({"name": "Standup", "duration": 15}, GARDEN)

    entry = save_to_library(mote["id"], GARDEN)
    assert entry["properties"] == {"name": "Standup", "duration": 15}
    assert [m["id"] for m in list_library()] == [entry["id"]]

    copy = reuse_from_library(entry["id"], {"name": "Monday standup"}, GARDEN)
    assert copy["properties"] == {"name": "Monday standup", "duration": 15}
    assert copy["id"] not in (mote["id"], entry["id"])
    # A copy, not a link: the entry is untouched and the new mote lives
    # in the caller's garden.
    assert get_mote(copy["id"], GARDEN) is not None
    assert [m["properties"]["name"] for m in list_library()] == ["Standup"]


def test_the_library_is_not_offered_as_an_ordinary_garden(box):
    mote = create_mote({"name": "Task"}, GARDEN)
    save_to_library(mote["id"], GARDEN)

    # It exists and list_library() reads it, but it is not a workspace.
    assert list_library() != []
    assert "_library" not in list_gardens()



# ---- provenance ----


def test_why_explains_a_solved_value(box):
    conn = get_connection(GARDEN)
    try:
        core_run_simulation(conn)
    finally:
        conn.close()

    explanation = why(box["id"], "area", garden=GARDEN)
    assert explanation["value"] == 12.0
    assert explanation["method"] == "substitution"
    assert [e["rule_name"] for e in explanation["equations"]] == ["Area"]
    assert {i["key"] for i in explanation["inputs"]} == {"width", "height"}


def test_why_on_an_underived_value_returns_a_note_not_an_error(box):
    conn = get_connection(GARDEN)
    try:
        core_run_simulation(conn)
    finally:
        conn.close()

    explanation = why(box["id"], "width", garden=GARDEN)
    # asdict() preserves the dataclass's tuples; they reach a client as
    # JSON arrays either way, so what matters is that it is empty.
    assert not explanation["equations"]
    assert explanation["note"]


def test_why_before_any_run_says_the_garden_has_no_runs(box):
    assert why(box["id"], "area", garden=GARDEN)["note"] == "this garden has no logged runs"


# ---- dependency queries ----


def test_impact_reports_what_could_move(box):
    result = impact(box["id"], "width", garden=GARDEN)
    assert [(e["key"], e["distance"]) for e in result["effects"]] == [("area", 1)]
    assert result["effects"][0]["through"][0]["rule_name"] == "Area"


def test_requires_reports_prerequisites_and_their_status(box):
    result = requires(box["id"], "area", garden=GARDEN)
    assert result["free"] is True
    assert result["determinable"] is True
    assert {n["key"]: n["status"] for n in result["needs"]} == {
        "width": "stored", "height": "stored"
    }


def test_requires_flags_a_value_that_is_not_free(box):
    result = requires(box["id"], "width", garden=GARDEN)
    assert result["free"] is False
    assert result["determinable"] is False


def test_producers_and_consumers_read_the_same_index(box):
    assert [t["rule_name"] for t in producers(box["id"], "area", garden=GARDEN)] == ["Area"]
    assert [t["rule_name"] for t in consumers(box["id"], "width", garden=GARDEN)] == ["Area"]


def test_the_queries_answer_before_any_run(box):
    """No simulation has been run in this garden -- the index comes from
    the rules, not the run log."""
    assert list_runs(garden=GARDEN) == []
    assert producers(box["id"], "area", garden=GARDEN)


# ---- roles ----


def test_check_roles_reports_wiring_problems(box):
    whole = create_mote({"name": "W1", "roles": ["left"]}, GARDEN)
    report = check_roles(garden=GARDEN)

    assert report["motes_checked"] == 1
    assert len(report["problems"]) == 1
    assert "'left'" in report["problems"][0]

    part = create_mote({"name": "Left"}, GARDEN)
    create_relationship(whole["id"], part["id"], "left", GARDEN)
    assert check_roles(garden=GARDEN)["problems"] == []


def test_check_roles_reports_once_scoped_rules(box):
    mote = create_mote({"name": "Solo", "freedoms": "x"}, GARDEN)
    rule = create_mote({"name": "One-off", "equation": "x == 1"}, GARDEN)
    create_relationship(rule["id"], mote["id"], APPLIES_TO, GARDEN)

    assert check_roles(garden=GARDEN)["once_scoped"] == [
        "'One-off' is asserted once, about Solo"
    ]


def test_roles_is_a_reserved_property():
    assert "roles" in reserved_names()["properties"]


# ---- scenarios ----


def _scenario_over(box):
    override = create_mote({"width": 10}, GARDEN)
    create_relationship(override["id"], box["id"], "overrides", GARDEN)
    scenario = create_mote({"name": "Wide"}, GARDEN)
    create_relationship(scenario["id"], override["id"], "varies", GARDEN)
    return scenario["id"]


def test_list_scenarios_reports_what_each_varies(box):
    scenario_id = _scenario_over(box)
    scenarios = list_scenarios(garden=GARDEN)

    assert [s["name"] for s in scenarios] == ["Wide"]
    assert scenarios[0]["id"] == scenario_id
    assert scenarios[0]["overrides"][0]["target_mote_id"] == box["id"]
    assert scenarios[0]["overrides"][0]["properties"] == {"width": 10}


def test_list_scenarios_is_empty_when_nothing_varies(box):
    assert list_scenarios(garden=GARDEN) == []


def test_preview_simulation_can_be_run_under_a_scenario(box):
    scenario_id = _scenario_over(box)

    plain = preview_simulation(1, GARDEN)
    assert plain["steps"][0]["changes"][0]["new_value"] == 12.0

    varied = preview_simulation(1, GARDEN, scenario=scenario_id)
    assert varied["steps"][0]["changes"][0]["new_value"] == 40.0
    assert "area" not in get_mote(box["id"], GARDEN)["properties"]


def test_the_scenario_relationship_types_are_reserved():
    types = reserved_names()["relationshipTypes"]
    assert {"overrides", "varies"} <= set(types)


# ---- constraints ----


def test_preview_simulation_reports_constraint_violations(box):
    limit = create_mote({"name": "Too big", "equation": "area <= 5"}, GARDEN)
    create_relationship(limit["id"], box["species_id"], APPLIES_TO, GARDEN)

    result = preview_simulation(1, GARDEN)
    assert result["error"] is None
    assert len(result["constraint_violations"]) == 1
    assert "Too big" in result["constraint_violations"][0]
    # Advisory: the preview still produced its answer.
    assert result["steps"][0]["changes"][0]["new_value"] == 12.0


def test_preview_simulation_is_quiet_when_constraints_hold(box):
    limit = create_mote({"name": "Fine", "equation": "area <= 100"}, GARDEN)
    create_relationship(limit["id"], box["species_id"], APPLIES_TO, GARDEN)

    assert preview_simulation(1, GARDEN)["constraint_violations"] == []


# ---- sync ----
# Just the MCP plumbing -- core/sync.py's own multi-device merge
# behavior (tests/test_sync.py) is exercised there, since simulating a
# second device needs switching GARDENS_DIR mid-test, which none of the
# other tools here do.


def test_sync_init_then_status_reports_the_folder(tmp_path):
    sync_dir = str(tmp_path / "shared")
    result = sync_init(sync_dir, GARDEN)
    assert result["garden"] == GARDEN
    assert result["cloned"] is False

    status = sync_status(GARDEN)
    assert status["sync_dir"] == sync_dir
    assert status["remotes"] == []


def test_sync_push_then_status_shows_nothing_pending(tmp_path):
    create_mote({"name": "Alice"}, GARDEN)
    sync_init(str(tmp_path / "shared"), GARDEN)

    pushed = sync_push(GARDEN)
    assert pushed["pushed"] == 1
    assert sync_status(GARDEN)["local_ops_pending"] == 0


def test_sync_pull_with_no_other_device_is_a_noop(tmp_path):
    sync_init(str(tmp_path / "shared"), GARDEN)
    assert sync_pull(GARDEN) == {"applied": 0, "skipped": []}


def test_sync_run_pushes_and_pulls_in_one_call(tmp_path):
    create_mote({"name": "Alice"}, GARDEN)
    sync_init(str(tmp_path / "shared"), GARDEN)

    result = sync_run(GARDEN)
    assert result["push"]["pushed"] == 1
    assert result["pull"] == {"applied": 0, "skipped": []}

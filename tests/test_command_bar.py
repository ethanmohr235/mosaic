"""core/command_bar.py -- the "/" command bar's grammar and execution."""
import pytest

from core import motes as motes_repo
from core import relationships as rel_repo
from core import rules as rules_repo
from core import species as species_repo
from core.command_bar import run_command, suggest


def _name(conn, mote_id):
    mote = motes_repo.get_mote(conn, mote_id)
    return mote.properties.get("name")


# ---- property form ----

def test_sets_a_property_and_creates_the_mote(conn):
    result = run_command(conn, "R1.resistance = 500 ohms")
    assert result.ok
    mote = motes_repo.get_mote(conn, result.mote_id)
    assert mote.properties["resistance"] == 500
    assert mote.properties["units"] == {"resistance": "ohm"}
    assert mote.properties["name"] == "R1"


def test_tolerates_plural_units(conn):
    result = run_command(conn, "R1.resistance = 500 ohms")
    assert motes_repo.get_mote(conn, result.mote_id).properties["units"]["resistance"] == "ohm"


def test_reuses_an_existing_mote_by_name(conn):
    first = run_command(conn, "R1.resistance = 500 ohms")
    second = run_command(conn, "R1.voltage = 12 V")
    assert first.mote_id == second.mote_id
    mote = motes_repo.get_mote(conn, second.mote_id)
    assert mote.properties["resistance"] == 500
    assert mote.properties["voltage"] == 12


def test_value_without_a_recognized_unit_is_stored_plain(conn):
    result = run_command(conn, "R1.label = 500 widgets")
    mote = motes_repo.get_mote(conn, result.mote_id)
    assert mote.properties["label"] == "500 widgets"
    assert "units" not in mote.properties


def test_value_coerces_through_json(conn):
    result = run_command(conn, "R1.active = true")
    assert motes_repo.get_mote(conn, result.mote_id).properties["active"] is True


def test_value_accepts_scientific_notation_with_a_unit(conn):
    result = run_command(conn, "n.m = 1.6749E-27 kg")
    mote = motes_repo.get_mote(conn, result.mote_id)
    assert mote.properties["m"] == pytest.approx(1.6749e-27)
    assert mote.properties["units"] == {"m": "kg"}


def test_value_accepts_scientific_notation_without_a_unit(conn):
    result = run_command(conn, "n.m = 1.6749E-27")
    mote = motes_repo.get_mote(conn, result.mote_id)
    assert mote.properties["m"] == pytest.approx(1.6749e-27)
    assert "units" not in mote.properties


def test_percent_value_parses_without_a_space(conn):
    # Nobody writes `50 %`, so the parser splits a trailing `%` off the
    # number -- the one unit that doesn't need the separating space.
    result = run_command(conn, "Cell.efficiency = 22.5%")
    mote = motes_repo.get_mote(conn, result.mote_id)
    assert mote.properties["efficiency"] == pytest.approx(22.5)
    assert mote.properties["units"] == {"efficiency": "%"}


def test_percent_value_also_parses_with_a_space(conn):
    result = run_command(conn, "Cell.efficiency = 22.5 %")
    mote = motes_repo.get_mote(conn, result.mote_id)
    assert mote.properties["efficiency"] == pytest.approx(22.5)
    assert mote.properties["units"] == {"efficiency": "%"}


def test_second_units_write_merges_rather_than_replaces(conn):
    run_command(conn, "R1.resistance = 500 ohms")
    result = run_command(conn, "R1.voltage = 12 V")
    mote = motes_repo.get_mote(conn, result.mote_id)
    assert mote.properties["units"] == {"resistance": "ohm", "voltage": "V"}


def test_units_command_annotates_an_existing_property(conn):
    run_command(conn, "R1.resistance = 500")
    result = run_command(conn, "R1.resistance units kohm")
    assert result.ok
    mote = motes_repo.get_mote(conn, result.mote_id)
    assert mote.properties["resistance"] == 500
    assert mote.properties["units"] == {"resistance": "kohm"}


def test_units_command_does_not_touch_the_value(conn):
    run_command(conn, "R1.resistance = 500 ohms")
    result = run_command(conn, "R1.resistance units kohm")
    mote = motes_repo.get_mote(conn, result.mote_id)
    assert mote.properties["resistance"] == 500
    assert mote.properties["units"]["resistance"] == "kohm"


def test_units_command_creates_the_mote_if_missing(conn):
    result = run_command(conn, "W1.height units ft")
    assert result.ok
    mote = motes_repo.get_mote(conn, result.mote_id)
    assert mote.properties["name"] == "W1"
    assert mote.properties["units"] == {"height": "ft"}
    assert "height" not in mote.properties


def test_units_command_merges_with_existing_units(conn):
    run_command(conn, "R1.resistance = 500 ohms")
    result = run_command(conn, "R1.voltage units V")
    mote = motes_repo.get_mote(conn, result.mote_id)
    assert mote.properties["units"] == {"resistance": "ohm", "voltage": "V"}


def test_units_command_rejects_an_unrecognized_unit(conn):
    result = run_command(conn, "R1.resistance units furlongs")
    assert not result.ok
    assert "furlongs" in result.message


def test_units_command_tolerates_plurals(conn):
    result = run_command(conn, "R1.resistance units ohms")
    assert result.ok
    mote = motes_repo.get_mote(conn, result.mote_id)
    assert mote.properties["units"]["resistance"] == "ohm"


def test_ambiguous_mote_name_is_reported_with_choices(conn):
    motes_repo.create_mote(conn, {"name": "R1"})
    motes_repo.create_mote(conn, {"name": "R1"})
    result = run_command(conn, "R1.resistance = 500 ohms")
    assert not result.ok
    assert result.choices and len(result.choices) == 2


def test_hash_id_bypasses_name_lookup(conn):
    motes_repo.create_mote(conn, {"name": "R1"})
    dup = motes_repo.create_mote(conn, {"name": "R1"})
    result = run_command(conn, f"#{dup}.resistance = 500 ohms")
    assert result.ok
    assert result.mote_id == dup


# ---- rule form ----

def test_creates_a_rule_attached_to_the_anchor(conn):
    anchor = run_command(conn, "R1.resistance = 500 ohms").mote_id
    result = run_command(conn, "R1.voltage == R1.current * R1.resistance")
    assert result.ok
    rule = motes_repo.get_mote(conn, result.mote_id)
    # The anchor's own prefix is stripped -- the equation runs as a bare
    # property lookup once the rule applies_to R1.
    assert rule.properties[rules_repo.EQUATION_KEY] == "voltage == current * resistance"
    rels = rel_repo.list_relationships(conn, from_id=result.mote_id, type_=rules_repo.APPLIES_TO)
    assert len(rels) == 1 and rels[0].to_id == anchor


def test_rule_creates_the_anchor_if_missing(conn):
    result = run_command(conn, "R1.voltage == current * resistance")
    assert result.ok
    rels = rel_repo.list_relationships(conn, type_=rules_repo.APPLIES_TO)
    assert _name(conn, rels[0].to_id) == "R1"


def test_rule_draws_no_edge_for_another_mote_it_names(conn):
    # Garden-scope names made the auto-drawn `related_to` unnecessary, so
    # a rule leaves the garden's relationships alone (roadmap phase 2).
    r1 = run_command(conn, "R1.resistance = 500 ohms").mote_id
    r2 = run_command(conn, "R2.voltage = 5 V").mote_id
    result = run_command(conn, "R1.voltage == R2.voltage")
    assert result.ok
    assert "ambiguous" not in result.message
    assert not [r for r in rel_repo.list_relationships(conn, from_id=r1) if r.to_id == r2]


def test_rule_does_not_flag_a_connected_mote(conn):
    r1 = run_command(conn, "R1.resistance = 500 ohms").mote_id
    r2 = run_command(conn, "R2.voltage = 5 V").mote_id
    rel_repo.create_relationship(conn, r1, r2, "related_to")
    result = run_command(conn, "R1.x == R2.voltage")
    assert "ambiguous" not in result.message
    rels = rel_repo.list_relationships(conn, from_id=r1)
    assert sum(1 for r in rels if r.to_id == r2) == 1


def test_rule_flags_an_ambiguous_name(conn):
    run_command(conn, "R1.resistance = 500 ohms")
    run_command(conn, "R2.voltage = 5 V")
    run_command(conn, "R3.voltage = 9 V")
    motes_repo.update_mote(conn, run_command(conn, "R3").mote_id, {"name": "R2"})
    result = run_command(conn, "R1.x == R2.voltage")
    assert result.ok
    assert "R2" in result.message and "ambiguous" in result.message
    rels = rel_repo.list_relationships(conn, from_id=result.mote_id)
    assert all(r.type == rules_repo.APPLIES_TO for r in rels)


def test_rule_stubs_own_properties_as_empty(conn):
    rule_id = run_command(conn, "v.x == v.y * 2").mote_id
    rels = rel_repo.list_relationships(conn, from_id=rule_id, type_=rules_repo.APPLIES_TO)
    mote = motes_repo.get_mote(conn, rels[0].to_id)
    assert mote.properties["x"] == ""
    assert mote.properties["y"] == ""


def test_rule_stub_does_not_overwrite_an_existing_value(conn):
    v = motes_repo.create_mote(conn, {"name": "v", "y": 5})
    run_command(conn, "v.x == v.y * 2")
    mote = motes_repo.get_mote(conn, v)
    assert mote.properties["y"] == 5
    assert mote.properties["x"] == ""


def test_rule_stubs_a_property_on_an_already_resolvable_other_mote(conn):
    r2 = motes_repo.create_mote(conn, {"name": "R2"})
    run_command(conn, "R1.voltage == R2.current * 2")
    assert motes_repo.get_mote(conn, r2).properties["current"] == ""


def test_rule_does_not_stub_an_unresolvable_other_mote(conn):
    result = run_command(conn, "R1.voltage == R2.current * 2")
    assert result.ok
    # No mote named R2 exists to stub -- only R1 (and the rule) were created.
    names = {m.properties.get("name") for m in motes_repo.list_motes(conn)}
    assert "R2" not in names


def test_rule_mote_is_named_from_the_stripped_equation(conn):
    result = run_command(conn, "v.x == v.y * 2")
    rule = motes_repo.get_mote(conn, result.mote_id)
    assert rule.properties["name"] == "Rule: x == y * 2"


def test_rule_left_side_may_be_any_expression(conn):
    """The reason this bar exists is to type what you mean. `beam.I /
    beam.I0 == 0.1` is an equation; it shouldn't have to be rearranged
    into an assignment to be typeable."""
    result = run_command(conn, "beam.I / beam.I0 == 0.1")
    assert result.ok
    rule = motes_repo.get_mote(conn, result.mote_id)
    assert rule.properties[rules_repo.EQUATION_KEY] == "I / I0 == 0.1"
    rels = rel_repo.list_relationships(
        conn, from_id=result.mote_id, type_=rules_repo.APPLIES_TO
    )
    assert _name(conn, rels[0].to_id) == "beam"


def test_rule_anchor_is_the_first_mote_named(conn):
    result = run_command(conn, "A.x * 2 == B.y")
    assert result.ok
    rels = rel_repo.list_relationships(
        conn, from_id=result.mote_id, type_=rules_repo.APPLIES_TO
    )
    assert _name(conn, rels[0].to_id) == "A"
    rule = motes_repo.get_mote(conn, result.mote_id)
    assert rule.properties[rules_repo.EQUATION_KEY] == "x * 2 == B.y"


def test_rule_with_no_mote_named_is_rejected(conn):
    result = run_command(conn, "R1 == 5")
    assert not result.ok
    assert "which mote" in result.message


def test_rule_that_does_not_parse_is_rejected_at_the_bar(conn):
    # Better here than as a rule mote that only fails at the next run.
    result = run_command(conn, "R1.x == 3 +")
    assert not result.ok
    names = {m.properties.get("name") for m in motes_repo.list_motes(conn)}
    assert not any(str(n).startswith("Rule:") for n in names if n)


# ---- freedom form ----

def test_freedom_marks_a_property_free_and_creates_the_mote(conn):
    result = run_command(conn, "R1.resistance ?")
    assert result.ok
    mote = motes_repo.get_mote(conn, result.mote_id)
    assert rules_repo.parse_freedoms(mote.properties[rules_repo.FREEDOMS_KEY]) == ["resistance"]


def test_freedom_toggles_off_on_second_use(conn):
    run_command(conn, "R1.resistance ?")
    result = run_command(conn, "R1.resistance ?")
    assert result.ok
    mote = motes_repo.get_mote(conn, result.mote_id)
    assert rules_repo.parse_freedoms(mote.properties[rules_repo.FREEDOMS_KEY]) == []


def test_freedom_adds_to_existing_freedoms(conn):
    r1 = motes_repo.create_mote(conn, {"name": "R1", rules_repo.FREEDOMS_KEY: "voltage"})
    result = run_command(conn, "R1.resistance ?")
    assert result.ok
    freedoms = rules_repo.parse_freedoms(motes_repo.get_mote(conn, r1).properties[rules_repo.FREEDOMS_KEY])
    assert set(freedoms) == {"voltage", "resistance"}


# ---- relationship form ----

def test_relate_defaults_to_related_to_and_creates_both_ends(conn):
    result = run_command(conn, "R1 -> R2")
    assert result.ok
    rels = rel_repo.list_relationships(conn)
    assert len(rels) == 1
    assert rels[0].type == "related_to"
    assert _name(conn, rels[0].from_id) == "R1"
    assert _name(conn, rels[0].to_id) == "R2"


def test_relate_with_explicit_type(conn):
    run_command(conn, "R1 -> owns -> R2")
    rels = rel_repo.list_relationships(conn)
    assert rels[0].type == "owns"


def test_relate_rejects_a_multi_word_type(conn):
    result = run_command(conn, "R1 -> owns this -> R2")
    assert not result.ok


# ---- species form ----

def test_species_sets_instance_of_and_creates_both(conn):
    result = run_command(conn, "R1 : Resistor")
    assert result.ok
    rels = rel_repo.list_relationships(conn, type_=species_repo.INSTANCE_OF)
    assert len(rels) == 1
    assert _name(conn, rels[0].to_id) == "Resistor"


def test_species_replaces_rather_than_accumulates(conn):
    run_command(conn, "R1 : Resistor")
    run_command(conn, "R1 : Capacitor")
    r1 = motes_repo.list_motes(conn)
    rels = rel_repo.list_relationships(conn, type_=species_repo.INSTANCE_OF)
    assert len(rels) == 1
    assert _name(conn, rels[0].to_id) == "Capacitor"


# ---- delete form ----

def test_delete_property(conn):
    mote_id = run_command(conn, "R1.resistance = 500 ohms").mote_id
    result = run_command(conn, "delete R1.resistance")
    assert result.ok
    assert "resistance" not in motes_repo.get_mote(conn, mote_id).properties


def test_delete_mote_needs_confirmation(conn):
    run_command(conn, "R1.resistance = 500 ohms")
    first = run_command(conn, "delete R1")
    assert not first.ok and first.needs_confirm
    assert len(motes_repo.list_motes(conn)) == 1

    second = run_command(conn, "delete R1", confirm=True)
    assert second.ok
    assert motes_repo.list_motes(conn) == []


def test_delete_unknown_mote_reports_an_error(conn):
    result = run_command(conn, "delete Nothing")
    assert not result.ok
    assert not result.needs_confirm


# ---- navigate form ----

def test_bare_name_navigates_without_creating(conn):
    made = run_command(conn, "R1.resistance = 500 ohms").mote_id
    result = run_command(conn, "R1")
    assert result.ok
    assert result.mote_id == made
    assert len(motes_repo.list_motes(conn)) == 1


def test_bare_unknown_name_creates_and_navigates(conn):
    result = run_command(conn, "Nowhere")
    assert result.ok
    motes = motes_repo.list_motes(conn)
    assert len(motes) == 1
    assert motes[0].id == result.mote_id
    assert motes[0].properties.get("name") == "Nowhere"


def test_empty_command_is_a_no_op(conn):
    result = run_command(conn, "   ")
    assert not result.ok
    assert motes_repo.list_motes(conn) == []


# ---- tab-completion ----

def test_suggest_completes_mote_names(conn):
    run_command(conn, "R1.resistance = 500 ohms")
    run_command(conn, "R2.resistance = 200 ohms")
    run_command(conn, "Lamp.voltage = 12 V")
    assert suggest(conn, "R") == ["R1", "R2"]


def test_suggest_completes_property_names_after_a_dot(conn):
    run_command(conn, "R1.resistance = 500 ohms")
    run_command(conn, "R1.voltage = 12 V")
    assert suggest(conn, "R1.v") == ["voltage"]
    assert set(suggest(conn, "R1.")) == {"name", "resistance", "units", "voltage"}


def test_suggest_is_empty_for_an_ambiguous_mote(conn):
    motes_repo.create_mote(conn, {"name": "R1"})
    motes_repo.create_mote(conn, {"name": "R1"})
    assert suggest(conn, "R1.") == []


def test_suggest_respects_cursor_not_just_text_end(conn):
    run_command(conn, "R1.resistance = 500 ohms")
    run_command(conn, "R12.resistance = 500 ohms")
    # Cursor sits right after "R1", ignoring the trailing "2.res" typed after it.
    assert suggest(conn, "R12.res", cursor=2) == ["R1", "R12"]
    assert suggest(conn, "R12.res", cursor=7) == ["resistance"]


def test_suggest_is_empty_with_no_partial_token(conn):
    run_command(conn, "R1.resistance = 500 ohms")
    assert suggest(conn, "R1 -> ") == []

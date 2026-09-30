"""core/batch.py -- one edit applied to many motes, as one undo step.

The three rules the module is built on are what earn tests: one primitive
write per mote, one undo step for the whole batch, and a missing id skipped
rather than fatal. Ported from tests/test_monolith_api.py, which asked the
same questions of the bridge that used to own this code.
"""
from core import batch
from core import units
from core.motes import create_mote, get_mote
from core.query import INSTANCE_OF
from core.relationships import create_relationship
from core.graph import whole_garden
from core.undo import UndoManager
import pytest


@pytest.fixture
def garden(conn) -> dict:
    species = create_mote(conn, {"name": "Task"})
    ids = {"species": species}
    for name, status in (("Schema", "doing"), ("Backfill", "doing"), ("Rehearsal", "todo")):
        ids[name.lower()] = create_mote(conn, {"name": name, "status": status, "estimate": 3})
        create_relationship(conn, ids[name.lower()], species, INSTANCE_OF)
    create_relationship(conn, ids["backfill"], ids["schema"], "depends_on")
    return ids


@pytest.fixture
def undo_stack() -> UndoManager:
    return UndoManager()


def apply(undo_stack, result):
    """What a surface does with a batch's answer: push the step if there
    was one. Returns the count, so a test reads like the call it wraps."""
    count, command = result
    if command is not None:
        undo_stack.push(command)
    return count


# ---- set_property ----

def test_set_property_writes_every_id_it_was_given_and_no_others(conn, garden):
    count, _ = batch.set_property(conn, [garden["schema"], garden["backfill"]], "owner", "Rina")
    assert count == 2
    assert get_mote(conn, garden["schema"]).properties["owner"] == "Rina"
    assert get_mote(conn, garden["backfill"]).properties["owner"] == "Rina"
    assert "owner" not in get_mote(conn, garden["rehearsal"]).properties


def test_a_typed_number_is_stored_as_a_number(conn, garden):
    """So that `estimate:>3` keeps meaning what it says after somebody
    types 5 into the cell."""
    batch.set_property(conn, [garden["schema"]], "estimate", "5")
    assert get_mote(conn, garden["schema"]).properties["estimate"] == 5


def test_a_typed_unit_annotates_the_motes_units_map(conn, garden):
    """The reason this reads a value the way the command bar does: `500
    ohm` is a number *and* a unit, and a unit-blind reading would store
    the word."""
    batch.set_property(conn, [garden["schema"]], "resistance", "500 ohm")
    properties = get_mote(conn, garden["schema"]).properties
    assert properties["resistance"] == 500
    assert properties[units.UNITS_KEY] == {"resistance": "ohm"}


def test_a_typed_unit_leaves_the_rest_of_the_units_map_alone(conn, garden):
    batch.set_property(conn, [garden["schema"]], "current", "3 mA")
    batch.set_property(conn, [garden["schema"]], "resistance", "500 ohm")
    assert get_mote(conn, garden["schema"]).properties[units.UNITS_KEY] == {
        "current": "mA", "resistance": "ohm",
    }


def test_a_value_that_is_not_a_string_is_written_as_it_came(conn, garden):
    batch.set_property(conn, [garden["schema"]], "done", True)
    assert get_mote(conn, garden["schema"]).properties["done"] is True


def test_a_stale_selection_writes_the_rows_that_are_still_there(conn, garden):
    count, _ = batch.set_property(
        conn, [garden["schema"], "deleted-by-another-window"], "owner", "Rina")
    assert count == 1


def test_a_property_needs_a_key(conn, garden):
    with pytest.raises(ValueError, match="needs a key"):
        batch.set_property(conn, [garden["schema"]], "", "Rina")


# ---- clear_property ----

def test_clearing_removes_the_key_rather_than_emptying_it(conn, garden):
    batch.clear_property(conn, [garden["schema"]], "status")
    assert "status" not in get_mote(conn, garden["schema"]).properties


def test_clearing_a_key_nobody_has_writes_nothing_and_pushes_nothing(conn, garden):
    count, command = batch.clear_property(conn, [garden["schema"]], "gone")
    assert count == 0
    assert command is None


def test_clearing_leaves_the_units_entry_alone(conn, garden):
    """A cleared value is one not yet filled in; what it will be measured
    in has not changed."""
    batch.set_property(conn, [garden["schema"]], "resistance", "500 ohm")
    batch.clear_property(conn, [garden["schema"]], "resistance")
    assert get_mote(conn, garden["schema"]).properties[units.UNITS_KEY] == {"resistance": "ohm"}


# ---- delete_motes ----

def test_deleting_takes_the_mote_and_its_relationships(conn, garden):
    count, _ = batch.delete_motes(conn, [garden["schema"]])
    assert count == 1
    assert get_mote(conn, garden["schema"]) is None
    _motes, relationships = whole_garden(conn)
    touched = {r.from_id for r in relationships} | {r.to_id for r in relationships}
    assert garden["schema"] not in touched


# ---- one undo step ----

def test_a_delete_is_undone_with_its_relationships(conn, garden, undo_stack):
    apply(undo_stack, batch.delete_motes(conn, [garden["schema"]]))

    assert undo_stack.undo() == "delete a mote"
    assert get_mote(conn, garden["schema"]).properties["name"] == "Schema"
    _motes, relationships = whole_garden(conn)
    pairs = {(r.from_id, r.to_id, r.type) for r in relationships}
    assert (garden["schema"], garden["species"], INSTANCE_OF) in pairs
    assert (garden["backfill"], garden["schema"], "depends_on") in pairs


def test_redoing_a_delete_removes_the_mote_again(conn, garden, undo_stack):
    apply(undo_stack, batch.delete_motes(conn, [garden["schema"]]))
    undo_stack.undo()

    assert undo_stack.redo() == "delete a mote"
    assert get_mote(conn, garden["schema"]) is None


def test_deleting_several_motes_is_undone_in_one_step(conn, garden, undo_stack):
    count = apply(undo_stack, batch.delete_motes(
        conn, [garden["schema"], garden["backfill"]]))
    assert count == 2

    assert undo_stack.undo() == "delete 2 motes"
    assert get_mote(conn, garden["schema"]) is not None
    assert get_mote(conn, garden["backfill"]) is not None
    assert undo_stack.undo() is None  # the whole batch was one step


def test_setting_a_property_on_many_motes_is_undone_in_one_step(conn, garden, undo_stack):
    apply(undo_stack, batch.set_property(
        conn, [garden["schema"], garden["backfill"]], "owner", "Rina"))

    assert undo_stack.undo() == "set owner on 2 motes"
    assert "owner" not in get_mote(conn, garden["schema"]).properties
    assert "owner" not in get_mote(conn, garden["backfill"]).properties
    assert undo_stack.undo() is None


def test_undoing_a_set_restores_the_value_that_was_there(conn, garden, undo_stack):
    apply(undo_stack, batch.set_property(conn, [garden["schema"]], "status", "blocked"))

    assert undo_stack.undo() == "set status"
    assert get_mote(conn, garden["schema"]).properties["status"] == "doing"


def test_undoing_a_unit_bearing_set_restores_the_units_map(conn, garden, undo_stack):
    apply(undo_stack, batch.set_property(conn, [garden["schema"]], "resistance", "500 ohm"))

    undo_stack.undo()
    properties = get_mote(conn, garden["schema"]).properties
    assert "resistance" not in properties
    assert units.UNITS_KEY not in properties


def test_undoing_a_clear_puts_the_key_back(conn, garden, undo_stack):
    apply(undo_stack, batch.clear_property(
        conn, [garden["schema"], garden["backfill"]], "status"))

    assert undo_stack.undo() == "clear status on 2 motes"
    assert get_mote(conn, garden["schema"]).properties["status"] == "doing"
    assert get_mote(conn, garden["backfill"]).properties["status"] == "doing"


def test_a_batch_that_wrote_nothing_pushes_nothing(conn, garden, undo_stack):
    count, command = batch.set_property(conn, ["gone", "also-gone"], "owner", "Rina")
    assert (count, command) == (0, None)


def test_a_one_mote_batch_is_labelled_in_the_singular(conn, garden, undo_stack):
    apply(undo_stack, batch.set_property(conn, [garden["schema"]], "owner", "Rina"))
    assert undo_stack.undo() == "set owner"

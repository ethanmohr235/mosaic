import time

import pytest

from core import motes as motes_repo
from core import relationships as rel_repo
from core import undo


def test_undo_with_empty_stack_returns_none():
    manager = undo.UndoManager()
    assert manager.undo() is None
    assert manager.can_undo() is False


def test_redo_with_empty_stack_returns_none():
    manager = undo.UndoManager()
    assert manager.redo() is None
    assert manager.can_redo() is False


def test_undo_runs_the_command_and_returns_its_label():
    manager = undo.UndoManager()
    ran = []
    manager.push(undo.Command(label="did a thing", undo=lambda: ran.append("undo"), redo=lambda: ran.append("redo")))

    assert manager.undo() == "did a thing"
    assert ran == ["undo"]
    assert manager.can_undo() is False


def test_redo_after_undo_reruns_the_redo_half():
    manager = undo.UndoManager()
    ran = []
    manager.push(undo.Command(label="did a thing", undo=lambda: ran.append("undo"), redo=lambda: ran.append("redo")))
    manager.undo()

    assert manager.redo() == "did a thing"
    assert ran == ["undo", "redo"]
    assert manager.can_redo() is False
    assert manager.can_undo() is True


def test_pushing_a_new_command_clears_the_redo_stack():
    manager = undo.UndoManager()
    manager.push(undo.Command(label="first", undo=lambda: None, redo=lambda: None))
    manager.undo()
    assert manager.can_redo() is True

    manager.push(undo.Command(label="second", undo=lambda: None, redo=lambda: None))
    assert manager.can_redo() is False


def test_multiple_undos_pop_in_reverse_order():
    manager = undo.UndoManager()
    manager.push(undo.Command(label="one", undo=lambda: None, redo=lambda: None))
    manager.push(undo.Command(label="two", undo=lambda: None, redo=lambda: None))

    assert manager.undo() == "two"
    assert manager.undo() == "one"
    assert manager.undo() is None


def test_eviction_past_limit_drops_the_oldest():
    manager = undo.UndoManager(limit=2)
    manager.push(undo.Command(label="one", undo=lambda: None, redo=lambda: None))
    manager.push(undo.Command(label="two", undo=lambda: None, redo=lambda: None))
    manager.push(undo.Command(label="three", undo=lambda: None, redo=lambda: None))

    assert manager.undo() == "three"
    assert manager.undo() == "two"
    assert manager.undo() is None


def test_coalescing_within_window_keeps_the_original_undo():
    manager = undo.UndoManager()
    manager.push(
        undo.Command(label="typed a", undo=lambda: "first-undo", redo=lambda: None),
        coalesce_key=("mote-1", "name"),
        coalesce_window=5,
    )
    manager.push(
        undo.Command(label="typed ab", undo=lambda: "second-undo", redo=lambda: None),
        coalesce_key=("mote-1", "name"),
        coalesce_window=5,
    )

    # Coalesced into one entry, and undoing it runs the *first* push's
    # undo -- the snapshot from before the burst started.
    assert len(manager._undo_stack) == 1
    assert manager.undo() == "typed ab"  # label follows the latest push
    assert manager._undo_stack == []


def test_coalescing_outside_window_pushes_a_separate_entry():
    manager = undo.UndoManager()
    manager.push(
        undo.Command(label="one", undo=lambda: None, redo=lambda: None),
        coalesce_key="k",
        coalesce_window=0,
    )
    time.sleep(0.01)
    manager.push(
        undo.Command(label="two", undo=lambda: None, redo=lambda: None),
        coalesce_key="k",
        coalesce_window=0,
    )

    assert manager.undo() == "two"
    assert manager.undo() == "one"


def test_different_coalesce_keys_do_not_merge():
    manager = undo.UndoManager()
    manager.push(
        undo.Command(label="one", undo=lambda: None, redo=lambda: None),
        coalesce_key="a",
        coalesce_window=5,
    )
    manager.push(
        undo.Command(label="two", undo=lambda: None, redo=lambda: None),
        coalesce_key="b",
        coalesce_window=5,
    )

    assert manager.undo() == "two"
    assert manager.undo() == "one"


# ---- Command factories, against a real garden connection ----------------


def test_for_create_mote_round_trips(conn):
    mote_id = motes_repo.create_mote(conn, {"name": "R1"})
    command = undo.for_create_mote(conn, mote_id, {"name": "R1"}, label="create R1")

    command.undo()
    assert motes_repo.get_mote(conn, mote_id) is None

    command.redo()
    restored = motes_repo.get_mote(conn, mote_id)
    assert restored is not None
    assert restored.properties == {"name": "R1"}


def test_for_delete_mote_restores_properties_and_relationships(conn):
    a = motes_repo.create_mote(conn, {"name": "A"})
    b = motes_repo.create_mote(conn, {"name": "B"})
    rel_id = rel_repo.create_relationship(conn, a, b, "depends_on")

    relationships = [{"id": rel_id, "fromId": a, "toId": b, "type": "depends_on"}]
    command = undo.for_delete_mote(conn, a, {"name": "A"}, relationships, label="delete A")

    motes_repo.delete_mote(conn, a)
    assert motes_repo.get_mote(conn, a) is None

    command.undo()
    restored = motes_repo.get_mote(conn, a)
    assert restored is not None
    assert restored.properties == {"name": "A"}
    assert rel_repo.get_relationship(conn, rel_id) is not None

    command.redo()
    assert motes_repo.get_mote(conn, a) is None


def test_for_update_mote_restores_old_values_and_deletes_new_keys(conn):
    mote_id = motes_repo.create_mote(conn, {"priority": "low"})
    old_props = {"priority": "low"}
    new_props = {"priority": "high", "owner": "ethan"}
    command = undo.for_update_mote(conn, mote_id, old_props, new_props, label="edit A")

    motes_repo.update_mote(conn, mote_id, new_props)
    assert motes_repo.get_mote(conn, mote_id).properties == {"priority": "high", "owner": "ethan"}

    command.undo()
    assert motes_repo.get_mote(conn, mote_id).properties == {"priority": "low"}

    command.redo()
    assert motes_repo.get_mote(conn, mote_id).properties == {"priority": "high", "owner": "ethan"}


def test_for_delete_property_restores_old_value(conn):
    mote_id = motes_repo.create_mote(conn, {"priority": "low"})
    command = undo.for_delete_property(conn, mote_id, "priority", "low", label="clear priority")

    motes_repo.delete_property(conn, mote_id, "priority")
    assert "priority" not in motes_repo.get_mote(conn, mote_id).properties

    command.undo()
    assert motes_repo.get_mote(conn, mote_id).properties == {"priority": "low"}

    command.redo()
    assert "priority" not in motes_repo.get_mote(conn, mote_id).properties


def test_for_create_relationship_round_trips(conn):
    a = motes_repo.create_mote(conn)
    b = motes_repo.create_mote(conn)
    rel_id = rel_repo.create_relationship(conn, a, b, "depends_on")
    command = undo.for_create_relationship(conn, rel_id, a, b, "depends_on", label="link A to B")

    command.undo()
    assert rel_repo.get_relationship(conn, rel_id) is None

    command.redo()
    assert rel_repo.get_relationship(conn, rel_id) is not None


def test_for_delete_relationship_round_trips(conn):
    a = motes_repo.create_mote(conn)
    b = motes_repo.create_mote(conn)
    rel_id = rel_repo.create_relationship(conn, a, b, "depends_on")
    command = undo.for_delete_relationship(conn, rel_id, a, b, "depends_on", label="unlink A from B")

    rel_repo.delete_relationship(conn, rel_id)
    assert rel_repo.get_relationship(conn, rel_id) is None

    command.undo()
    assert rel_repo.get_relationship(conn, rel_id) is not None

    command.redo()
    assert rel_repo.get_relationship(conn, rel_id) is None


def test_for_update_relationship_restores_old_fields(conn):
    a = motes_repo.create_mote(conn)
    b = motes_repo.create_mote(conn)
    c = motes_repo.create_mote(conn)
    rel_id = rel_repo.create_relationship(conn, a, b, "depends_on")
    command = undo.for_update_relationship(
        conn, rel_id, old_fields={"to_id": b}, new_fields={"to_id": c}, label="repoint to C",
    )

    rel_repo.update_relationship(conn, rel_id, to_id=c)
    assert rel_repo.get_relationship(conn, rel_id).to_id == c

    command.undo()
    assert rel_repo.get_relationship(conn, rel_id).to_id == b

    command.redo()
    assert rel_repo.get_relationship(conn, rel_id).to_id == c

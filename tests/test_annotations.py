import pytest

from core import annotations as ann
from core import db
from core import motes as motes_repo


def test_create_sticky_note_world_anchored(conn):
    aid = ann.create_sticky_note(conn, anchor_mode="world", x=1.0, y=2.0, text="hi")
    note = ann.get_annotation(conn, aid)

    assert note.anchor_mode == "world"
    assert note.mote_id is None
    assert note.x == 1.0
    assert note.y == 2.0
    assert note.text == "hi"
    assert note.type == "sticky_note"


def test_create_sticky_note_mote_anchored_requires_mote_id(conn):
    mote_id = motes_repo.create_mote(conn, {"name": "A"})
    aid = ann.create_sticky_note(conn, anchor_mode="mote", x=5, y=5, mote_id=mote_id)
    note = ann.get_annotation(conn, aid)
    assert note.mote_id == mote_id

    with pytest.raises(ValueError):
        ann.create_sticky_note(conn, anchor_mode="mote", x=0, y=0)


def test_create_sticky_note_rejects_invalid_anchor_mode(conn):
    with pytest.raises(ValueError):
        ann.create_sticky_note(conn, anchor_mode="bogus", x=0, y=0)


def test_list_and_get_annotations(conn):
    a = ann.create_sticky_note(conn, anchor_mode="world", x=0, y=0)
    b = ann.create_sticky_note(conn, anchor_mode="world", x=1, y=1)

    listed_ids = {note.id for note in ann.list_annotations(conn)}
    assert listed_ids == {a, b}
    assert ann.get_annotation(conn, "missing") is None


def test_update_annotation_updates_fields_and_bumps_updated_at(conn):
    aid = ann.create_sticky_note(conn, anchor_mode="world", x=0, y=0, text="old")
    before = ann.get_annotation(conn, aid).updated_at

    ann.update_annotation(conn, aid, text="new", color="#fff")
    note = ann.get_annotation(conn, aid)

    assert note.text == "new"
    assert note.color == "#fff"
    assert note.updated_at >= before


def test_update_annotation_rejects_unknown_field(conn):
    aid = ann.create_sticky_note(conn, anchor_mode="world", x=0, y=0)
    with pytest.raises(ValueError):
        ann.update_annotation(conn, aid, anchor_mode="mote")


def test_move_annotation_touches_only_position(conn):
    aid = ann.create_sticky_note(conn, anchor_mode="world", x=0, y=0, text="keep me")
    ann.move_annotation(conn, aid, 9, 9)
    note = ann.get_annotation(conn, aid)

    assert (note.x, note.y) == (9, 9)
    assert note.text == "keep me"


def test_delete_annotation(conn):
    aid = ann.create_sticky_note(conn, anchor_mode="world", x=0, y=0)
    assert ann.delete_annotation(conn, aid) is True
    assert ann.get_annotation(conn, aid) is None
    assert ann.delete_annotation(conn, aid) is False


def test_mote_anchored_annotation_survives_its_mote_being_deleted(conn):
    """The key regression test for the no-FK/no-cascade design: an
    annotation must never be silently destroyed, or block a delete, just
    because the mote it's anchored to goes away."""
    mote_id = motes_repo.create_mote(conn, {"name": "A"})
    aid = ann.create_sticky_note(conn, anchor_mode="mote", x=3, y=4, mote_id=mote_id)

    motes_repo.delete_mote(conn, mote_id)

    note = ann.get_annotation(conn, aid)
    assert note is not None
    assert note.mote_id == mote_id
    assert note.anchor_mode == "mote"


def test_initialize_database_is_idempotent_for_annotations(conn):
    db.initialize_database(conn)
    db.initialize_database(conn)
    assert ann.list_annotations(conn) == []


def test_create_group_stores_members_title_and_size(conn):
    gid = ann.create_group(conn, x=10, y=20, width=200, height=100, members=["a", "b", "a"], title="Core")
    group = ann.get_annotation(conn, gid)

    assert group.type == ann.GROUP
    assert group.anchor_mode == "world"
    assert (group.x, group.y, group.width, group.height) == (10, 20, 200, 100)
    assert group.text == "Core"
    assert group.data == {"members": ["a", "b"], "slot": 0}


def test_group_color_slots_keep_climbing_past_deleted_groups(conn):
    first = ann.create_group(conn, x=0, y=0, width=1, height=1, members=[])
    second = ann.create_group(conn, x=0, y=0, width=1, height=1, members=[])
    ann.delete_annotation(conn, first)
    third = ann.create_group(conn, x=0, y=0, width=1, height=1, members=[])
    assert ann.get_annotation(conn, second).data["slot"] == 1
    assert ann.get_annotation(conn, third).data["slot"] == 2


def test_create_group_rejects_empty_size(conn):
    with pytest.raises(ValueError):
        ann.create_group(conn, x=0, y=0, width=0, height=10, members=[])


def test_create_stroke_stores_relative_points_and_width(conn):
    sid = ann.create_stroke(conn, x=5, y=6, points=[[0, 0], [1.5, 2]], width=3, color="#ff0000")
    stroke = ann.get_annotation(conn, sid)

    assert stroke.type == ann.STROKE
    assert (stroke.x, stroke.y, stroke.color) == (5, 6, "#ff0000")
    assert stroke.data == {"points": [[0.0, 0.0], [1.5, 2.0]], "width": 3}


def test_create_stroke_rejects_empty_points_or_width(conn):
    with pytest.raises(ValueError):
        ann.create_stroke(conn, x=0, y=0, points=[], width=2)
    with pytest.raises(ValueError):
        ann.create_stroke(conn, x=0, y=0, points=[[0, 0]], width=0)


def test_group_of_finds_the_group_owning_a_mote(conn):
    gid = ann.create_group(conn, x=0, y=0, width=10, height=10, members=["m1"])
    assert ann.group_of(conn, "m1").id == gid
    assert ann.group_of(conn, "m2") is None

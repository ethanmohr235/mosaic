import pytest

from core.db import get_connection
from core.gardens import create_garden, garden_exists
from core.library import LIBRARY_GARDEN, reuse_from_library, save_to_library
from core.motes import create_mote, get_mote, update_mote


def test_save_to_library_creates_independent_copy_with_same_properties(conn):
    mote_id = create_mote(conn, {"name": "Standing Meeting", "duration": 30})

    entry_id = save_to_library(conn, mote_id)
    assert entry_id != mote_id

    library_conn = get_connection(LIBRARY_GARDEN)
    entry = get_mote(library_conn, entry_id)
    library_conn.close()

    assert entry.properties == {"name": "Standing Meeting", "duration": 30}


def test_save_to_library_rejects_missing_mote(conn):
    with pytest.raises(LookupError):
        save_to_library(conn, "no-such-id")


def test_save_to_library_lazily_creates_the_library_garden(conn):
    assert not garden_exists(LIBRARY_GARDEN)

    mote_id = create_mote(conn, {"name": "Task"})
    save_to_library(conn, mote_id)

    assert garden_exists(LIBRARY_GARDEN)


def test_editing_the_source_after_saving_does_not_affect_the_library_entry(conn):
    mote_id = create_mote(conn, {"name": "Task", "priority": "low"})
    entry_id = save_to_library(conn, mote_id)

    update_mote(conn, mote_id, {"priority": "high"})

    library_conn = get_connection(LIBRARY_GARDEN)
    entry = get_mote(library_conn, entry_id)
    library_conn.close()

    assert entry.properties["priority"] == "low"


def test_reuse_from_library_creates_new_mote_with_copied_properties(conn):
    mote_id = create_mote(conn, {"name": "Task", "priority": "low"})
    entry_id = save_to_library(conn, mote_id)

    new_id = reuse_from_library(conn, entry_id)
    assert new_id != mote_id
    assert new_id != entry_id
    assert get_mote(conn, new_id).properties == {"name": "Task", "priority": "low"}


def test_reuse_from_library_applies_overrides(conn):
    mote_id = create_mote(conn, {"name": "Task", "priority": "low"})
    entry_id = save_to_library(conn, mote_id)

    new_id = reuse_from_library(conn, entry_id, {"priority": "high"})
    assert get_mote(conn, new_id).properties == {"name": "Task", "priority": "high"}


def test_reuse_from_library_rejects_missing_entry(conn):
    with pytest.raises(LookupError):
        reuse_from_library(conn, "no-such-id")


def test_editing_the_reused_copy_does_not_affect_the_library_entry(conn):
    mote_id = create_mote(conn, {"name": "Task", "priority": "low"})
    entry_id = save_to_library(conn, mote_id)

    new_id = reuse_from_library(conn, entry_id)
    update_mote(conn, new_id, {"priority": "high"})

    library_conn = get_connection(LIBRARY_GARDEN)
    entry = get_mote(library_conn, entry_id)
    library_conn.close()

    assert entry.properties["priority"] == "low"


def test_save_then_reuse_round_trips_across_two_different_gardens(conn):
    mote_id = create_mote(conn, {"name": "Standing Meeting", "duration": 30})
    entry_id = save_to_library(conn, mote_id)

    create_garden("other")
    other_conn = get_connection("other")
    try:
        new_id = reuse_from_library(other_conn, entry_id)
        assert get_mote(other_conn, new_id).properties == {
            "name": "Standing Meeting", "duration": 30
        }
    finally:
        other_conn.close()

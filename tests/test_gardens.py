import pytest

from core.db import garden_db_path
from core.gardens import (
    clear_sync_dir,
    create_garden,
    delete_garden,
    garden_exists,
    get_auto_sync_enabled,
    get_default_garden,
    get_sync_dir,
    list_gardens,
    rename_garden,
    resolve_garden,
    restore_garden,
    set_auto_sync_enabled,
    set_default_garden,
    set_sync_dir,
)


def test_create_garden_creates_initialized_db():
    create_garden("work")
    assert garden_exists("work")


def test_create_garden_duplicate_raises():
    create_garden("work")
    with pytest.raises(FileExistsError):
        create_garden("work")


def test_list_gardens_empty_when_none_exist():
    assert list_gardens() == []


def test_list_gardens_returns_sorted_names():
    create_garden("work")
    create_garden("default")
    assert list_gardens() == ["default", "work"]


def test_resolve_garden_none_autocreates_default():
    assert not garden_exists("default")
    name = resolve_garden(None)
    assert name == "default"
    assert garden_exists("default")


def test_resolve_garden_missing_raises():
    with pytest.raises(LookupError):
        resolve_garden("missing")


def test_resolve_garden_existing_returns_name():
    create_garden("work")
    assert resolve_garden("work") == "work"


def test_delete_garden_removes_it():
    create_garden("work")
    delete_garden("work")
    assert not garden_exists("work")
    assert list_gardens() == []


def test_delete_garden_missing_raises():
    with pytest.raises(LookupError):
        delete_garden("missing")


def test_delete_garden_leaves_a_trash_file():
    create_garden("work")
    delete_garden("work")
    assert garden_db_path("work").with_name("work.db.trash").exists()


def test_restore_garden_brings_it_back():
    create_garden("work")
    delete_garden("work")
    restore_garden("work")
    assert garden_exists("work")
    assert list_gardens() == ["work"]
    assert not garden_db_path("work").with_name("work.db.trash").exists()


def test_restore_garden_missing_raises():
    with pytest.raises(LookupError):
        restore_garden("never-deleted")


def test_restore_garden_onto_existing_name_raises():
    create_garden("work")
    delete_garden("work")
    create_garden("work")
    with pytest.raises(FileExistsError):
        restore_garden("work")


def test_get_default_garden_none_when_unset():
    assert get_default_garden() is None


def test_set_default_garden_missing_raises():
    with pytest.raises(LookupError):
        set_default_garden("missing")


def test_set_default_garden_is_read_back():
    create_garden("work")
    set_default_garden("work")
    assert get_default_garden() == "work"


def test_resolve_garden_none_uses_preferred_default():
    create_garden("work")
    set_default_garden("work")
    assert resolve_garden(None) == "work"
    assert not garden_exists("default")


def test_resolve_garden_none_falls_back_when_preferred_deleted():
    create_garden("work")
    set_default_garden("work")
    delete_garden("work")
    assert resolve_garden(None) == "default"
    assert garden_exists("default")


# ---- sync-dir pointer survives delete/restore, but not a fresh re-create ----
#
# get_sync_dir/set_sync_dir (core/gardens.py) point a garden at a sync
# server for core/sync.py -- just an opaque string here, since this layer
# doesn't care what it contains. Deleting a garden trashes its .db by
# renaming it aside; the sync-dir pointer must travel with it, otherwise a
# later, unrelated garden created under the same name would silently
# inherit it and start pushing/pulling through a sync server that has
# nothing to do with it.

def test_delete_garden_trashes_the_sync_dir_pointer_too():
    create_garden("work")
    set_sync_dir("work", "http://token@host/work")
    delete_garden("work")
    assert get_sync_dir("work") is None


def test_restore_garden_brings_the_sync_dir_pointer_back():
    create_garden("work")
    set_sync_dir("work", "http://token@host/work")
    delete_garden("work")
    restore_garden("work")
    assert get_sync_dir("work") == "http://token@host/work"


def test_a_fresh_garden_does_not_inherit_a_deleted_namesakes_sync_dir():
    create_garden("work")
    set_sync_dir("work", "http://token@host/work")
    delete_garden("work")
    create_garden("work")
    assert get_sync_dir("work") is None


def test_clear_sync_dir_removes_it():
    create_garden("work")
    set_sync_dir("work", "http://token@host/work")
    clear_sync_dir("work")
    assert get_sync_dir("work") is None


def test_clear_sync_dir_on_a_garden_without_one_is_a_noop():
    create_garden("work")
    clear_sync_dir("work")
    assert get_sync_dir("work") is None


# ---- auto-sync flag: gates the console's lifecycle sync points, mirrors the
# sync-dir pointer's own delete/restore behavior ----

def test_get_auto_sync_enabled_defaults_true_when_unset():
    create_garden("work")
    assert get_auto_sync_enabled("work") is True


def test_set_auto_sync_enabled_round_trips():
    create_garden("work")
    set_auto_sync_enabled("work", False)
    assert get_auto_sync_enabled("work") is False
    set_auto_sync_enabled("work", True)
    assert get_auto_sync_enabled("work") is True


def test_delete_garden_trashes_the_auto_sync_pointer_too():
    create_garden("work")
    set_auto_sync_enabled("work", False)
    delete_garden("work")
    assert get_auto_sync_enabled("work") is True  # back to absent-file default


def test_restore_garden_brings_the_auto_sync_pointer_back():
    create_garden("work")
    set_auto_sync_enabled("work", False)
    delete_garden("work")
    restore_garden("work")
    assert get_auto_sync_enabled("work") is False


def test_a_fresh_garden_does_not_inherit_a_deleted_namesakes_auto_sync_setting():
    create_garden("work")
    set_auto_sync_enabled("work", False)
    delete_garden("work")
    create_garden("work")
    assert get_auto_sync_enabled("work") is True


# ---- rename ----

def test_rename_garden_moves_the_database():
    create_garden("work")
    rename_garden("work", "life")
    assert not garden_exists("work")
    assert garden_exists("life")
    assert list_gardens() == ["life"]


def test_rename_garden_carries_the_auto_sync_pointer():
    # The sidecars are named after the garden, so a rename that moved
    # only the .db would silently reset this to its absent-file default.
    create_garden("work")
    set_auto_sync_enabled("work", False)
    rename_garden("work", "life")
    assert get_auto_sync_enabled("life") is False


def test_rename_garden_follows_the_default_garden_pointer():
    # The pointer holds a name, not a path -- left alone it would aim at
    # a garden that no longer exists, which resolve_garden() reads as
    # "never chose one" and answers by auto-creating an empty default.
    create_garden("work")
    set_default_garden("work")
    rename_garden("work", "life")
    assert get_default_garden() == "life"
    assert resolve_garden(None) == "life"


def test_rename_garden_leaves_an_unrelated_default_pointer_alone():
    create_garden("work")
    create_garden("other")
    set_default_garden("other")
    rename_garden("work", "life")
    assert get_default_garden() == "other"


def test_rename_garden_refuses_a_name_already_taken():
    create_garden("work")
    create_garden("life")
    with pytest.raises(FileExistsError):
        rename_garden("work", "life")
    assert garden_exists("work")


def test_rename_garden_refuses_an_invalid_name():
    create_garden("work")
    with pytest.raises(ValueError):
        rename_garden("work", "not a valid name")
    assert garden_exists("work")


def test_rename_garden_missing_raises():
    with pytest.raises(LookupError):
        rename_garden("nothing", "something")


def test_rename_garden_refuses_while_the_garden_syncs(tmp_path):
    # The sync server keys a garden's ops by its name, so renaming here
    # would point this device at an empty op stream while the other
    # devices kept writing to the old one.
    create_garden("work")
    set_sync_dir("work", str(tmp_path))
    with pytest.raises(ValueError, match="syncing"):
        rename_garden("work", "life")
    assert garden_exists("work")

    clear_sync_dir("work")
    rename_garden("work", "life")
    assert garden_exists("life")

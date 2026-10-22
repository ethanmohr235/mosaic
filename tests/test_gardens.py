import pytest

from core.db import garden_db_path
from core.gardens import (
    create_garden,
    delete_garden,
    ensure_garden_id,
    garden_exists,
    garden_id,
    get_default_garden,
    hold,
    is_held,
    is_local_only,
    list_gardens,
    rename_garden,
    resolve_garden,
    restore_garden,
    set_default_garden,
    set_local_only,
    trashed_garden_id,
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


# ---- sync identity: inside the .db, so it follows the file ----

def test_a_garden_id_is_made_once_and_follows_rename_trash_and_restore():
    create_garden("work")
    assert garden_id("work") is None
    made = ensure_garden_id("work")
    assert ensure_garden_id("work") == made
    rename_garden("work", "life")
    assert garden_id("life") == made
    delete_garden("life")
    assert trashed_garden_id("life") == made
    restore_garden("life")
    assert garden_id("life") == made


def test_a_fresh_garden_does_not_inherit_a_trashed_namesakes_id():
    create_garden("work")
    ensure_garden_id("work")
    delete_garden("work")
    create_garden("work")
    assert garden_id("work") is None


def test_local_only_round_trips():
    create_garden("work")
    assert is_local_only("work") is False
    set_local_only("work", True)
    assert is_local_only("work") is True
    set_local_only("work", False)
    assert is_local_only("work") is False


def test_a_held_garden_says_so_until_released():
    create_garden("work")
    assert is_held("work") is False
    handle = hold("work")
    assert is_held("work") is True
    handle.close()
    assert is_held("work") is False


# ---- rename ----

def test_rename_garden_moves_the_database():
    create_garden("work")
    rename_garden("work", "life")
    assert not garden_exists("work")
    assert garden_exists("life")
    assert list_gardens() == ["life"]


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

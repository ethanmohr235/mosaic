import pytest

from core.db import garden_db_path
from core.gardens import (
    create_garden,
    delete_garden,
    garden_exists,
    list_gardens,
    resolve_garden,
    restore_garden,
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

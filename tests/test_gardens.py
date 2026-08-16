import pytest

from core.db import garden_db_path, get_connection, initialize_database
from core.gardens import (
    clear_default_garden,
    create_garden,
    delete_garden,
    garden_exists,
    get_default_garden,
    list_gardens,
    resolve_garden,
    restore_garden,
    set_default_garden,
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


def test_clear_default_garden_reverts_to_autocreate():
    create_garden("work")
    set_default_garden("work")
    clear_default_garden()
    assert get_default_garden() is None
    assert resolve_garden(None) == "default"


def test_resolve_garden_none_falls_back_when_preferred_deleted():
    create_garden("work")
    set_default_garden("work")
    delete_garden("work")
    assert resolve_garden(None) == "default"
    assert garden_exists("default")


# ---- reserved gardens ----
#
# `_library` is MonOSS's own storage rather than a workspace. The rule is
# core's, so every surface agrees: the UI used to filter its switcher
# while `garden list` and the MCP tool both offered `_library` as an
# ordinary garden.

def test_create_garden_rejects_a_reserved_name():
    with pytest.raises(ValueError, match="reserved"):
        create_garden("_library")
    assert not garden_exists("_library")


def test_create_garden_rejects_any_underscore_prefixed_name():
    # A rule, not a list of special cases to keep in sync.
    with pytest.raises(ValueError, match="reserved"):
        create_garden("_scratch")


def test_list_gardens_hides_reserved_gardens():
    create_garden("work")
    library_conn = get_connection("_library")
    initialize_database(library_conn)
    library_conn.close()

    assert list_gardens() == ["work"]


def test_list_gardens_can_include_reserved_gardens_when_asked():
    create_garden("work")
    library_conn = get_connection("_library")
    initialize_database(library_conn)
    library_conn.close()

    assert list_gardens(include_reserved=True) == ["_library", "work"]


def test_a_reserved_garden_is_still_openable_by_name():
    # Reserved governs discovery and creation, not access -- the library
    # is a real, fully editable garden reachable via "Open library".
    library_conn = get_connection("_library")
    initialize_database(library_conn)
    library_conn.close()

    assert garden_exists("_library")
    assert resolve_garden("_library") == "_library"

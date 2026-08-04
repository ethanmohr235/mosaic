import pytest

import core.db as db
from core.gardens import create_garden


def test_initialize_database_creates_expected_tables():
    create_garden("test")
    conn = db.get_connection("test")
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = {row["name"] for row in cur.fetchall()}
    conn.close()

    assert {"motes", "mote_properties", "relationships"} <= tables


def test_initialize_database_creates_expected_indexes():
    create_garden("test")
    conn = db.get_connection("test")
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='index'")
    indexes = {row["name"] for row in cur.fetchall()}
    conn.close()

    assert {
        "idx_relationships_from",
        "idx_relationships_to",
        "idx_relationships_type",
    } <= indexes


@pytest.mark.parametrize("name", ["default", "work", "my-garden_2"])
def test_validate_garden_name_accepts_valid_names(name):
    db.validate_garden_name(name)  # should not raise


@pytest.mark.parametrize("name", ["", "../escape", "has space", "a/b", "x" * 65])
def test_validate_garden_name_rejects_invalid_names(name):
    with pytest.raises(ValueError):
        db.validate_garden_name(name)


def test_generate_uuid_is_unique():
    values = {db.generate_uuid() for _ in range(100)}
    assert len(values) == 100

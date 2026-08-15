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


def test_initialize_database_migrates_a_pre_run_number_garden():
    # Simulates a garden created before simulation_runs.tick was renamed
    # to run_number and committed/step were added -- initialize_database
    # must bring it up to the current shape without losing data.
    create_garden("test")
    conn = db.get_connection("test")
    cur = conn.cursor()
    cur.executescript("""
        DROP TABLE simulation_changes;
        DROP TABLE simulation_runs;
        CREATE TABLE simulation_runs (
            id TEXT PRIMARY KEY, tick INTEGER NOT NULL, ran_at TEXT NOT NULL,
            status TEXT NOT NULL, detail TEXT
        );
        CREATE TABLE simulation_changes (
            run_id TEXT NOT NULL, mote_id TEXT NOT NULL, key TEXT NOT NULL,
            old_value TEXT, new_value TEXT
        );
        INSERT INTO simulation_runs VALUES ('r1', 3, '2026-01-01T00:00:00+00:00', 'ok', NULL);
        INSERT INTO simulation_changes VALUES ('r1', 'm1', 'x', '1.0', '2.0');
    """)
    conn.commit()

    db.initialize_database(conn)  # re-run the migration path

    cur.execute("PRAGMA table_info(simulation_runs)")
    run_columns = {row["name"] for row in cur.fetchall()}
    assert "tick" not in run_columns
    assert {"run_number", "committed"} <= run_columns

    cur.execute("SELECT run_number, committed FROM simulation_runs WHERE id = 'r1'")
    row = cur.fetchone()
    assert row["run_number"] == 3
    assert row["committed"] == 1  # every pre-existing run was a write

    cur.execute("PRAGMA table_info(simulation_changes)")
    change_columns = {row["name"] for row in cur.fetchall()}
    assert "step" in change_columns

    cur.execute("SELECT step, old_value, new_value FROM simulation_changes WHERE run_id = 'r1'")
    row = cur.fetchone()
    assert row["step"] == 0
    assert row["old_value"] == "1.0"
    assert row["new_value"] == "2.0"

    conn.close()

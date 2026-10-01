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


_OLD_RUN_HISTORY = """
    DROP TABLE simulation_derivations;
    DROP TABLE simulation_derivation_equations;
    DROP TABLE simulation_derivation_inputs;
    CREATE TABLE simulation_runs (
        id TEXT PRIMARY KEY, run_number INTEGER NOT NULL, ran_at TEXT NOT NULL,
        status TEXT NOT NULL, detail TEXT, committed INTEGER NOT NULL DEFAULT 1,
        scenario_id TEXT
    );
    CREATE TABLE simulation_changes (
        run_id TEXT NOT NULL, step INTEGER NOT NULL DEFAULT 0, mote_id TEXT NOT NULL,
        key TEXT NOT NULL, old_value TEXT, new_value TEXT
    );
    CREATE INDEX idx_simulation_changes_run ON simulation_changes(run_id);
    CREATE TABLE simulation_derivations (
        run_id TEXT NOT NULL, step INTEGER NOT NULL DEFAULT 0, block INTEGER NOT NULL,
        mote_id TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
        method TEXT NOT NULL, detail TEXT
    );
    CREATE TABLE simulation_derivation_equations (
        run_id TEXT NOT NULL, step INTEGER NOT NULL DEFAULT 0, block INTEGER NOT NULL,
        rule_mote_id TEXT NOT NULL, rule_name TEXT NOT NULL,
        anchor_mote_id TEXT NOT NULL, equation TEXT NOT NULL
    );
    CREATE TABLE simulation_derivation_inputs (
        run_id TEXT NOT NULL, step INTEGER NOT NULL DEFAULT 0, block INTEGER NOT NULL,
        kind TEXT NOT NULL, mote_id TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL
    );
    CREATE INDEX idx_simulation_derivations_lookup
        ON simulation_derivations(run_id, mote_id, key);
    INSERT INTO simulation_runs VALUES ('r1', 3, '2026-01-01T00:00:00+00:00', 'ok', NULL, 1, NULL);
    INSERT INTO simulation_changes VALUES ('r1', 0, 'm1', 'x', '1.0', '2.0');
    INSERT INTO simulation_derivations VALUES ('r1', 0, 0, 'm1', 'x', '2.0', 'substitution', NULL);
    INSERT INTO simulation_derivation_equations VALUES ('r1', 0, 0, 'rule', 'double', 'm1', 'x == 2');
    INSERT INTO simulation_derivation_inputs VALUES ('r1', 0, 0, 'property', 'm1', 'y', '1.0');
"""


def _tables(cur) -> set[str]:
    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    return {row["name"] for row in cur.fetchall()}


def _columns(cur, table: str) -> set[str]:
    cur.execute(f"PRAGMA table_info({table})")
    return {row["name"] for row in cur.fetchall()}


def test_a_fresh_garden_keeps_no_run_history():
    create_garden("test")
    conn = db.get_connection("test")
    cur = conn.cursor()

    assert not {"simulation_runs", "simulation_changes"} & _tables(cur)
    assert "run_id" not in _columns(cur, "simulation_derivations")
    assert "step" not in _columns(cur, "simulation_derivations")
    conn.close()


def test_initialize_database_drops_an_old_run_history_garden():
    # A garden from when every run was logged and derivations were keyed
    # by run and step. The log is dropped rather than converted: `why`
    # explains only the last solve now, and the next solve rebuilds it.
    create_garden("test")
    conn = db.get_connection("test")
    cur = conn.cursor()
    cur.executescript(_OLD_RUN_HISTORY)
    conn.commit()

    db.initialize_database(conn)

    tables = _tables(cur)
    assert not {"simulation_runs", "simulation_changes"} & tables
    for table in (
        "simulation_derivations",
        "simulation_derivation_equations",
        "simulation_derivation_inputs",
    ):
        assert table in tables
        assert not {"run_id", "step"} & _columns(cur, table)
        cur.execute(f"SELECT COUNT(*) AS n FROM {table}")
        assert cur.fetchone()["n"] == 0

    cur.execute("SELECT name FROM sqlite_master WHERE type='index'")
    indexes = {row["name"] for row in cur.fetchall()}
    assert "idx_simulation_changes_run" not in indexes
    assert "idx_simulation_derivations_lookup" in indexes
    conn.close()


def test_reopening_a_migrated_garden_keeps_its_derivations():
    # Idempotent: once the old tables are gone, opening again must not
    # drop the new ones -- they hold the last solve's provenance.
    create_garden("test")
    conn = db.get_connection("test")
    cur = conn.cursor()
    cur.executescript(_OLD_RUN_HISTORY)
    conn.commit()
    db.initialize_database(conn)
    cur.execute(
        "INSERT INTO simulation_derivations VALUES (0, 'm1', 'x', '2.0', 'substitution', NULL)"
    )
    conn.commit()

    db.initialize_database(conn)

    cur.execute("SELECT COUNT(*) AS n FROM simulation_derivations")
    assert cur.fetchone()["n"] == 1
    conn.close()


def _pre_rename_garden(conn):
    """A garden as it stood before `unknowns` became `freedoms`: a
    species declaring it, an instance pointing at that species, and an
    ordinary mote that merely happens to have a property of that name."""
    cur = conn.cursor()
    cur.executescript("""
        INSERT INTO motes (id) VALUES ('species-1'), ('instance-1'), ('plain-1');
        INSERT INTO mote_properties (mote_id, key, value) VALUES ('species-1', 'unknowns', '"voltage"');
        INSERT INTO mote_properties (mote_id, key, value) VALUES ('plain-1', 'unknowns', '"my own data"');
        INSERT INTO relationships (id, from_id, to_id, type) VALUES ('r1', 'instance-1', 'species-1', 'instance_of');
    """)
    conn.commit()
    return cur


def _keys(cur) -> set:
    cur.execute("SELECT mote_id, key FROM mote_properties")
    return {(row["mote_id"], row["key"]) for row in cur.fetchall()}


def test_initialize_database_renames_unknowns_on_a_species_to_freedoms():
    create_garden("test")
    conn = db.get_connection("test")
    cur = _pre_rename_garden(conn)

    db.initialize_database(conn)

    assert ("species-1", "freedoms") in _keys(cur)
    assert ("species-1", "unknowns") not in _keys(cur)
    cur.execute("SELECT value FROM mote_properties WHERE mote_id='species-1'")
    assert cur.fetchone()["value"] == '"voltage"'   # the value is carried over untouched
    conn.close()


def test_the_freedoms_migration_leaves_an_ordinary_property_alone():
    """`unknowns` only ever meant anything on a mote acting as a species.
    Renaming a user's own property of that name would be destroying data
    to tidy up a reserved word."""
    create_garden("test")
    conn = db.get_connection("test")
    cur = _pre_rename_garden(conn)

    db.initialize_database(conn)

    assert ("plain-1", "unknowns") in _keys(cur)
    assert ("plain-1", "freedoms") not in _keys(cur)
    conn.close()


def test_the_freedoms_migration_is_idempotent():
    create_garden("test")
    conn = db.get_connection("test")
    cur = _pre_rename_garden(conn)

    db.initialize_database(conn)
    once = _keys(cur)
    db.initialize_database(conn)

    assert _keys(cur) == once
    conn.close()


def test_the_freedoms_migration_never_clobbers_an_existing_freedoms():
    """A half-migrated garden -- someone set `freedoms` by hand before
    re-opening -- must keep the newer key, not have it overwritten."""
    create_garden("test")
    conn = db.get_connection("test")
    cur = _pre_rename_garden(conn)
    cur.execute("INSERT INTO mote_properties (mote_id, key, value) VALUES ('species-1', 'freedoms', '\"current\"')")
    conn.commit()

    db.initialize_database(conn)

    cur.execute("SELECT value FROM mote_properties WHERE mote_id='species-1' AND key='freedoms'")
    assert cur.fetchone()["value"] == '"current"'
    conn.close()


# ---- the interval migration ----
#
# `date` + `duration` + `ends`/`end` + `recurrence` fold into one ISO 8601
# interval on the start's own key. What earns a test here is the part a
# person would notice going wrong: a span silently changing length, a
# key being renamed out from under a garden, or an unrelated property
# called `duration` being eaten.

def _dated(conn, mote_id, properties):
    """A mote written the pre-interval way, straight into the table --
    before the migration this file is testing has had a chance to run."""
    import json

    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO motes (id) VALUES (?)", (mote_id,))
    for key, value in properties.items():
        cur.execute(
            "INSERT INTO mote_properties (mote_id, key, value) VALUES (?, ?, ?)",
            (mote_id, key, json.dumps(value)),
        )
    conn.commit()
    return cur


def _properties(conn, mote_id):
    import json

    cur = conn.cursor()
    cur.execute("SELECT key, value FROM mote_properties WHERE mote_id = ?", (mote_id,))
    return {row["key"]: json.loads(row["value"]) for row in cur.fetchall()}


def test_a_timed_duration_folds_into_the_starts_own_value():
    create_garden("test")
    conn = db.get_connection("test")
    db.initialize_database(conn)
    _dated(conn, "m1", {"name": "Standup", "date": "2026-09-01 09:00", "duration": 30})

    db.initialize_database(conn)

    assert _properties(conn, "m1") == {"name": "Standup", "date": "2026-09-01 09:00/PT30M"}
    conn.close()


def test_an_all_day_duration_folds_as_days():
    create_garden("test")
    conn = db.get_connection("test")
    db.initialize_database(conn)
    _dated(conn, "m1", {"date": "2026-09-01", "duration": 3})

    db.initialize_database(conn)

    assert _properties(conn, "m1")["date"] == "2026-09-01/P3D"
    conn.close()


def test_an_inclusive_all_day_end_becomes_an_exclusive_interval():
    """"Away Sep 1 to Sep 3" meant the 3rd was an away day. ISO ends are
    exclusive, which is what core/calendar.py always computed anyway."""
    create_garden("test")
    conn = db.get_connection("test")
    db.initialize_database(conn)
    _dated(conn, "m1", {"starts": "2026-09-01", "ends": "2026-09-03"})

    db.initialize_database(conn)

    assert _properties(conn, "m1")["starts"] == "2026-09-01/P3D"
    conn.close()


def test_a_recurrence_folds_in_as_a_tagged_rule_line():
    create_garden("test")
    conn = db.get_connection("test")
    db.initialize_database(conn)
    _dated(conn, "m1", {
        "when": "2026-09-01T09:00", "duration": 45, "recurrence": "FREQ=WEEKLY;BYDAY=TU",
    })

    db.initialize_database(conn)

    assert _properties(conn, "m1")["when"] == \
        "2026-09-01T09:00/PT45M\nRRULE:FREQ=WEEKLY;BYDAY=TU"
    conn.close()


def test_an_exception_line_survives_the_fold():
    create_garden("test")
    conn = db.get_connection("test")
    db.initialize_database(conn)
    _dated(conn, "m1", {
        "date": "2026-09-01T09:00",
        "recurrence": "FREQ=DAILY\nEXDATE:20260903T090000",
    })

    db.initialize_database(conn)

    assert _properties(conn, "m1")["date"] == \
        "2026-09-01T09:00\nRRULE:FREQ=DAILY\nEXDATE:20260903T090000"
    conn.close()


def test_the_gardens_own_key_name_is_kept():
    """A garden's own vocabulary is the whole point of bindings; a
    migration that renamed everything to `date` would undo the thing it
    exists to serve."""
    create_garden("test")
    conn = db.get_connection("test")
    db.initialize_database(conn)
    _dated(conn, "m1", {"due": "2026-09-01 14:00", "duration": 90})

    db.initialize_database(conn)

    properties = _properties(conn, "m1")
    assert properties == {"due": "2026-09-01 14:00/PT1H30M"}
    assert "date" not in properties
    conn.close()


def test_a_declared_span_is_written_even_when_it_equals_the_default():
    """`duration: 60` and a bare start mean the same thing today, but
    only because the default happens to be 60. What the mote said is
    what gets recorded."""
    create_garden("test")
    conn = db.get_connection("test")
    db.initialize_database(conn)
    _dated(conn, "m1", {"date": "2026-09-01T09:00", "duration": 60})

    db.initialize_database(conn)

    assert _properties(conn, "m1")["date"] == "2026-09-01T09:00/PT1H"
    conn.close()


def test_a_duration_with_no_start_beside_it_is_left_alone():
    """A mote with a property called `duration` and no date is describing
    something else entirely -- how long a course runs, how long a part
    lasts -- and must not be touched."""
    create_garden("test")
    conn = db.get_connection("test")
    db.initialize_database(conn)
    _dated(conn, "m1", {"name": "Course", "duration": 14})

    db.initialize_database(conn)

    assert _properties(conn, "m1") == {"name": "Course", "duration": 14}
    conn.close()


def test_the_interval_migration_is_idempotent():
    create_garden("test")
    conn = db.get_connection("test")
    db.initialize_database(conn)
    _dated(conn, "m1", {"date": "2026-09-01 09:00", "duration": 30})

    db.initialize_database(conn)
    once = _properties(conn, "m1")
    db.initialize_database(conn)

    assert _properties(conn, "m1") == once
    conn.close()


def test_a_garden_already_written_in_intervals_is_untouched():
    create_garden("test")
    conn = db.get_connection("test")
    db.initialize_database(conn)
    _dated(conn, "m1", {"date": "2026-09-01T09:00/PT90M"})

    db.initialize_database(conn)

    assert _properties(conn, "m1")["date"] == "2026-09-01T09:00/PT90M"
    conn.close()


def test_initialize_database_drops_annotations():
    # Monarch's sticky notes, groups and strokes went with its window
    # (docs/console.md, U8). They were never synced, so the table goes
    # with nothing to convert; a fresh garden never makes it.
    create_garden("test")
    conn = db.get_connection("test")
    cur = conn.cursor()
    assert "annotations" not in _tables(cur)
    cur.executescript("""
        CREATE TABLE annotations (id TEXT PRIMARY KEY, anchor_mode TEXT NOT NULL,
            x REAL NOT NULL, y REAL NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE INDEX idx_annotations_mote ON annotations(id);
        INSERT INTO annotations VALUES ('a1', 'world', 0, 0, 'now', 'now');
    """)
    conn.commit()

    db.initialize_database(conn)

    assert "annotations" not in _tables(cur)
    cur.execute("SELECT 1 FROM sqlite_master WHERE name = 'idx_annotations_mote'")
    assert cur.fetchone() is None
    conn.close()

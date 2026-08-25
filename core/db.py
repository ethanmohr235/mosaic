import re
import sqlite3
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
GARDENS_DIR = PROJECT_ROOT / "gardens"
DEFAULT_GARDEN = "default"

_GARDEN_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def generate_uuid() -> str:
    return str(uuid.uuid4())


def validate_garden_name(name: str) -> None:
    if not _GARDEN_NAME_RE.match(name):
        raise ValueError(
            f"Invalid garden name: {name!r} "
            "(use 1-64 letters, digits, '_' or '-')"
        )


def is_reserved_garden(name: str) -> bool:
    """A leading underscore marks a garden as MonOSS's own rather than one
    of the user's workspaces -- `_library` (core/library.py) is the only
    one today, and named that way precisely so this is a rule rather than
    a list of special cases to keep in sync.

    Reserved gardens are real gardens: fully readable, writable and
    openable by name. What the rule governs is discovery and creation --
    they don't appear among your workspaces, and you can't create one by
    hand. Kept here beside validate_garden_name() because it is the same
    kind of statement about what a garden name means."""
    return name.startswith("_")


def garden_db_path(name: str) -> Path:
    validate_garden_name(name)
    return GARDENS_DIR / f"{name}.db"


def get_connection(garden: str = DEFAULT_GARDEN) -> sqlite3.Connection:
    GARDENS_DIR.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False: callers that hand this connection across
    # threads (e.g. monarch/api.py, whose methods pywebview invokes from a
    # worker thread) are responsible for serializing their own access.
    conn = sqlite3.connect(garden_db_path(garden), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def initialize_database(conn: sqlite3.Connection) -> None:
    cursor = conn.cursor()

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS motes (
        id TEXT PRIMARY KEY
    );
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS mote_properties (
        mote_id TEXT,
        key TEXT,
        value TEXT,
        PRIMARY KEY (mote_id, key),
        FOREIGN KEY (mote_id) REFERENCES motes(id) ON DELETE CASCADE
    );
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS relationships (
        id TEXT PRIMARY KEY,
        from_id TEXT NOT NULL,
        to_id TEXT NOT NULL,
        type TEXT NOT NULL,
        FOREIGN KEY (from_id) REFERENCES motes(id) ON DELETE CASCADE,
        FOREIGN KEY (to_id) REFERENCES motes(id) ON DELETE CASCADE
    );
    """)

    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_relationships_from ON relationships(from_id);
    """)

    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_relationships_to ON relationships(to_id);
    """)

    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_relationships_type ON relationships(type);
    """)

    # The change log multi-device sync (core/sync.py) is built on. Every
    # mutation in core/motes.py and core/relationships.py appends one row
    # here, in the same transaction as the write it describes -- see
    # core/oplog.py. No FK to motes/relationships: a delete's own op must
    # survive the row it deleted, the same reason simulation_changes below
    # has none either.
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS oplog (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        op_id TEXT UNIQUE NOT NULL,
        device_id TEXT NOT NULL,
        ts TEXT NOT NULL,
        entity_type TEXT NOT NULL,
        entity_id TEXT NOT NULL,
        action TEXT NOT NULL,
        payload TEXT NOT NULL
    );
    """)

    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_oplog_device_seq ON oplog(device_id, seq);
    """)

    # How far this garden has replayed each other device's oplog
    # (core/sync.py::pull), keyed by that device's id. Also holds one row
    # keyed by *this* device's own id, whose last_applied_seq then means
    # "how much of my own oplog I've already pushed out" -- push
    # progress and pull progress are the same shape of bookkeeping, so
    # one table covers both rather than two near-identical ones.
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS sync_state (
        remote_device_id TEXT PRIMARY KEY,
        last_applied_seq INTEGER NOT NULL DEFAULT 0
    );
    """)

    # Simulation audit log. Failed runs are recorded too -- a run that blew
    # up is exactly the one worth looking back at. simulation_changes
    # deliberately has no foreign key on mote_id, so the history of what a
    # run did survives deleting the mote it touched.
    #
    # `run_number` is this garden's own run counter (the Nth run ever
    # logged), unrelated to `step` below -- which tick *within* a run a
    # change belongs to. Both used to be called "tick", which conflated
    # two different things; see the migration below.
    #
    # `committed` distinguishes a run whose final values were actually
    # written to the graph from one only logged for later review (e.g.
    # comparing a few hypothetical parameter sets without any of them
    # becoming canon) -- see core/simulation.py's run_time_series(save=).
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS simulation_runs (
        id TEXT PRIMARY KEY,
        run_number INTEGER NOT NULL,
        ran_at TEXT NOT NULL,
        status TEXT NOT NULL,
        detail TEXT,
        committed INTEGER NOT NULL DEFAULT 1,
        scenario_id TEXT
    );
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS simulation_changes (
        run_id TEXT NOT NULL,
        step INTEGER NOT NULL DEFAULT 0,
        mote_id TEXT NOT NULL,
        key TEXT NOT NULL,
        old_value TEXT,
        new_value TEXT,
        FOREIGN KEY (run_id) REFERENCES simulation_runs(id) ON DELETE CASCADE
    );
    """)

    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_simulation_changes_run ON simulation_changes(run_id);
    """)

    # Provenance: which equations, with which known inputs, determined
    # which values. A `block` is one group solved together -- exactly one
    # equation and one value for a substitution step, N of each for a
    # solver block, which is why both fit one shape. Block numbers are
    # unique within a (run, step) only. See core/provenance.py.
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS simulation_derivations (
        run_id TEXT NOT NULL,
        step INTEGER NOT NULL DEFAULT 0,
        block INTEGER NOT NULL,
        mote_id TEXT NOT NULL,
        key TEXT NOT NULL,
        value TEXT NOT NULL,
        method TEXT NOT NULL,
        detail TEXT,
        FOREIGN KEY (run_id) REFERENCES simulation_runs(id) ON DELETE CASCADE
    );
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS simulation_derivation_equations (
        run_id TEXT NOT NULL,
        step INTEGER NOT NULL DEFAULT 0,
        block INTEGER NOT NULL,
        rule_mote_id TEXT NOT NULL,
        rule_name TEXT NOT NULL,
        anchor_mote_id TEXT NOT NULL,
        equation TEXT NOT NULL,
        FOREIGN KEY (run_id) REFERENCES simulation_runs(id) ON DELETE CASCADE
    );
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS simulation_derivation_inputs (
        run_id TEXT NOT NULL,
        step INTEGER NOT NULL DEFAULT 0,
        block INTEGER NOT NULL,
        kind TEXT NOT NULL,
        mote_id TEXT NOT NULL,
        key TEXT NOT NULL,
        value TEXT NOT NULL,
        FOREIGN KEY (run_id) REFERENCES simulation_runs(id) ON DELETE CASCADE
    );
    """)

    # Explaining a value looks it up by (run, mote, key); expanding an
    # input then looks up that input's own block the same way. Both are
    # covered by this one index.
    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_simulation_derivations_lookup
        ON simulation_derivations(run_id, mote_id, key);
    """)

    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_simulation_derivation_inputs_block
        ON simulation_derivation_inputs(run_id, step, block);
    """)

    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_simulation_derivation_equations_block
        ON simulation_derivation_equations(run_id, step, block);
    """)

    # Migrate a garden created before run_number/committed/step existed.
    # No-ops on both a fresh garden (already created with the new shape
    # above) and one already migrated.
    cursor.execute("PRAGMA table_info(simulation_runs)")
    run_columns = {row["name"] for row in cursor.fetchall()}
    if "tick" in run_columns:
        cursor.execute("ALTER TABLE simulation_runs RENAME COLUMN tick TO run_number")
        run_columns.discard("tick")
        run_columns.add("run_number")
    if "committed" not in run_columns:
        # Every run logged before this column existed was a write -- there
        # was no other kind -- so DEFAULT 1 is the correct backfill.
        cursor.execute("ALTER TABLE simulation_runs ADD COLUMN committed INTEGER NOT NULL DEFAULT 1")

    cursor.execute("PRAGMA table_info(simulation_changes)")
    change_columns = {row["name"] for row in cursor.fetchall()}
    if "step" not in change_columns:
        # Old rows only ever represented one net step, so DEFAULT 0 is
        # correct, not just a placeholder.
        cursor.execute("ALTER TABLE simulation_changes ADD COLUMN step INTEGER NOT NULL DEFAULT 0")

    # A run made against a scenario records which one, so runs over
    # different scenarios can be told apart and compared afterwards.
    # NULL means the garden as it actually stands.
    if "scenario_id" not in run_columns:
        cursor.execute("ALTER TABLE simulation_runs ADD COLUMN scenario_id TEXT")

    # Sync (core/sync.py) needs to tell, for a property or relationship
    # already stored locally, whether an incoming remote edit is newer --
    # last-write-wins per field, not per whole mote. NULL on a row
    # written before this column existed is treated as "very old" by the
    # comparison, which is correct: it predates sync entirely.
    cursor.execute("PRAGMA table_info(mote_properties)")
    prop_columns = {row["name"] for row in cursor.fetchall()}
    if "updated_at" not in prop_columns:
        cursor.execute("ALTER TABLE mote_properties ADD COLUMN updated_at TEXT")

    cursor.execute("PRAGMA table_info(relationships)")
    rel_columns = {row["name"] for row in cursor.fetchall()}
    if "updated_at" not in rel_columns:
        cursor.execute("ALTER TABLE relationships ADD COLUMN updated_at TEXT")

    # `unknowns` was renamed `freedoms` when the declaration moved from
    # the species to the mote: these are the properties a thing is free
    # to have determined, not a solver's list of variables. Scoped to
    # motes acting as a species, which is everywhere the old key ever
    # meant anything -- an ordinary mote with a property a user happened
    # to name `unknowns` is left alone. Idempotent, like the migrations
    # above: a no-op on a fresh garden and on one already migrated.
    cursor.execute("""
    UPDATE mote_properties SET key = 'freedoms'
     WHERE key = 'unknowns'
       AND mote_id IN (SELECT to_id FROM relationships
                        WHERE type IN ('instance_of', 'template_of'))
       AND mote_id NOT IN (SELECT mote_id FROM mote_properties WHERE key = 'freedoms')
    """)

    conn.commit()

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


def garden_db_path(name: str) -> Path:
    validate_garden_name(name)
    return GARDENS_DIR / f"{name}.db"


def get_connection(garden: str = DEFAULT_GARDEN) -> sqlite3.Connection:
    GARDENS_DIR.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False: callers that hand this connection across
    # threads (e.g. ui/api.py, whose methods pywebview invokes from a
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
        committed INTEGER NOT NULL DEFAULT 1
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

    conn.commit()

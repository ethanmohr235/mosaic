import re
import sqlite3
import uuid
from datetime import timedelta
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
    # threads (e.g. console/session.py, whose methods pywebview invokes
    # from a worker thread) are responsible for serializing their own access.
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
    # survive the row it deleted.
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

    # Annotations (sticky notes, group boxes, pen strokes) were Monarch's,
    # and went with its window (docs/console.md, U8). They were local-only
    # -- no oplog row was ever written for one -- so dropping the table is
    # the whole migration. Dropping a table drops its index with it.
    cursor.execute("DROP TABLE IF EXISTS annotations")

    _drop_run_history(cursor)

    # Provenance for the last write-back solve: which equations, with
    # which known inputs, determined which values. Only one solve's worth
    # is ever kept -- core/simulation.py's run_simulation() replaces the
    # whole set in the same transaction as its write-back, so it always
    # describes the values the motes actually hold. A `block` is one group
    # solved together -- exactly one equation and one value for a
    # substitution step, N of each for a solver block, which is why both
    # fit one shape. No FK to motes: the set is replaced wholesale, and an
    # explanation of a since-deleted mote's value is harmless until the
    # next solve drops it. See core/provenance.py and core/derivations.py.
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS simulation_derivations (
        block INTEGER NOT NULL,
        mote_id TEXT NOT NULL,
        key TEXT NOT NULL,
        value TEXT NOT NULL,
        method TEXT NOT NULL,
        detail TEXT
    );
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS simulation_derivation_equations (
        block INTEGER NOT NULL,
        rule_mote_id TEXT NOT NULL,
        rule_name TEXT NOT NULL,
        anchor_mote_id TEXT NOT NULL,
        equation TEXT NOT NULL
    );
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS simulation_derivation_inputs (
        block INTEGER NOT NULL,
        kind TEXT NOT NULL,
        mote_id TEXT NOT NULL,
        key TEXT NOT NULL,
        value TEXT NOT NULL
    );
    """)

    # Explaining a value looks it up by (mote, key); expanding an input
    # then looks up that input's own block the same way.
    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_simulation_derivations_lookup
        ON simulation_derivations(mote_id, key);
    """)

    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_simulation_derivation_inputs_block
        ON simulation_derivation_inputs(block);
    """)

    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_simulation_derivation_equations_block
        ON simulation_derivation_equations(block);
    """)

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
    _migrate_intervals(conn)


# Tables from when a garden kept a log of every simulation run, with
# derivations keyed by run and step. Dropped on open rather than
# converted: provenance now covers only the last write-back solve, and
# the next solve rebuilds it.
_RUN_HISTORY_TABLES = (
    "simulation_changes",
    "simulation_derivation_inputs",
    "simulation_derivation_equations",
    "simulation_derivations",
    "simulation_runs",
)
_DERIVATION_TABLES = _RUN_HISTORY_TABLES[1:4]


def _drop_run_history(cursor: sqlite3.Cursor) -> None:
    """Drop the run-log tables, and derivation tables still keyed by run,
    so initialize_database can create the single-solve shape. A no-op on
    a fresh garden and on one already migrated. Dropping a table drops
    its indexes with it."""
    if not _has_run_history(cursor):
        return
    for table in _RUN_HISTORY_TABLES:
        cursor.execute(f"DROP TABLE IF EXISTS {table}")


def _has_run_history(cursor: sqlite3.Cursor) -> bool:
    cursor.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'simulation_runs'"
    )
    if cursor.fetchone() is not None:
        return True
    for table in _DERIVATION_TABLES:
        cursor.execute(f"PRAGMA table_info({table})")
        if any(row["name"] == "run_id" for row in cursor.fetchall()):
            return True
    return False


# Companion keys an interval absorbs. `duration` and `ends`/`end` say how
# long; `recurrence` says how often. None of the three has a shape of its
# own to be recognized by, which is exactly why they had to be reserved
# names and exactly what folding them into the start's value fixes.
_SPAN_KEYS = ("ends", "end")
_ABSORBED_KEYS = ("duration", "recurrence", "ends", "end")


def _migrate_intervals(conn: sqlite3.Connection) -> None:
    """Fold `duration`/`ends`/`end`/`recurrence` into the value of the
    property that holds the start, as an ISO 8601 interval.

    Idempotent, like every migration above it: a garden with none of the
    companion keys left is untouched, so this is a no-op on a fresh
    garden and on one already migrated.

    **The start's key name is preserved** -- `date` stays `date`, `when`
    stays `when`. A garden's own vocabulary is the whole point of
    bindings, and a migration that renamed everything to one key would
    be undoing the thing it exists to serve.

    Python rather than SQL because values are JSON-encoded in the TEXT
    column and durations need formatting. That costs one pass over the
    motes carrying a companion key, which is bounded by how much of a
    garden is dated at all -- and this runs once, since afterwards there
    are no companion keys left to find.
    """
    # Imported here rather than at module scope: core/db.py is the lowest
    # layer and core/interval.py must not become something every import
    # of it drags in. The cost is one lookup on a migration that no-ops.
    import json

    from core import interval as interval_repo

    cursor = conn.cursor()
    cursor.execute(
        "SELECT DISTINCT mote_id FROM mote_properties WHERE key IN (?, ?, ?, ?)",
        _ABSORBED_KEYS,
    )
    candidates = [row["mote_id"] for row in cursor.fetchall()]
    if not candidates:
        return

    for mote_id in candidates:
        cursor.execute(
            "SELECT key, value FROM mote_properties WHERE mote_id = ?", (mote_id,)
        )
        raw = {}
        for row in cursor.fetchall():
            try:
                raw[row["key"]] = json.loads(row["value"])
            except (json.JSONDecodeError, TypeError):
                raw[row["key"]] = row["value"]

        start_key = _start_key(raw, interval_repo)
        if start_key is None:
            # Companion keys with nothing to attach to. A mote with a
            # property called `duration` and no start is describing
            # something else entirely -- how long a course runs, how long
            # a part lasts -- and must be left exactly alone.
            continue

        folded = _folded_value(raw, start_key, interval_repo)
        if folded is None or folded == raw[start_key]:
            continue
        cursor.execute(
            "UPDATE mote_properties SET value = ? WHERE mote_id = ? AND key = ?",
            (json.dumps(folded), mote_id, start_key),
        )
        cursor.execute(
            "DELETE FROM mote_properties WHERE mote_id = ? AND key IN (?, ?, ?, ?)",
            (mote_id, *_ABSORBED_KEYS),
        )

    conn.commit()


def _start_key(raw: dict, interval_repo) -> str | None:
    """Which property holds this mote's start. `date` wins when present
    because it is the conventional name; otherwise the first date-shaped
    key in insertion order, which is the garden's own answer. `ends`/
    `end` are never a start -- they qualify one.

    A mote dated *two* ways shared one `duration` between them, since the
    old reader took the span off the mote rather than off the binding.
    Only one of them can keep it now, and it is this one; the other
    placement falls back to the default span. That is a real change in a
    rare case, and the honest one -- a span that silently belonged to two
    different events is exactly the ambiguity intervals remove.
    """
    if interval_repo.parse(raw.get("date")) is not None:
        return "date"
    for key, value in raw.items():
        if key in _ABSORBED_KEYS:
            continue
        if interval_repo.parse(value) is not None:
            return key
    return None


def _folded_value(raw: dict, start_key: str, interval_repo) -> str | None:
    """The start's value with its companions folded in, or None if there
    was nothing to fold."""
    original = raw[start_key]
    found = interval_repo.parse(original)
    if found is None:
        return None

    span = None
    for key in _SPAN_KEYS:
        ended = interval_repo.parse_datetime(raw.get(key))
        if ended is None:
            continue
        end, end_all_day = ended
        # An all-day `ends` is *inclusive* as people wrote it -- "away
        # Aug 12 to Aug 14" means the 14th is an away day -- and ISO
        # intervals are exclusive. core/calendar.py already made this
        # conversion on every read; now it is made once, in storage.
        if found.all_day and end_all_day:
            end = end + timedelta(days=1)
        if end > found.start:
            span = end - found.start
        break

    if span is None:
        duration = raw.get("duration")
        if isinstance(duration, (int, float)) and not isinstance(duration, bool) and duration > 0:
            span = timedelta(days=duration) if found.all_day else timedelta(minutes=duration)

    if span is not None:
        found = interval_repo.with_span(found, span)

    rule = raw.get("recurrence")
    if isinstance(rule, str) and rule.strip():
        found = interval_repo.with_rule_lines(found, _rule_lines(rule))

    if not found.rule_lines and span is None:
        return None
    # force_span when the old properties named one: `duration: 60` and a
    # bare start mean the same thing today, but only because the default
    # happens to be 60. What the mote said is what gets recorded.
    return interval_repo.format_like(original, found, force_span=span is not None)


def _rule_lines(raw: str) -> list[str]:
    """A `recurrence` property as tagged RFC 5545 lines. A bare
    `FREQ=...` gets `RRULE:` added, matching what core/calendar.py has
    always done when handing one to dateutil."""
    lines = []
    for line in raw.splitlines():
        text = line.strip()
        if not text:
            continue
        if not text.upper().startswith(("RRULE:", "EXDATE", "RDATE", "EXRULE")):
            text = f"RRULE:{text}"
        lines.append(text)
    return lines

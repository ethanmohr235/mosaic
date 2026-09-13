-- Placeholder single-tenant anchor. Milestone 2 adds owner_user_id (FK to a
-- future users table) and an ACL table keyed off garden name; nothing here
-- should need to change shape when that lands, only gain a column/table.
CREATE TABLE IF NOT EXISTS gardens (
    name TEXT PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One row per op, not one row per batch file, unlike the client's local
-- oplog table and its folder-transport .jsonl batches -- Postgres needs to
-- answer "everything after cursor N, excluding device D" with an indexed
-- range scan, not "read every row of every file and filter in Python".
-- `id` is a global bigserial across all gardens/devices: it is the
-- server-assigned pull cursor, distinct from each device's own local
-- oplog `seq`.
CREATE TABLE IF NOT EXISTS ops (
    id BIGSERIAL PRIMARY KEY,
    garden TEXT NOT NULL REFERENCES gardens(name),
    device_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    op_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    action TEXT NOT NULL,
    payload JSONB NOT NULL,
    -- Captured on every row but NOT consumed by core/sync.py's LWW
    -- comparison in milestone 1 (see core/sync.py module docstring) --
    -- the target side of that comparison (`updated_at`) is still a
    -- client-clock value, so mixing bases now would be worse, not
    -- better. Capturing it here means milestone 2 doesn't need a
    -- backfill migration to get honest server-observed ordering for
    -- ops already pushed under milestone 1.
    server_received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Makes push retry-safe: a retried POST after a dropped connection
    -- re-sends the same rows safely (ON CONFLICT DO NOTHING), which the
    -- old file-write path got for free (re-writing the same filename is
    -- also a no-op) but an HTTP POST does not.
    UNIQUE (garden, device_id, seq)
);

-- Serves pull()'s "WHERE garden = $1 AND id > $2" range scan (id is
-- already globally ordered, so this lets Postgres seek straight to the
-- garden's slice instead of scanning every garden's ops).
CREATE INDEX IF NOT EXISTS idx_ops_garden_id ON ops (garden, id);

-- Serves push()'s per-device duplicate check.
CREATE INDEX IF NOT EXISTS idx_ops_garden_device_seq ON ops (garden, device_id, seq);

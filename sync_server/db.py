"""Server-side Postgres connection pool and schema. One pool per process,
opened at FastAPI startup and closed at shutdown (see app.py's lifespan).

The schema is a list of numbered migrations, applied in order on every
startup from whatever `schema_version` says: a fresh database and a v1
server that predates `schema_version` take the same path, since the v1
step is all CREATE ... IF NOT EXISTS. Each step runs in its own
transaction under an advisory lock, so two processes starting at once
can't both apply one.
"""
import os

import asyncpg

# v1: the server as first built -- gardens keyed by name, one global
# bigserial `ops.id` as every client's pull cursor.
_V1 = """
CREATE TABLE IF NOT EXISTS gardens (
    name TEXT PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
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
    server_received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (garden, device_id, seq)
);
"""

# v2: one account, many devices, gardens by id (docs/architecture.md ->
# Sync). Existing gardens get an id, and their ops keep theirs, so every
# client's cursor stays valid. `ops.id` and `gardens.meta_seq` draw from
# one sequence, so /changes orders ops and renames/trashes with one
# cursor. A name is unique among gardens not in the trash.
_V2 = """
CREATE SEQUENCE feed_seq;
SELECT setval('feed_seq', GREATEST((SELECT COALESCE(MAX(id), 0) FROM ops), 1));
ALTER TABLE ops ALTER COLUMN id SET DEFAULT nextval('feed_seq');
ALTER SEQUENCE ops_id_seq OWNED BY NONE;
DROP SEQUENCE ops_id_seq;

ALTER TABLE gardens ADD COLUMN id UUID NOT NULL DEFAULT gen_random_uuid();
ALTER TABLE gardens ADD COLUMN created_by TEXT;
ALTER TABLE gardens ADD COLUMN trashed_at TIMESTAMPTZ;
ALTER TABLE gardens ADD COLUMN meta_seq BIGINT NOT NULL DEFAULT nextval('feed_seq');

ALTER TABLE ops ADD COLUMN garden_id UUID;
UPDATE ops SET garden_id = g.id FROM gardens g WHERE ops.garden = g.name;
ALTER TABLE ops DROP COLUMN garden;
ALTER TABLE ops ALTER COLUMN garden_id SET NOT NULL;

ALTER TABLE gardens DROP CONSTRAINT gardens_pkey;
ALTER TABLE gardens ADD PRIMARY KEY (id);
CREATE UNIQUE INDEX gardens_live_name ON gardens (name) WHERE trashed_at IS NULL;

ALTER TABLE ops ADD FOREIGN KEY (garden_id) REFERENCES gardens(id);
ALTER TABLE ops ADD UNIQUE (garden_id, device_id, seq);
DROP INDEX IF EXISTS idx_ops_garden_id;
DROP INDEX IF EXISTS idx_ops_garden_device_seq;
CREATE INDEX idx_ops_garden_id ON ops (garden_id, id);

CREATE TABLE devices (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    token_hash TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ
);
CREATE UNIQUE INDEX devices_token ON devices (token_hash);
"""

MIGRATIONS = (_V1, _V2)
_LOCK = 4_262_009  # any constant; serializes migrations and feed writes


def database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    return url


async def migrate(conn: asyncpg.Connection, upto: int = len(MIGRATIONS)) -> int:
    """Bring the schema to version `upto` (all of them by default) and
    return the version it is at."""
    await conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
    for target in range(1, upto + 1):
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock($1)", _LOCK)
            current = await conn.fetchval("SELECT MAX(version) FROM schema_version") or 0
            if current >= target:
                continue
            await conn.execute(MIGRATIONS[target - 1])
            await conn.execute("INSERT INTO schema_version (version) VALUES ($1)", target)
    return await conn.fetchval("SELECT MAX(version) FROM schema_version") or 0


async def lock_feed(conn: asyncpg.Connection) -> None:
    """Hold, for this transaction, the right to draw from `feed_seq`.
    Writers then commit in the order their ids were drawn, so a reader
    can never see id N+1 before N and skip N by moving its cursor past."""
    await conn.execute("SELECT pg_advisory_xact_lock($1)", _LOCK)


async def create_pool() -> asyncpg.Pool:
    pool = await asyncpg.create_pool(dsn=database_url())
    async with pool.acquire() as conn:
        await migrate(conn)
    return pool

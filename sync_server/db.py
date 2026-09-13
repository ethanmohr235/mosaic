"""Server-side Postgres connection pool. One pool per process, opened at
FastAPI startup and closed at shutdown (see app.py's lifespan).

schema.sql is applied on every startup -- every statement in it is
CREATE ... IF NOT EXISTS, so this is a no-op on a server that's already
been initialized, same idempotent-migration philosophy as
core/db.py::initialize_database.
"""
import os
from pathlib import Path

import asyncpg

_SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    return url


async def create_pool() -> asyncpg.Pool:
    pool = await asyncpg.create_pool(dsn=database_url())
    async with pool.acquire() as conn:
        await conn.execute(_SCHEMA_PATH.read_text())
    return pool

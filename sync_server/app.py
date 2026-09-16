"""FastAPI sync server -- milestone 1's transport swap for core/sync.py.

Mirrors the semantics of the folder transport it replaces (push writes an
immutable batch, pull reads everything new since last time) but as an
indexed Postgres range query instead of a directory glob -- see
sync_server/schema.sql's `ops` table and the pull() docstring below for
why that matters once a garden has real multi-user history.

Garden creation is implicit on first push (no init endpoint) and
core/sync.py::status() stays purely local, so there is no status
endpoint either -- this server's whole route surface is exactly push,
pull, and a health check.
"""
import json
from contextlib import asynccontextmanager

import asyncpg
from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel

from sync_server.auth import require_token
from sync_server.db import create_pool
from sync_server.names import validate_garden_name

DEFAULT_PAGE_LIMIT = 2000


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pool = await create_pool()
    try:
        yield
    finally:
        await app.state.pool.close()


app = FastAPI(lifespan=lifespan)


def _garden(garden: str) -> str:
    try:
        validate_garden_name(garden)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return garden


class OpIn(BaseModel):
    op_id: str
    seq: int
    ts: str
    entity_type: str
    entity_id: str
    action: str
    payload: dict


class PushBody(BaseModel):
    device_id: str
    ops: list[OpIn]


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/gardens/{garden}/ops", dependencies=[Depends(require_token)])
async def push(garden: str, body: PushBody) -> dict:
    """Append this device's new ops as one batch. Retry-safe: re-posting
    a batch already stored is a no-op per row, via the same (garden,
    device_id, seq) UNIQUE constraint that made a repeated file write a
    no-op under the old folder transport."""
    garden = _garden(garden)
    if not body.ops:
        return {"accepted": 0, "duplicate": 0}

    pool: asyncpg.Pool = app.state.pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO gardens (name) VALUES ($1) ON CONFLICT (name) DO NOTHING",
                garden,
            )
            rows = await conn.fetch(
                """
                INSERT INTO ops (garden, device_id, seq, op_id, ts, entity_type, entity_id, action, payload)
                SELECT $1, $2, u.seq, u.op_id, u.ts, u.entity_type, u.entity_id, u.action, u.payload::jsonb
                FROM unnest($3::int[], $4::text[], $5::text[], $6::text[], $7::text[], $8::text[], $9::text[])
                    AS u(seq, op_id, ts, entity_type, entity_id, action, payload)
                ON CONFLICT (garden, device_id, seq) DO NOTHING
                RETURNING id
                """,
                garden,
                body.device_id,
                [op.seq for op in body.ops],
                [op.op_id for op in body.ops],
                [op.ts for op in body.ops],
                [op.entity_type for op in body.ops],
                [op.entity_id for op in body.ops],
                [op.action for op in body.ops],
                [json.dumps(op.payload) for op in body.ops],
            )
    accepted = len(rows)
    return {"accepted": accepted, "duplicate": len(body.ops) - accepted}


@app.get("/gardens/{garden}/ops", dependencies=[Depends(require_token)])
async def pull(
    garden: str,
    since_id: int = 0,
    exclude_device: str | None = None,
    limit: int = DEFAULT_PAGE_LIMIT,
) -> dict:
    """One page of ops after `since_id`, excluding `exclude_device`'s own
    ops (a device never needs its own history echoed back, same as the
    folder transport's `if op["device_id"] == self_id: continue`).
    `limit` bounds response size regardless of garden age/size -- the
    directory-glob approach it replaces had no equivalent control at
    all. Client pages by re-calling with `since_id = next_since_id`
    until `ops` comes back empty. Also serves core/sync.py::init()'s
    "does remote history exist" check via `limit=1`."""
    garden = _garden(garden)
    pool: asyncpg.Pool = app.state.pool
    async with pool.acquire() as conn:
        if exclude_device is not None:
            rows = await conn.fetch(
                """
                SELECT id, device_id, seq, op_id, ts, entity_type, entity_id, action, payload, server_received_at
                FROM ops
                WHERE garden = $1 AND id > $2 AND device_id != $3
                ORDER BY id
                LIMIT $4
                """,
                garden, since_id, exclude_device, limit,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT id, device_id, seq, op_id, ts, entity_type, entity_id, action, payload, server_received_at
                FROM ops
                WHERE garden = $1 AND id > $2
                ORDER BY id
                LIMIT $3
                """,
                garden, since_id, limit,
            )

    ops = [
        {
            "server_id": row["id"],
            "device_id": row["device_id"],
            "seq": row["seq"],
            "op_id": row["op_id"],
            "ts": row["ts"],
            "entity_type": row["entity_type"],
            "entity_id": row["entity_id"],
            "action": row["action"],
            "payload": json.loads(row["payload"]),
            "server_received_at": row["server_received_at"].isoformat(),
        }
        for row in rows
    ]
    next_since_id = ops[-1]["server_id"] if ops else since_id
    return {"ops": ops, "next_since_id": next_since_id}

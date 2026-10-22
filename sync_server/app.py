"""FastAPI sync server: one person's devices, every garden
(docs/architecture.md -> Sync).

- `POST /devices` pairs a device with the server secret and hands back its
  own token; `GET /devices`, `DELETE /devices/{id}` list and revoke.
- `GET /gardens` lists every garden, in the trash or not;
  `POST /gardens` registers one under the id its first device gave it;
  `PATCH /gardens/{id}` renames, trashes or restores it.
- `POST /gardens/{id}/ops` appends a device's new ops; `GET` pages them.
- `GET /changes` long-polls for gardens with anything new since a cursor.

The server never reads an op's payload: merging is the client's
(core/merge.py). It only stores, orders and hands back.
"""
import json
import uuid
from contextlib import asynccontextmanager

import asyncpg
from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel

from sync_server.auth import hash_token, new_token, require_device, require_secret
from sync_server.db import create_pool, lock_feed
from sync_server.names import validate_garden_name
from sync_server.notify import CHANNEL, Notifier

DEFAULT_PAGE_LIMIT = 2000
MAX_WAIT = 30.0


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pool = await create_pool()
    app.state.notifier = Notifier()
    await app.state.notifier.start(app.state.pool)
    try:
        yield
    finally:
        await app.state.notifier.stop(app.state.pool)
        await app.state.pool.close()


app = FastAPI(lifespan=lifespan)


def _pool(request: Request) -> asyncpg.Pool:
    return request.app.state.pool


def _name(name: str) -> str:
    try:
        validate_garden_name(name)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return name


class PairBody(BaseModel):
    device_id: str
    name: str


class GardenIn(BaseModel):
    id: uuid.UUID
    name: str


class GardenPatch(BaseModel):
    name: str | None = None
    trashed: bool | None = None


class OpIn(BaseModel):
    op_id: str
    seq: int
    ts: str
    entity_type: str
    entity_id: str
    action: str
    payload: dict


class PushBody(BaseModel):
    ops: list[OpIn]


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


# ---- devices ----

@app.post("/devices", dependencies=[Depends(require_secret)])
async def pair(body: PairBody, request: Request) -> dict:
    """Pair (or re-pair) a device: a fresh token replaces any old one."""
    token = new_token()
    async with _pool(request).acquire() as conn:
        await conn.execute(
            "INSERT INTO devices (id, name, token_hash) VALUES ($1, $2, $3) "
            "ON CONFLICT (id) DO UPDATE SET name = excluded.name, token_hash = excluded.token_hash, "
            "revoked_at = NULL",
            body.device_id, body.name, hash_token(token),
        )
    return {"device_id": body.device_id, "token": token}


@app.get("/devices")
async def devices(request: Request, _device: str = Depends(require_device)) -> dict:
    async with _pool(request).acquire() as conn:
        rows = await conn.fetch("SELECT * FROM devices ORDER BY created_at")
    return {"devices": [
        {"id": r["id"], "name": r["name"], "revoked": r["revoked_at"] is not None,
         "created_at": r["created_at"].isoformat(),
         "last_seen_at": r["last_seen_at"].isoformat() if r["last_seen_at"] else None}
        for r in rows
    ]}


@app.delete("/devices/{device_id}")
async def revoke(device_id: str, request: Request, _device: str = Depends(require_device)) -> dict:
    async with _pool(request).acquire() as conn:
        found = await conn.fetchval(
            "UPDATE devices SET revoked_at = now(), token_hash = NULL WHERE id = $1 RETURNING id", device_id
        )
    if found is None:
        raise HTTPException(status_code=404, detail=f"No device {device_id!r}")
    return {"revoked": device_id}


# ---- gardens ----

_GARDEN_SELECT = """
SELECT g.id, g.name, g.trashed_at, g.created_by, d.name AS created_by_name
FROM gardens g LEFT JOIN devices d ON d.id = g.created_by
"""


def _garden_out(row: asyncpg.Record) -> dict:
    return {"id": str(row["id"]), "name": row["name"], "trashed": row["trashed_at"] is not None,
            "created_by": row["created_by"], "created_by_name": row["created_by_name"]}


async def _garden(conn: asyncpg.Connection, garden_id: uuid.UUID) -> asyncpg.Record:
    row = await conn.fetchrow(_GARDEN_SELECT + " WHERE g.id = $1", garden_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"No garden {garden_id}")
    return row


async def _name_taken(conn: asyncpg.Connection, name: str, garden_id: uuid.UUID) -> None:
    other = await conn.fetchrow(
        _GARDEN_SELECT + " WHERE g.name = $1 AND g.trashed_at IS NULL AND g.id <> $2", name, garden_id
    )
    if other is not None:
        raise HTTPException(status_code=409, detail={"message": f"A garden named {name!r} exists",
                                                     "garden": _garden_out(other)})


@app.get("/gardens")
async def list_gardens(request: Request, _device: str = Depends(require_device)) -> dict:
    async with _pool(request).acquire() as conn:
        rows = await conn.fetch(_GARDEN_SELECT + " ORDER BY g.name")
    return {"gardens": [_garden_out(r) for r in rows]}


@app.post("/gardens")
async def register(body: GardenIn, request: Request, device: str = Depends(require_device)) -> dict:
    """Register a garden under the id its device gave it. Registering an
    id the server already has is a no-op that returns it."""
    name = _name(body.name)
    async with _pool(request).acquire() as conn:
        async with conn.transaction():
            await lock_feed(conn)
            existing = await conn.fetchrow(_GARDEN_SELECT + " WHERE g.id = $1", body.id)
            if existing is not None:
                return _garden_out(existing)
            await _name_taken(conn, name, body.id)
            await conn.execute(
                "INSERT INTO gardens (id, name, created_by) VALUES ($1, $2, $3)", body.id, name, device
            )
            await conn.execute(f"NOTIFY {CHANNEL}")
            return _garden_out(await _garden(conn, body.id))


@app.patch("/gardens/{garden_id}")
async def patch_garden(garden_id: uuid.UUID, body: GardenPatch, request: Request,
                       _device: str = Depends(require_device)) -> dict:
    """Rename, trash (`trashed: true`) or restore a garden. A name a live
    garden already has is refused, whether by rename or by restore."""
    async with _pool(request).acquire() as conn:
        async with conn.transaction():
            await lock_feed(conn)
            row = await _garden(conn, garden_id)
            name = _name(body.name) if body.name is not None else row["name"]
            trashed = body.trashed if body.trashed is not None else row["trashed_at"] is not None
            if not trashed:
                await _name_taken(conn, name, garden_id)
            await conn.execute(
                "UPDATE gardens SET name = $2, "
                "trashed_at = CASE WHEN $3 THEN COALESCE(trashed_at, now()) ELSE NULL END, "
                "meta_seq = nextval('feed_seq') WHERE id = $1",
                garden_id, name, trashed,
            )
            await conn.execute(f"NOTIFY {CHANNEL}")
            return _garden_out(await _garden(conn, garden_id))


# ---- ops ----

@app.post("/gardens/{garden_id}/ops")
async def push(garden_id: uuid.UUID, body: PushBody, request: Request,
               device: str = Depends(require_device)) -> dict:
    """Append the calling device's new ops. Retry-safe: a batch already
    stored is a no-op per row, via the (garden_id, device_id, seq) key."""
    if not body.ops:
        return {"accepted": 0, "duplicate": 0}
    async with _pool(request).acquire() as conn:
        async with conn.transaction():
            await lock_feed(conn)
            await _garden(conn, garden_id)
            rows = await conn.fetch(
                """
                INSERT INTO ops (garden_id, device_id, seq, op_id, ts, entity_type, entity_id, action, payload)
                SELECT $1, $2, u.seq, u.op_id, u.ts, u.entity_type, u.entity_id, u.action, u.payload::jsonb
                FROM unnest($3::int[], $4::text[], $5::text[], $6::text[], $7::text[], $8::text[], $9::text[])
                    AS u(seq, op_id, ts, entity_type, entity_id, action, payload)
                ON CONFLICT (garden_id, device_id, seq) DO NOTHING
                RETURNING id
                """,
                garden_id, device,
                [op.seq for op in body.ops], [op.op_id for op in body.ops], [op.ts for op in body.ops],
                [op.entity_type for op in body.ops], [op.entity_id for op in body.ops],
                [op.action for op in body.ops], [json.dumps(op.payload) for op in body.ops],
            )
            if rows:
                await conn.execute(f"NOTIFY {CHANNEL}")
    return {"accepted": len(rows), "duplicate": len(body.ops) - len(rows)}


@app.get("/gardens/{garden_id}/ops")
async def pull(garden_id: uuid.UUID, request: Request, since_id: int = 0, exclude_device: str | None = None,
               limit: int = DEFAULT_PAGE_LIMIT, _device: str = Depends(require_device)) -> dict:
    """One page of ops after `since_id`, optionally leaving out one
    device's (a device that has pushed here needs none of its own back;
    one cloning afresh needs them all). Page by re-calling with
    `since_id = next_since_id` until `ops` comes back empty."""
    async with _pool(request).acquire() as conn:
        await _garden(conn, garden_id)
        rows = await conn.fetch(
            """
            SELECT id, device_id, seq, op_id, ts, entity_type, entity_id, action, payload
            FROM ops
            WHERE garden_id = $1 AND id > $2 AND ($3::text IS NULL OR device_id <> $3)
            ORDER BY id
            LIMIT $4
            """,
            garden_id, since_id, exclude_device, limit,
        )
    ops = [{"server_id": r["id"], "device_id": r["device_id"], "seq": r["seq"], "op_id": r["op_id"],
            "ts": r["ts"], "entity_type": r["entity_type"], "entity_id": r["entity_id"],
            "action": r["action"], "payload": json.loads(r["payload"])} for r in rows]
    return {"ops": ops, "next_since_id": ops[-1]["server_id"] if ops else since_id}


@app.get("/gardens/{garden_id}/head")
async def head(garden_id: uuid.UUID, request: Request, device: str = Depends(require_device)) -> dict:
    """The highest op seq the calling device has stored in this garden, so
    a device cloning a garden it pushed to before numbers its new ops
    after the old ones."""
    async with _pool(request).acquire() as conn:
        await _garden(conn, garden_id)
        seq = await conn.fetchval(
            "SELECT COALESCE(MAX(seq), 0) FROM ops WHERE garden_id = $1 AND device_id = $2", garden_id, device
        )
    return {"seq": seq}


# ---- changes ----

_CHANGES = """
SELECT g.id FROM gardens g
WHERE g.meta_seq > $1
   OR EXISTS (SELECT 1 FROM ops o WHERE o.garden_id = g.id AND o.id > $1 AND o.device_id <> $2)
"""


async def _changes(pool: asyncpg.Pool, since: int, device: str) -> tuple[list[str], int]:
    async with pool.acquire() as conn:
        async with conn.transaction():
            # The cursor is read under the feed lock, so nothing can commit
            # an id at or below it after this.
            await lock_feed(conn)
            cursor = await conn.fetchval(
                "SELECT GREATEST((SELECT COALESCE(MAX(id), 0) FROM ops), "
                "(SELECT COALESCE(MAX(meta_seq), 0) FROM gardens))"
            )
            ids = [str(r["id"]) for r in await conn.fetch(_CHANGES, since, device)]
    return ids, max(cursor, since)


@app.get("/changes")
async def changes(request: Request, since: int = 0, wait: float = 0,
                  device: str = Depends(require_device)) -> dict:
    """The ids of gardens renamed, trashed, restored or registered since
    `since`, or holding another device's ops newer than it, plus the
    cursor to pass next time. With `wait`, holds the request open up to
    that many seconds (at most 30) until there is something to say."""
    notifier: Notifier = request.app.state.notifier
    generation = notifier.generation
    ids, cursor = await _changes(_pool(request), since, device)
    if not ids and wait > 0:
        await notifier.wait(generation, min(wait, MAX_WAIT))
        ids, cursor = await _changes(_pool(request), since, device)
    return {"gardens": ids, "cursor": cursor}

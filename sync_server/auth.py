"""Who may talk to the server (docs/architecture.md -> Sync).

One person, many devices. `MONOSS_SYNC_TOKEN` is the account's secret: it
does one thing, pair a device (`POST /devices`), which hands that device
a token of its own. Every other route wants a device token, stored here
only as its sha256, so one lost laptop is revoked without re-pairing the
rest. Not mcp_server/auth.py's OAuth layer: this server's only client is
core/sync_remote.py, with no protocol dance to satisfy.
"""
import hashlib
import os
import secrets

import asyncpg
from fastapi import Header, HTTPException, Request


def _expected_secret() -> str:
    secret = os.environ.get("MONOSS_SYNC_TOKEN")
    if not secret:
        raise RuntimeError("MONOSS_SYNC_TOKEN is not set")
    return secret


def _bearer(authorization: str | None) -> str:
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    return authorization.removeprefix("Bearer ")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token() -> str:
    return secrets.token_urlsafe(32)


async def require_secret(authorization: str | None = Header(default=None)) -> None:
    if not secrets.compare_digest(_bearer(authorization), _expected_secret()):
        raise HTTPException(status_code=401, detail="Wrong server secret")


async def require_device(request: Request, authorization: str | None = Header(default=None)) -> str:
    """The id of the device presenting this token."""
    pool: asyncpg.Pool = request.app.state.pool
    async with pool.acquire() as conn:
        device_id = await conn.fetchval(
            "UPDATE devices SET last_seen_at = now() "
            "WHERE token_hash = $1 AND revoked_at IS NULL RETURNING id",
            hash_token(_bearer(authorization)),
        )
    if device_id is None:
        raise HTTPException(status_code=401, detail="Unknown or revoked device token")
    return device_id

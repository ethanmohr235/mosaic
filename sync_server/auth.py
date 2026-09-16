"""One shared bearer token per server instance -- the whole auth surface
for milestone 1. Not mcp_server/auth.py's SingleUserOAuthProvider: that
machinery exists to satisfy MCP clients' OAuth 2.1 dance (dynamic client
registration, authorize/token endpoints) for interactive tools like Claude
Desktop's connector UI. This server's only client is a plain HTTP call
from core/sync_remote.py, so there's no protocol dance to satisfy -- just
a header to check.

Same posture as mcp_server/auth.py documents for its own token: no second
party to protect the resource from yet, so one token standing in for
"whoever holds this has the same access the CLI/GUI have" is honest,
not a shortcut. Real per-user access control is milestone 2.
"""
import os
import secrets

from fastapi import Header, HTTPException


def _expected_token() -> str:
    token = os.environ.get("MONOSS_SYNC_TOKEN")
    if not token:
        raise RuntimeError("MONOSS_SYNC_TOKEN is not set")
    return token


async def require_token(authorization: str | None = Header(default=None)) -> None:
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    presented = authorization.removeprefix("Bearer ")
    if not secrets.compare_digest(presented, _expected_token()):
        raise HTTPException(status_code=401, detail="Invalid bearer token")

"""Thin HTTP client for the sync_server transport (sync_server/app.py) --
the only sync transport core/sync.py speaks. push()/pull()/init() there
call straight through to the functions below wherever they'd write/list/
read `.jsonl` files under an older, since-removed folder transport.

The auth token travels embedded in the sync_dir URL's userinfo
(`https://<token>@host/path`), the same way a git remote carries
credentials -- so core/gardens.py's set_sync_dir/get_sync_dir need no
schema change: a sync_dir is still just one opaque string.
"""
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

_TIMEOUT = 30.0


def _split(sync_dir: str) -> tuple[str, str | None]:
    """(base_url_without_credentials, bearer_token_or_None)."""
    parts = urlsplit(str(sync_dir))
    netloc = parts.hostname or ""
    if parts.port:
        netloc += f":{parts.port}"
    base = urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    return base.rstrip("/"), parts.username


def _client(sync_dir: str | Path) -> httpx.Client:
    sync_dir = str(sync_dir)
    if not sync_dir.startswith(("http://", "https://")):
        raise ValueError(
            f"{sync_dir!r} is not a sync server URL -- point this garden at a "
            "sync server instead (docker compose up, then `sync init "
            '"http://<token>@host:8080/"`)'
        )
    base, token = _split(sync_dir)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx.Client(base_url=base, headers=headers, timeout=_TIMEOUT)


def push(sync_dir: str | Path, garden: str, device_id: str, ops: list[dict[str, Any]]) -> dict[str, Any]:
    with _client(str(sync_dir)) as client:
        resp = client.post(f"/gardens/{garden}/ops", json={"device_id": device_id, "ops": ops})
        resp.raise_for_status()
        return resp.json()


def pull_page(
    sync_dir: str | Path, garden: str, self_device_id: str, since_id: int
) -> tuple[list[dict[str, Any]], int]:
    """One page of ops after `since_id`, excluding our own device's ops.
    Callers page by re-calling with the returned `next_since_id` until
    the returned list is empty."""
    with _client(str(sync_dir)) as client:
        resp = client.get(
            f"/gardens/{garden}/ops",
            params={"since_id": since_id, "exclude_device": self_device_id},
        )
        resp.raise_for_status()
        body = resp.json()
        return body["ops"], body["next_since_id"]


def has_history(sync_dir: str | Path, garden: str) -> bool:
    with _client(str(sync_dir)) as client:
        resp = client.get(f"/gardens/{garden}/ops", params={"limit": 1})
        resp.raise_for_status()
        return bool(resp.json()["ops"])

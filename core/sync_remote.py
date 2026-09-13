"""Thin HTTP client for the sync_server transport (sync_server/app.py),
used by core/sync.py wherever it would otherwise touch a local sync
folder. No merge/conflict logic here -- push()/pull()/init() in
core/sync.py call these functions exactly where they'd write/list/read
`.jsonl` files, and stay unaware of which transport supplied the ops.

The auth token travels embedded in the sync_dir URL's userinfo
(`https://<token>@host/path`), the same way a git remote carries
credentials -- so core/gardens.py's set_sync_dir/get_sync_dir need no
schema change: a remote sync_dir is still just one opaque string.
"""
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

_TIMEOUT = 30.0


def is_remote(sync_dir: str | Path) -> bool:
    return str(sync_dir).startswith(("http://", "https://"))


def _split(sync_dir: str) -> tuple[str, str | None]:
    """(base_url_without_credentials, bearer_token_or_None)."""
    parts = urlsplit(str(sync_dir))
    netloc = parts.hostname or ""
    if parts.port:
        netloc += f":{parts.port}"
    base = urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    return base.rstrip("/"), parts.username


def _client(sync_dir: str) -> httpx.Client:
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

# core/sync_remote.py
"""Thin HTTP client for the sync server (sync_server/app.py) -- the only
transport core/sync.py and core/sync_all.py speak. Everything here is one
request; what to do with the answers is theirs.

A `Remote` is the server's URL and this device's own token, from the
account core/sync_account.py keeps. The token is never in a URL.
"""
from dataclasses import dataclass
from typing import Any

import httpx

_TIMEOUT = 30.0


class SyncError(Exception):
    """The server said no, or couldn't be reached."""


class NameTaken(SyncError):
    """A live garden on the server already has that name: `garden` is it."""

    def __init__(self, garden: dict[str, Any]):
        super().__init__(f"the server already has a garden named {garden['name']!r}")
        self.garden = garden


@dataclass(frozen=True)
class Remote:
    url: str
    token: str

    def client(self, timeout: float = _TIMEOUT) -> httpx.Client:
        return httpx.Client(base_url=self.url.rstrip("/"), timeout=timeout,
                            headers={"Authorization": f"Bearer {self.token}"})


def _check(resp: httpx.Response) -> dict[str, Any]:
    if resp.status_code == 409:
        detail = resp.json().get("detail") if resp.headers.get("content-type", "").startswith("application/json") else None
        if isinstance(detail, dict) and "garden" in detail:
            raise NameTaken(detail["garden"])
    if resp.status_code == 401:
        raise SyncError("the server refused this device's token -- `monoss sync login` again")
    if resp.is_error:
        raise SyncError(f"the server answered {resp.status_code}: {resp.text[:200]}")
    return resp.json()


def _call(remote: Remote, method: str, path: str, *, timeout: float = _TIMEOUT, **kwargs) -> dict[str, Any]:
    try:
        with remote.client(timeout) as client:
            return _check(client.request(method, path, **kwargs))
    except httpx.HTTPError as e:
        raise SyncError(f"can't reach {remote.url}: {e}") from None


def pair(url: str, secret: str, device_id: str, name: str) -> str:
    """This device's own token, given the server's secret."""
    try:
        with httpx.Client(base_url=url.rstrip("/"), timeout=_TIMEOUT) as client:
            resp = client.post("/devices", json={"device_id": device_id, "name": name},
                               headers={"Authorization": f"Bearer {secret}"})
    except httpx.HTTPError as e:
        raise SyncError(f"can't reach {url}: {e}") from None
    if resp.status_code == 401:
        raise SyncError("the server refused that secret")
    return _check(resp)["token"]


def devices(remote: Remote) -> list[dict[str, Any]]:
    return _call(remote, "GET", "/devices")["devices"]


def revoke(remote: Remote, device_id: str) -> None:
    _call(remote, "DELETE", f"/devices/{device_id}")


def list_gardens(remote: Remote) -> list[dict[str, Any]]:
    return _call(remote, "GET", "/gardens")["gardens"]


def register(remote: Remote, garden_id: str, name: str) -> dict[str, Any]:
    """Raises NameTaken when a live garden already has `name`."""
    return _call(remote, "POST", "/gardens", json={"id": garden_id, "name": name})


def patch(remote: Remote, garden_id: str, **fields: Any) -> dict[str, Any]:
    """`name=`, `trashed=`. Raises NameTaken on a live name."""
    return _call(remote, "PATCH", f"/gardens/{garden_id}", json=fields)


def push(remote: Remote, garden_id: str, ops: list[dict[str, Any]]) -> dict[str, Any]:
    return _call(remote, "POST", f"/gardens/{garden_id}/ops", json={"ops": ops})


def pull_page(
    remote: Remote, garden_id: str, since_id: int, exclude_device: str | None
) -> tuple[list[dict[str, Any]], int]:
    """One page of ops after `since_id`. Callers page by re-calling with
    the returned `next_since_id` until the returned list is empty."""
    params: dict[str, Any] = {"since_id": since_id}
    if exclude_device is not None:
        params["exclude_device"] = exclude_device
    body = _call(remote, "GET", f"/gardens/{garden_id}/ops", params=params)
    return body["ops"], body["next_since_id"]


def head(remote: Remote, garden_id: str) -> int:
    """The highest seq this device has stored in the garden."""
    return _call(remote, "GET", f"/gardens/{garden_id}/head")["seq"]


def changes(remote: Remote, since: int, wait: float = 0) -> tuple[list[str], int]:
    """Ids of gardens with anything new since `since`, and the next
    cursor. `wait` holds the request open on the server (a long poll)."""
    body = _call(remote, "GET", "/changes", params={"since": since, "wait": wait}, timeout=wait + _TIMEOUT)
    return body["gardens"], body["cursor"]

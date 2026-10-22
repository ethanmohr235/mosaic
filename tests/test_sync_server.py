"""Route-level tests for sync_server/app.py, run against a real ephemeral
Postgres (testcontainers) rather than a mock -- the JSONB payload
round-trip, the (garden_id, device_id, seq) key that makes push
retry-safe, the shared feed sequence /changes pages on and the v1 -> v2
migration are all things a mock wouldn't actually exercise.

Requires Docker to be available locally (testcontainers spins up a real
postgres:16-alpine container for the whole test session) -- skipped
entirely if Docker isn't reachable.
"""
import asyncio
import threading
import time
import uuid

import pytest

pytest.importorskip("testcontainers")

import asyncpg  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from testcontainers.postgres import PostgresContainer  # noqa: E402

import sync_server.app as app_module  # noqa: E402
from sync_server.db import migrate  # noqa: E402
from tests.conftest import DOCKER_AVAILABLE  # noqa: E402

SECRET = "test-secret"

pytestmark = pytest.mark.skipif(not DOCKER_AVAILABLE, reason="Docker not available for testcontainers")


@pytest.fixture(scope="session")
def postgres_url():
    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg.get_connection_url(driver=None)


@pytest.fixture
def client(postgres_url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", postgres_url)
    monkeypatch.setenv("MONOSS_SYNC_TOKEN", SECRET)
    with TestClient(app_module.app) as c:
        yield c


def _pair(client, name: str = "laptop") -> tuple[str, dict]:
    device_id = str(uuid.uuid4())
    resp = client.post("/devices", json={"device_id": device_id, "name": name},
                       headers={"Authorization": f"Bearer {SECRET}"})
    assert resp.status_code == 200
    return device_id, {"Authorization": f"Bearer {resp.json()['token']}"}


def _register(client, headers, name: str | None = None) -> str:
    garden_id = str(uuid.uuid4())
    resp = client.post("/gardens", json={"id": garden_id, "name": name or f"g{garden_id[:8]}"}, headers=headers)
    assert resp.status_code == 200, resp.text
    return garden_id


def _op(seq: int) -> dict:
    entity_id = str(uuid.uuid4())
    return {"op_id": str(uuid.uuid4()), "seq": seq, "ts": "2026-01-01T00:00:00.000000+00:00",
            "entity_type": "mote", "entity_id": entity_id, "action": "create_mote",
            "payload": {"id": entity_id, "properties": {"n": seq}}}


def _pull(client, headers, garden_id, **params) -> dict:
    resp = client.get(f"/gardens/{garden_id}/ops", params=params, headers=headers)
    assert resp.status_code == 200
    return resp.json()


def test_health_requires_no_auth():
    assert TestClient(app_module.app).get("/health").status_code == 200


def test_pairing_wants_the_server_secret(client):
    resp = client.post("/devices", json={"device_id": "d", "name": "x"}, headers={"Authorization": "Bearer nope"})
    assert resp.status_code == 401


def test_routes_want_a_device_token_not_the_secret(client):
    assert client.get("/gardens").status_code == 401
    assert client.get("/gardens", headers={"Authorization": f"Bearer {SECRET}"}).status_code == 401
    _, headers = _pair(client)
    assert client.get("/gardens", headers=headers).status_code == 200


def test_a_revoked_device_is_refused(client):
    _, headers = _pair(client, "kept")
    lost, lost_headers = _pair(client, "lost")
    assert client.delete(f"/devices/{lost}", headers=headers).status_code == 200
    assert client.get("/gardens", headers=lost_headers).status_code == 401
    listed = {d["id"]: d["revoked"] for d in client.get("/devices", headers=headers).json()["devices"]}
    assert listed[lost] is True


def test_re_pairing_replaces_the_old_token(client):
    device_id, old = _pair(client)
    resp = client.post("/devices", json={"device_id": device_id, "name": "again"},
                       headers={"Authorization": f"Bearer {SECRET}"})
    new = {"Authorization": f"Bearer {resp.json()['token']}"}
    assert client.get("/gardens", headers=old).status_code == 401
    assert client.get("/gardens", headers=new).status_code == 200


def test_register_is_idempotent_and_a_live_name_is_unique(client):
    _, headers = _pair(client, "compass")
    name = f"g{uuid.uuid4().hex[:8]}"
    garden_id = _register(client, headers, name)
    again = client.post("/gardens", json={"id": garden_id, "name": name}, headers=headers)
    assert again.status_code == 200 and again.json()["id"] == garden_id

    clash = client.post("/gardens", json={"id": str(uuid.uuid4()), "name": name}, headers=headers)
    assert clash.status_code == 409
    assert clash.json()["detail"]["garden"]["id"] == garden_id
    assert clash.json()["detail"]["garden"]["created_by_name"] == "compass"


def test_rename_trash_and_restore(client):
    _, headers = _pair(client)
    garden_id = _register(client, headers)
    new_name = f"r{uuid.uuid4().hex[:8]}"
    assert client.patch(f"/gardens/{garden_id}", json={"name": new_name}, headers=headers).json()["name"] == new_name
    assert client.patch(f"/gardens/{garden_id}", json={"trashed": True}, headers=headers).json()["trashed"] is True

    # A trashed garden's name is free; restoring into a taken one is refused.
    taker = _register(client, headers, new_name)
    assert client.patch(f"/gardens/{garden_id}", json={"trashed": False}, headers=headers).status_code == 409
    client.patch(f"/gardens/{taker}", json={"trashed": True}, headers=headers)
    assert client.patch(f"/gardens/{garden_id}", json={"trashed": False}, headers=headers).json()["trashed"] is False


def test_push_then_pull_round_trip_and_retry_safety(client):
    device, headers = _pair(client)
    garden_id = _register(client, headers)
    ops = [_op(1), _op(2)]
    assert client.post(f"/gardens/{garden_id}/ops", json={"ops": ops}, headers=headers).json() == \
        {"accepted": 2, "duplicate": 0}
    assert client.post(f"/gardens/{garden_id}/ops", json={"ops": ops}, headers=headers).json() == \
        {"accepted": 0, "duplicate": 2}

    body = _pull(client, headers, garden_id)
    assert [o["seq"] for o in body["ops"]] == [1, 2]
    assert body["ops"][0]["payload"] == ops[0]["payload"]
    assert {o["device_id"] for o in body["ops"]} == {device}  # from the token, not the body
    assert _pull(client, headers, garden_id, exclude_device=device)["ops"] == []
    assert client.get(f"/gardens/{garden_id}/head", headers=headers).json() == {"seq": 2}


def test_pull_pagination(client):
    _, headers = _pair(client)
    garden_id = _register(client, headers)
    client.post(f"/gardens/{garden_id}/ops", json={"ops": [_op(s) for s in range(1, 6)]}, headers=headers)
    since, pages = 0, []
    while True:
        body = _pull(client, headers, garden_id, since_id=since, limit=2)
        if not body["ops"]:
            break
        pages.append([o["seq"] for o in body["ops"]])
        since = body["next_since_id"]
    assert pages == [[1, 2], [3, 4], [5]]


def test_push_to_an_unknown_garden_is_refused(client):
    _, headers = _pair(client)
    resp = client.post(f"/gardens/{uuid.uuid4()}/ops", json={"ops": [_op(1)]}, headers=headers)
    assert resp.status_code == 404


def test_changes_name_gardens_with_other_devices_ops_or_new_metadata(client):
    _, mine = _pair(client, "mine")
    _, theirs = _pair(client, "theirs")
    cursor = client.get("/changes", headers=mine).json()["cursor"]

    garden_id = _register(client, theirs)
    body = client.get("/changes", params={"since": cursor}, headers=mine).json()
    assert garden_id in body["gardens"]
    cursor = body["cursor"]

    client.post(f"/gardens/{garden_id}/ops", json={"ops": [_op(1)]}, headers=mine)
    # My own ops are not news to me...
    assert garden_id not in client.get("/changes", params={"since": cursor}, headers=mine).json()["gardens"]
    # ...but they are to the other device.
    assert garden_id in client.get("/changes", params={"since": cursor}, headers=theirs).json()["gardens"]


def test_changes_long_poll_wakes_on_a_push(client):
    _, mine = _pair(client, "mine")
    _, theirs = _pair(client, "theirs")
    garden_id = _register(client, theirs)
    cursor = client.get("/changes", headers=mine).json()["cursor"]

    def later():
        time.sleep(0.5)
        client.post(f"/gardens/{garden_id}/ops", json={"ops": [_op(1)]}, headers=theirs)

    threading.Thread(target=later).start()
    started = time.monotonic()
    body = client.get("/changes", params={"since": cursor, "wait": 10}, headers=mine).json()
    assert body["gardens"] == [garden_id]
    assert time.monotonic() - started < 5


def test_v1_history_survives_the_v2_migration(postgres_url):
    async def scenario():
        admin = await asyncpg.connect(postgres_url)
        name = f"m{uuid.uuid4().hex[:8]}"
        await admin.execute(f"CREATE DATABASE {name}")
        await admin.close()
        conn = await asyncpg.connect(postgres_url.rsplit("/", 1)[0] + f"/{name}")
        try:
            assert await migrate(conn, upto=1) == 1
            await conn.execute("INSERT INTO gardens (name) VALUES ('college'), ('lab')")
            for garden, seq in (("college", 1), ("college", 2), ("lab", 1)):
                await conn.execute(
                    "INSERT INTO ops (garden, device_id, seq, op_id, ts, entity_type, entity_id, action, payload) "
                    "VALUES ($1, 'old-laptop', $2, $3, 'ts', 'mote', 'e', 'create_mote', '{}')",
                    garden, seq, str(uuid.uuid4()),
                )
            before = await conn.fetch("SELECT id, garden, seq FROM ops ORDER BY id")

            assert await migrate(conn) == 2
            gardens = {r["name"]: r["id"] for r in await conn.fetch("SELECT id, name FROM gardens")}
            after = await conn.fetch("SELECT id, garden_id, seq FROM ops ORDER BY id")
            assert [(r["id"], r["seq"]) for r in after] == [(r["id"], r["seq"]) for r in before]
            assert [r["garden_id"] for r in after] == [gardens[r["garden"]] for r in before]

            # New ids continue after the old ones: no client cursor is reset.
            fresh = await conn.fetchval(
                "INSERT INTO ops (garden_id, device_id, seq, op_id, ts, entity_type, entity_id, action, payload) "
                "VALUES ($1, 'new', 1, 'x', 'ts', 'mote', 'e', 'create_mote', '{}') RETURNING id",
                gardens["lab"],
            )
            assert fresh > max(r["id"] for r in before)
            assert await migrate(conn) == 2  # idempotent
        finally:
            await conn.close()

    asyncio.run(scenario())

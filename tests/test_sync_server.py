"""Route-level tests for sync_server/app.py, run against a real ephemeral
Postgres (testcontainers) rather than a mock -- the JSONB payload
round-trip, the (garden, device_id, seq) UNIQUE constraint that makes
push retry-safe, and the bigserial id ordering pull() pages on are all
things a mock wouldn't actually exercise.

Requires Docker to be available locally (testcontainers spins up a real
postgres:16-alpine container for the whole test session) -- skipped
entirely if Docker isn't reachable.
"""
import uuid

import pytest

pytest.importorskip("testcontainers")

from fastapi.testclient import TestClient  # noqa: E402
from testcontainers.postgres import PostgresContainer  # noqa: E402

import sync_server.app as app_module  # noqa: E402
from tests.conftest import DOCKER_AVAILABLE  # noqa: E402

TOKEN = "test-token"

pytestmark = pytest.mark.skipif(not DOCKER_AVAILABLE, reason="Docker not available for testcontainers")


@pytest.fixture(scope="session")
def postgres_url():
    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg.get_connection_url(driver=None)


@pytest.fixture
def client(postgres_url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", postgres_url)
    monkeypatch.setenv("MONOSS_SYNC_TOKEN", TOKEN)
    with TestClient(app_module.app) as c:
        c.headers.update({"Authorization": f"Bearer {TOKEN}"})
        yield c


def _garden() -> str:
    # A fresh garden name per test avoids needing to truncate tables
    # between tests -- each test's ops are invisible to every other.
    return f"g{uuid.uuid4().hex[:12]}"


def _op(seq: int, action: str = "create_mote", entity_id: str | None = None, payload: dict | None = None) -> dict:
    entity_id = entity_id or str(uuid.uuid4())
    return {
        "op_id": str(uuid.uuid4()),
        "seq": seq,
        "ts": "2026-01-01T00:00:00+00:00",
        "entity_type": "mote",
        "entity_id": entity_id,
        "action": action,
        "payload": payload or {"id": entity_id, "properties": {}},
    }


def test_health_requires_no_auth():
    # No DB needed -- health doesn't touch the pool dependency at all in
    # this app (it's declared before the lifespan-created pool is used).
    resp = TestClient(app_module.app).get("/health")
    assert resp.status_code == 200


def test_push_requires_auth(client):
    garden = _garden()
    resp = client.post(f"/gardens/{garden}/ops", json={"device_id": "d1", "ops": [_op(1)]},
                        headers={"Authorization": ""})
    assert resp.status_code == 401


def test_push_rejects_wrong_token(client):
    garden = _garden()
    resp = client.post(f"/gardens/{garden}/ops", json={"device_id": "d1", "ops": [_op(1)]},
                        headers={"Authorization": "Bearer wrong"})
    assert resp.status_code == 401


def test_push_rejects_invalid_garden_name(client):
    resp = client.post("/gardens/not valid!/ops", json={"device_id": "d1", "ops": [_op(1)]})
    assert resp.status_code == 422


def test_push_then_pull_round_trip(client):
    garden = _garden()
    ops = [_op(1), _op(2)]
    push_resp = client.post(f"/gardens/{garden}/ops", json={"device_id": "d1", "ops": ops})
    assert push_resp.status_code == 200
    assert push_resp.json() == {"accepted": 2, "duplicate": 0}

    pull_resp = client.get(f"/gardens/{garden}/ops", params={"since_id": 0, "exclude_device": "d2"})
    assert pull_resp.status_code == 200
    body = pull_resp.json()
    assert len(body["ops"]) == 2
    assert [o["seq"] for o in body["ops"]] == [1, 2]
    assert body["ops"][0]["payload"] == ops[0]["payload"]
    assert body["next_since_id"] == body["ops"][-1]["server_id"]


def test_pull_excludes_own_device(client):
    garden = _garden()
    client.post(f"/gardens/{garden}/ops", json={"device_id": "d1", "ops": [_op(1)]})
    client.post(f"/gardens/{garden}/ops", json={"device_id": "d2", "ops": [_op(1)]})

    body = client.get(f"/gardens/{garden}/ops", params={"since_id": 0, "exclude_device": "d1"}).json()
    assert [o["device_id"] for o in body["ops"]] == ["d2"]


def test_push_is_retry_safe(client):
    garden = _garden()
    ops = [_op(1), _op(2)]
    first = client.post(f"/gardens/{garden}/ops", json={"device_id": "d1", "ops": ops}).json()
    assert first == {"accepted": 2, "duplicate": 0}

    retry = client.post(f"/gardens/{garden}/ops", json={"device_id": "d1", "ops": ops}).json()
    assert retry == {"accepted": 0, "duplicate": 2}

    body = client.get(f"/gardens/{garden}/ops", params={"since_id": 0, "exclude_device": "nobody"}).json()
    assert len(body["ops"]) == 2  # not duplicated


def test_pull_pagination(client):
    garden = _garden()
    ops = [_op(seq) for seq in range(1, 6)]
    client.post(f"/gardens/{garden}/ops", json={"device_id": "d1", "ops": ops})

    first_page = client.get(f"/gardens/{garden}/ops",
                             params={"since_id": 0, "exclude_device": "nobody", "limit": 2}).json()
    assert [o["seq"] for o in first_page["ops"]] == [1, 2]

    second_page = client.get(f"/gardens/{garden}/ops",
                              params={"since_id": first_page["next_since_id"], "exclude_device": "nobody",
                                       "limit": 2}).json()
    assert [o["seq"] for o in second_page["ops"]] == [3, 4]

    third_page = client.get(f"/gardens/{garden}/ops",
                             params={"since_id": second_page["next_since_id"], "exclude_device": "nobody",
                                      "limit": 2}).json()
    assert [o["seq"] for o in third_page["ops"]] == [5]

    final_page = client.get(f"/gardens/{garden}/ops",
                             params={"since_id": third_page["next_since_id"], "exclude_device": "nobody",
                                      "limit": 2}).json()
    assert final_page["ops"] == []
    assert final_page["next_since_id"] == third_page["next_since_id"]


def test_pull_limit_one_serves_has_history_check(client):
    garden = _garden()
    empty = client.get(f"/gardens/{garden}/ops", params={"limit": 1}).json()
    assert empty["ops"] == []

    client.post(f"/gardens/{garden}/ops", json={"device_id": "d1", "ops": [_op(1)]})
    nonempty = client.get(f"/gardens/{garden}/ops", params={"limit": 1}).json()
    assert len(nonempty["ops"]) == 1


def test_gardens_are_isolated(client):
    a, b = _garden(), _garden()
    client.post(f"/gardens/{a}/ops", json={"device_id": "d1", "ops": [_op(1)]})

    body = client.get(f"/gardens/{b}/ops", params={"since_id": 0, "exclude_device": "nobody"}).json()
    assert body["ops"] == []

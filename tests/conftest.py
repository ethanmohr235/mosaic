import shutil
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pytest

import core.db as db
from core.gardens import create_garden


@pytest.fixture(autouse=True)
def isolated_gardens_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "GARDENS_DIR", tmp_path / "gardens")


@pytest.fixture
def conn():
    create_garden("test")
    connection = db.get_connection("test")
    yield connection
    connection.close()


# ---- sync_server (sync_server/) test support ----
#
# Exercising the server transport needs a real Postgres, via
# testcontainers, which in turn needs Docker -- tests that need it are
# skipped outright when Docker isn't reachable, rather than falling back
# to a mock that wouldn't prove anything about the real transport.

SYNC_SERVER_TOKEN = "test-token"


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        subprocess.run(["docker", "info"], capture_output=True, timeout=5, check=True)
        return True
    except Exception:
        return False


DOCKER_AVAILABLE = _docker_available()


@contextmanager
def live_sync_server():
    """A real sync_server (sync_server/app.py), served over a real
    localhost socket by uvicorn and backed by a fresh, ephemeral
    testcontainers Postgres -- yields the `http://<token>@host:port`
    base URL to hand straight to core/sync.py as a sync_dir.

    Fresh per call (own container, own port) so two tests never share
    history through reusing the same garden name -- the same isolation
    a fresh tmp_path folder already gives the local-folder transport.
    """
    import os

    import uvicorn
    from testcontainers.postgres import PostgresContainer

    import sync_server.app as app_module

    with PostgresContainer("postgres:16-alpine") as pg:
        prior_db_url = os.environ.get("DATABASE_URL")
        prior_token = os.environ.get("MONOSS_SYNC_TOKEN")
        os.environ["DATABASE_URL"] = pg.get_connection_url(driver=None)
        os.environ["MONOSS_SYNC_TOKEN"] = SYNC_SERVER_TOKEN
        try:
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
            sock.close()

            config = uvicorn.Config(app_module.app, host="127.0.0.1", port=port, log_level="warning")
            server = uvicorn.Server(config)
            thread = threading.Thread(target=server.run, daemon=True)
            thread.start()
            deadline = time.monotonic() + 10
            while not server.started and time.monotonic() < deadline:
                time.sleep(0.05)
            try:
                yield f"http://{SYNC_SERVER_TOKEN}@127.0.0.1:{port}"
            finally:
                server.should_exit = True
                thread.join(timeout=5)
        finally:
            if prior_db_url is None:
                os.environ.pop("DATABASE_URL", None)
            else:
                os.environ["DATABASE_URL"] = prior_db_url
            if prior_token is None:
                os.environ.pop("MONOSS_SYNC_TOKEN", None)
            else:
                os.environ["MONOSS_SYNC_TOKEN"] = prior_token


@pytest.fixture(params=["folder", "server"])
def sync_dir(request, tmp_path):
    """Yields either a local folder path or a live sync-server base URL,
    letting a test written once exercise both transports identically --
    core/sync.py's push()/pull()/init() branch on
    sync_remote.is_remote() but share every merge/conflict/replay code
    path either way, so the same assertions should hold under both."""
    if request.param == "folder":
        yield tmp_path / "shared"
        return
    if not DOCKER_AVAILABLE:
        pytest.skip("Docker not available for testcontainers")
    with live_sync_server() as base_url:
        yield base_url

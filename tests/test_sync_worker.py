"""The sync worker (core/sync_worker.py): one per machine, and a pass
whenever there is something to send or receive. The pass itself is
stubbed here except in the last test, which runs a real worker in its
own process against the live server (skipped without Docker)."""
import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time

import pytest

import core.db as db
from core import sync_account, sync_remote, sync_worker
from core.gardens import create_garden
from core.motes import create_mote
from core.sync_account import Account
from core.sync_all import sync_all
from core.sync_status import Report
from core.sync_worker import CONSOLE, SERVICE, Worker
from tests.conftest import PROJECT_ROOT, SYNC_SERVER_SECRET, wait_until
from tests.sync_fakes import Device


def test_there_is_one_worker_per_machine():
    first = Worker.acquire(SERVICE)
    assert first is not None and sync_worker.running()
    assert Worker.acquire(CONSOLE) is None
    first.stop(final_pass=False)
    assert not sync_worker.running()
    again = Worker.acquire(CONSOLE)
    assert again is not None
    again.stop(final_pass=False)


@pytest.fixture
def passes(monkeypatch):
    """Every pass the worker asks for, as its `full` flag; the server's
    feed says something changed only when `news` is set."""
    calls: list[bool] = []
    news = threading.Event()
    monkeypatch.setattr(sync_account, "load", lambda: Account("http://x", "here", "t"))

    def fake_pass(full=False, worker=None):
        calls.append(full)
        return Report(ok=not getattr(fake_pass, "offline", False))

    def fake_changes(remote, since, wait=0):
        if news.wait(timeout=0.2):
            news.clear()
            return ["g"], since + 1
        return [], since

    monkeypatch.setattr(sync_worker, "sync_all", fake_pass)
    monkeypatch.setattr(sync_remote, "changes", fake_changes)
    return calls, news, fake_pass


def _worker() -> Worker:
    return Worker.acquire(CONSOLE, poll=0.05, debounce=0.05, heartbeat=60, long_poll=0.2).start()


def test_the_first_pass_is_full_and_a_write_wakes_another(passes):
    calls, _, _ = passes
    create_garden("g")
    worker = _worker()
    try:
        assert wait_until(lambda: calls == [True])
        create_mote(db.get_connection("g"), {"name": "typed"})
        assert wait_until(lambda: len(calls) >= 2, timeout=3)
        assert calls[1] is False
    finally:
        worker.stop(final_pass=False)


def test_news_from_the_server_wakes_a_pass(passes):
    calls, news, _ = passes
    worker = _worker()
    try:
        assert wait_until(lambda: len(calls) == 1)
        news.set()
        assert wait_until(lambda: len(calls) >= 2, timeout=3)
    finally:
        worker.stop(final_pass=False)


def test_a_poke_wakes_a_pass_and_offline_backs_off(passes):
    calls, _, fake_pass = passes
    worker = _worker()
    try:
        assert wait_until(lambda: len(calls) == 1)
        fake_pass.offline = True
        worker.poke()
        assert wait_until(lambda: len(calls) == 2)
        time.sleep(1.0)  # backs off 2s after a failure, not straight round again
        assert len(calls) == 2
    finally:
        worker.stop(final_pass=False)


_RUN_WORKER = """
import sys
from pathlib import Path
import core.db as db
db.GARDENS_DIR = Path(sys.argv[1])
import signal
from core.sync_worker import SERVICE, Worker
worker = Worker.acquire(SERVICE, poll=0.1, debounce=0.1, heartbeat=60, long_poll=5)
signal.signal(signal.SIGTERM, lambda *_: worker.request_stop())
worker.run_forever()
"""


def _names_in(path) -> set:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return {json.loads(r[0]) for r in conn.execute("SELECT value FROM mote_properties WHERE key = 'name'")}
    finally:
        conn.close()


def test_a_running_worker_receives_and_sends_on_its_own(sync_server, tmp_path, monkeypatch):
    a, b = Device(tmp_path / "compass", monkeypatch), Device(tmp_path / "cartographer", monkeypatch)
    for machine in (a, b):
        with machine.active():
            sync_account.login(sync_server, SYNC_SERVER_SECRET, machine.root.name)
    with a.active():
        create_garden("hw")
        sync_all(full=True)
    with b.active():
        sync_all(full=True)

    env = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT)}
    process = subprocess.Popen([sys.executable, "-c", _RUN_WORKER, str(a.root)], cwd=PROJECT_ROOT, env=env)
    try:
        with b.active():
            create_mote(db.get_connection("hw"), {"name": "from cartographer"})
            sync_all()
        assert wait_until(lambda: "from cartographer" in _names_in(a.root / "hw.db"), timeout=15)

        with a.active():
            create_mote(db.get_connection("hw"), {"name": "from compass"})
        with b.active():
            assert wait_until(lambda: sync_all() and "from compass" in _names_in(b.root / "hw.db"), timeout=15)
    finally:
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=15) == 0

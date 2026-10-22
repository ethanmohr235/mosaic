"""Two machines and a real sync server, end to end: one account, every
garden (core/sync_all.py). Each "machine" is its own gardens/ directory,
so its own device id and account (tests/sync_fakes.py's Device); the
server is the live one from tests/conftest.py, skipped without Docker.

The merge itself, in every interleaving, is tests/test_sync_merge.py's.
"""
import pytest

import core.db as db
from core import gardens as gardens_repo
from core import sync_account, sync_commands, sync_remote
from core.command_bar import CommandError
from core.gardens import create_garden, delete_garden, garden_exists, rename_garden, restore_garden
from core.motes import create_mote, list_motes
from core.sync_all import sync_all
from core.sync_status import CONFLICT, LOCAL, WAITING
from tests.conftest import SYNC_SERVER_SECRET
from tests.sync_fakes import Device, snapshot


@pytest.fixture
def machines(sync_server, tmp_path, monkeypatch):
    """Two machines, each signed in to the server."""
    pair = (Device(tmp_path / "compass", monkeypatch), Device(tmp_path / "cartographer", monkeypatch))
    for machine in pair:
        with machine.active():
            sync_account.login(sync_server, SYNC_SERVER_SECRET, machine.root.name)
    return pair


def _make(machine: Device, garden: str, *names: str) -> None:
    with machine.active():
        create_garden(garden)
        conn = db.get_connection(garden)
        for name in names:
            create_mote(conn, {"name": name})
        conn.close()


def _pass(machine: Device, full: bool = False):
    with machine.active():
        return sync_all(full=full)


def _names(machine: Device, garden: str) -> set[str]:
    with machine.active():
        conn = db.get_connection(garden)
        try:
            return {m.properties.get("name") for m in list_motes(conn)}
        finally:
            conn.close()


def _has(machine: Device, garden: str) -> bool:
    with machine.active():
        return garden_exists(garden)


def test_a_second_machine_gets_every_garden(machines):
    a, b = machines
    _make(a, "hw", "beam", "load")
    _make(a, "notes", "lecture 1")
    assert _pass(a, full=True).ok
    report = _pass(b, full=True)
    assert report.ok and not report.conflicts
    assert _names(b, "hw") == {"beam", "load"} and _names(b, "notes") == {"lecture 1"}
    with a.active():
        ours = snapshot(db.get_connection("hw"))
    with b.active():
        assert snapshot(db.get_connection("hw")) == ours


def test_edits_reach_the_other_machine_through_the_feed(machines):
    a, b = machines
    _make(a, "hw", "beam")
    _pass(a, full=True)
    _pass(b, full=True)
    with a.active():
        create_mote(db.get_connection("hw"), {"name": "added on compass"})
    _pass(a)
    report = _pass(b)  # not full: only what the feed names is pulled
    assert "hw" in report.received
    assert "added on compass" in _names(b, "hw")


def test_a_rename_reaches_the_other_machine(machines):
    a, b = machines
    _make(a, "hw", "beam")
    _pass(a, full=True)
    _pass(b, full=True)
    with a.active():
        rename_garden("hw", "hw-4")
    _pass(a)
    _pass(b)
    assert _has(b, "hw-4") and not _has(b, "hw")
    assert _names(b, "hw-4") == {"beam"}


def test_remove_and_restore_reach_the_other_machine(machines):
    a, b = machines
    _make(a, "hw", "beam")
    _pass(a, full=True)
    _pass(b, full=True)
    with a.active():
        delete_garden("hw")
    _pass(a)
    _pass(b)
    assert not _has(b, "hw")
    with b.active():
        restore_garden("hw")
    _pass(b)
    _pass(a)
    assert _has(a, "hw") and _names(a, "hw") == {"beam"}


def test_a_name_clash_asks_and_merge_joins_the_two(machines):
    a, b = machines
    _make(a, "scratch", "from compass")
    _make(b, "scratch", "from cartographer")
    _pass(a, full=True)
    report = _pass(b, full=True)
    assert [c.name for c in report.conflicts] == ["scratch"]
    assert report.conflicts[0].question == "compass also has a garden named 'scratch': merge or rename?"
    assert {g.name: g.state for g in report.gardens}["scratch"] == CONFLICT

    with b.active():
        sync_commands.run(sync_commands.parse(["merge", "scratch"]))
    _pass(a)
    both = {"from compass", "from cartographer"}
    assert _names(a, "scratch") == both and _names(b, "scratch") == both


def test_a_name_clash_answered_by_renaming_keeps_both(machines):
    a, b = machines
    _make(a, "scratch", "from compass")
    _make(b, "scratch", "from cartographer")
    _pass(a, full=True)
    _pass(b, full=True)
    with b.active():
        sync_commands.run(sync_commands.parse(["rename", "scratch", "scratch-b"]))
    _pass(a)
    assert _names(b, "scratch") == {"from compass"}
    assert _names(b, "scratch-b") == {"from cartographer"}
    assert _names(a, "scratch-b") == {"from cartographer"}


def test_a_garden_kept_local_stays_home_and_asks_nothing(machines):
    a, b = machines
    _make(a, "testing", "compass's")
    _make(b, "testing", "cartographer's")
    with b.active():
        sync_commands.run(sync_commands.parse(["local", "testing"]))
    _pass(a, full=True)
    report = _pass(b, full=True)
    assert not report.conflicts
    assert {g.name: g.state for g in report.gardens}["testing"] == LOCAL
    assert _names(b, "testing") == {"cartographer's"}


def test_a_garden_open_here_waits_before_moving_aside(machines):
    a, b = machines
    _make(a, "hw", "beam")
    _pass(a, full=True)
    _pass(b, full=True)
    with a.active():
        delete_garden("hw")
    _pass(a)
    with b.active():
        held = gardens_repo.hold("hw")
    report = _pass(b)
    assert {g.name: g.state for g in report.gardens}["hw"] == WAITING and _has(b, "hw")
    held.close()
    _pass(b)
    assert not _has(b, "hw")


def test_a_legacy_garden_is_adopted_not_asked_about(machines):
    a, b = machines
    _make(a, "college", "exam 1")
    _pass(a, full=True)
    _make(b, "college", "homework 3")
    with b.active():
        # Synced the old way: one URL per garden, beside the .db.
        db.garden_db_path("college").with_name("college.db.sync_dir").write_text("http://old@compass:8080/")
    report = _pass(b, full=True)
    assert not report.conflicts
    _pass(a)
    assert _names(a, "college") == {"exam 1", "homework 3"}
    with b.active():
        assert gardens_repo.legacy_sync_url("college") is None


def test_a_machine_that_lost_a_file_clones_it_back_and_keeps_writing(machines):
    a, b = machines
    _make(a, "hw", "beam")
    _pass(a, full=True)
    _pass(b, full=True)
    with a.active():
        db.garden_db_path("hw").unlink()  # gone outside the app, not `garden rm`
    _pass(a)
    assert _names(a, "hw") == {"beam"}
    with a.active():
        create_mote(db.get_connection("hw"), {"name": "after the clone"})
    _pass(a)
    _pass(b)
    # Numbered after its old ops, so the server kept it rather than
    # dropping it as one it already had.
    assert "after the clone" in _names(b, "hw")


def test_a_revoked_machine_is_refused(machines):
    a, b = machines
    with a.active():
        assert "revoked" in sync_commands.run(sync_commands.parse(["revoke", "cartographer"]))
    report = _pass(b)
    assert not report.ok and "login" in report.error
    with b.active(), pytest.raises(CommandError):
        sync_commands.run(sync_commands.parse(["devices"]))


def test_signing_in_again_to_the_same_server_keeps_what_was_agreed(machines, sync_server):
    a, _ = machines
    _make(a, "hw")
    _pass(a, full=True)
    with a.active():
        known = sync_account.load().known
        sync_account.login(sync_server, SYNC_SERVER_SECRET, "compass")
        assert sync_account.load().known == known
        assert sync_remote.list_gardens(sync_account.load().remote())

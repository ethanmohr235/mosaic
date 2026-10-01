"""The Gardens window's bridge (gardens_app/api.py).

Most of this moved here from tests/test_api.py, where it tested the same
verbs inside Monarch -- creating, deleting, restoring, importing and
exporting a garden, and pointing one at a sync server. Two things
changed in the move and are tested as such: delete no longer refuses the
last or the current garden (this window is open on none), and rename is
new.

What is *not* retested here is core/gardens.py itself
(tests/test_gardens.py) -- only what this layer adds: the file dialogs,
the cleanup an import does when it fails, and the shape the frontend
gets back.
"""
import json

import pytest

from core.gardens import (
    create_garden,
    garden_exists,
    get_auto_sync_enabled,
    get_sync_dir,
)
from gardens_app.api import Api
from monarch.api import Api as MonarchApi


class _StubWindow:
    """Minimal stand-in for pywebview's window -- just enough
    create_file_dialog to exercise import/export without a native
    dialog. Returns whatever it was handed; None is a cancelled dialog."""

    def __init__(self, path):
        self._path = path

    def create_file_dialog(self, *args, **kwargs):
        return self._path


@pytest.fixture
def api():
    create_garden("test")
    instance = Api()
    yield instance
    instance.shutdown()


def _fill(garden: str, name: str) -> None:
    """Put one mote in a garden, through an app bridge rather than this
    one -- this window deliberately has no way to write into a garden."""
    app = MonarchApi(garden)
    try:
        app.create_mote({"name": name})
    finally:
        app.shutdown()


# ---- the list ----

def test_initial_carries_the_list_and_the_preselected_garden():
    create_garden("test")
    create_garden("other")
    instance = Api("other", open_sync=True)
    try:
        start = instance.initial()
    finally:
        instance.shutdown()

    assert [g["name"] for g in start["gardens"]] == ["other", "test"]
    assert start["select"] == "other"
    assert start["open_sync"] is True


def test_list_gardens_counts_what_is_in_each_one(api):
    _fill("test", "Alice")
    row = next(g for g in api.list_gardens() if g["name"] == "test")
    assert row["motes"] == 1
    assert row["relationships"] == 0
    assert row["sync_dir"] is None
    assert row["bytes"] > 0


def test_list_gardens_survives_a_file_that_is_not_a_garden(api, monkeypatch):
    # A folder-sync tool can drop a half-written .db in gardens/, and a
    # list that raised on it would be a window that won't open at all.
    import core.db as db
    (db.GARDENS_DIR / "broken.db").write_text("not a database")

    row = next(g for g in api.list_gardens() if g["name"] == "broken")
    assert row["motes"] is None
    assert row["relationships"] is None


# ---- create, rename, delete ----

def test_create_garden_makes_a_usable_garden(api):
    assert api.create_garden("fresh") == {"garden": "fresh"}
    assert garden_exists("fresh")
    _fill("fresh", "Alice")


def test_create_garden_rejects_a_bad_name(api):
    with pytest.raises(ValueError):
        api.create_garden("no spaces allowed")


def test_rename_garden_moves_it_and_keeps_its_contents(api):
    _fill("test", "Alice")
    assert api.rename_garden("test", "renamed") == {"garden": "renamed"}

    assert not garden_exists("test")
    assert garden_exists("renamed")
    row = next(g for g in api.list_gardens() if g["name"] == "renamed")
    assert row["motes"] == 1


def test_rename_garden_refuses_while_it_syncs(api, sync_dir):
    api.enable_sync("test", sync_dir)
    with pytest.raises(ValueError, match="syncing"):
        api.rename_garden("test", "renamed")
    assert garden_exists("test")


def test_delete_garden_allows_the_last_one(api):
    # Monarch's modal refused this because it *was* a garden window and
    # would have been left standing in nothing. This window isn't.
    assert api.list_gardens() == [g for g in api.list_gardens() if g["name"] == "test"]
    assert api.delete_garden("test") == {"deleted": "test"}
    assert api.list_gardens() == []


def test_restore_garden_brings_a_deleted_one_back(api):
    _fill("test", "Alice")
    api.delete_garden("test")
    assert api.restore_garden("test") == {"restored": "test"}

    row = next(g for g in api.list_gardens() if g["name"] == "test")
    assert row["motes"] == 1


def test_restore_garden_missing_raises(api):
    with pytest.raises(LookupError):
        api.restore_garden("never-deleted")


# ---- import and export ----

def test_export_then_import_round_trips_a_garden(api, tmp_path):
    _fill("test", "Original")
    out = tmp_path / "export.json"

    api._window = _StubWindow(str(out))
    assert api.export_garden_to_file("test") == {"path": str(out)}

    api._window = _StubWindow([str(out)])
    result = api.import_garden_into_new("imported")

    assert result == {"garden": "imported", "motes": 1, "relationships": 0}
    imported = MonarchApi("imported")
    try:
        assert [m["properties"]["name"] for m in imported.list_motes()] == ["Original"]
    finally:
        imported.shutdown()


def test_export_cancelled_writes_nothing(api, tmp_path):
    api._window = _StubWindow(None)
    assert api.export_garden_to_file("test") == {"cancelled": True}
    assert list(tmp_path.glob("*.json")) == []


def test_import_cancelled_makes_no_garden(api):
    api._window = _StubWindow(None)
    assert api.import_garden_into_new("imported") == {"cancelled": True}
    assert not garden_exists("imported")


def test_a_failed_import_does_not_leave_the_new_garden_behind(api, tmp_path):
    # Otherwise the retry fails on the name rather than on the file, and
    # the user is left deleting an empty garden they never wanted.
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"format": "something-else", "motes": []}))
    api._window = _StubWindow([str(bad)])

    with pytest.raises(ValueError, match="format"):
        api.import_garden_into_new("imported")

    assert not garden_exists("imported")


# ---- sync, per garden ----

def test_enable_sync_points_one_garden_at_a_server(api, sync_dir):
    create_garden("other")
    api.enable_sync("test", sync_dir)

    assert get_sync_dir("test") == sync_dir
    assert get_sync_dir("other") is None


def test_disable_sync_clears_it(api, sync_dir):
    api.enable_sync("test", sync_dir)
    assert api.disable_sync("test") == {"garden": "test", "sync_dir": None}
    assert get_sync_dir("test") is None


def test_auto_sync_round_trips(api, sync_dir):
    api.enable_sync("test", sync_dir)
    assert api.sync_status("test")["auto_sync"] is True

    api.set_auto_sync("test", False)
    assert get_auto_sync_enabled("test") is False
    assert api.sync_status("test")["auto_sync"] is False


def test_sync_now_pushes_that_garden(api, sync_dir):
    # Written before sync is turned on: an app bridge pushes on shutdown
    # when the garden already has a sync dir (its _best_effort_sync), and
    # there would then be nothing left for this call to prove.
    _fill("test", "Alice")
    api.enable_sync("test", sync_dir)
    result = api.sync_now("test")
    assert result["push"]["pushed"] >= 1


# ---- opening an app ----

def test_open_app_launches_that_app_on_that_garden(api, monkeypatch):
    import gardens_app.api as module

    launched = []
    monkeypatch.setattr(module, "_open_app", lambda *args: launched.append(args) or {"ok": True})

    api.open_app("mora", "test")
    assert launched == [("mora", "test")]

"""Home (core/home.py): the `gardens` tree, one garden's details, the
ownership path, and the `garden` subverbs. The garden-management cases
came over from the Gardens window's bridge tests when that window was
folded into the console (U9)."""
import json

import pytest

from core import home
from core.command_bar import CommandError
from core.db import get_connection
from core.gardens import create_garden, garden_exists, get_auto_sync_enabled, get_sync_dir
from core.motes import create_mote, list_motes


def fill(garden: str, properties: dict) -> str:
    conn = get_connection(garden)
    try:
        return create_mote(conn, properties)
    finally:
        conn.close()


def run(text: str, current=None):
    return home.run(home.parse_text(text), current)


@pytest.fixture
def semester():
    """college owns hw4 (through the mote HW4); scratch is loose; old is
    archived by a marker inside it."""
    for name in ("college", "hw4", "scratch", "old"):
        create_garden(name)
    fill("college", {"name": "HW4", "garden": "hw4", "due": "2026-10-09", "status": "todo"})
    fill("hw4", {"name": "beam"})
    fill("old", {"name": "Archived", "archived": True})


def rows(view, group):
    return [(r["garden"], r["depth"]) for g in view["groups"] if g["key"] == group for r in g["rows"]]


# ---- the tree ----

def test_the_tree_groups_projects_loose_and_folded_archived(semester):
    view = home.gardens_view(current="hw4")
    assert rows(view, home.PROJECTS) == [("college", 0), ("hw4", 1)]
    assert rows(view, home.LOOSE) == [("scratch", 0)]
    archived = next(g for g in view["groups"] if g["key"] == home.ARCHIVED)
    assert archived["rows"] == [] and archived["count"] == 1 and archived["folded"]
    assert rows(home.gardens_view(show_archived=True), home.ARCHIVED) == [("old", 0)]

    hw4 = next(r for g in view["groups"] for r in g["rows"] if r["garden"] == "hw4")
    assert hw4["label"] == "HW4" and hw4["cover"]["due"] == "2026-10-09" and hw4["cover"]["status"] == "todo"
    assert hw4["current"] and hw4["motes"] == 1 and not hw4["syncing"]
    assert view["command"] == "gardens" and view["total"] == 4


def test_a_file_that_is_not_a_garden_still_lists(semester):
    import core.db as db
    (db.GARDENS_DIR / "broken.db").write_text("not a database")
    row = next(r for g in home.gardens_view()["groups"] for r in g["rows"] if r["garden"] == "broken")
    assert row["motes"] is None and row["relationships"] is None


def test_problems_ride_on_their_garden(semester):
    fill("scratch", {"name": "Thief", "garden": "hw4"})
    view = home.gardens_view()
    hw4 = [r for g in view["groups"] for r in g["rows"] if r["garden"] == "hw4"]
    assert hw4 and all("claim" in r["problems"][0] for r in hw4)
    assert view["problems"]


def test_the_ownership_path(semester):
    assert home.path_of("hw4") == ["college", "hw4"]
    assert home.path_of("scratch") == ["scratch"]


def test_detail(semester):
    d = home.detail("hw4")
    assert d["crumbs"] == ["college", "hw4"] and d["motes"] == 1 and d["bytes"] > 0
    assert d["owner"]["name"] == "HW4" and d["owner"]["garden"] == "college"
    assert d["sync"] is None and d["archivedBy"] is None and d["problems"] == []
    assert home.detail("college")["owns"] == ["hw4"]
    assert "archived" in home.detail("old")["archivedBy"]
    assert d["command"] == "garden hw4"
    with pytest.raises(CommandError, match="no garden"):
        home.detail("nope")


# ---- the grammar ----

@pytest.mark.parametrize("text, action, args", [
    ("garden", home.SHOW, ()),
    ("garden hw4", home.SHOW, ("hw4",)),
    ("garden show new", home.SHOW, ("new",)),
    ("garden new lab", "new", ("lab",)),
    ("garden rename a b", "rename", ("a", "b")),
    ("garden export a", "export", ("a",)),
    ("garden import ~/a.json b", "import", ("~/a.json", "b")),
    ("garden sync a now", "sync", ("a", "now")),
    ("garden sync a auto off", "sync", ("a", "auto", "off")),
])
def test_lines(text, action, args):
    line = home.parse_text(text)
    assert (line.action, line.args) == (action, args)


@pytest.mark.parametrize("text, message", [
    ("garden rename a", "takes ‹old› ‹new›"),
    ("garden hw4 extra", "not an action"),
    ("garden sync a auto maybe", "auto on|off"),
    ("garden sync a", "takes"),
])
def test_bad_lines(text, message):
    with pytest.raises(CommandError, match=message):
        home.parse_text(text)


def test_gardens_takes_only_archived():
    assert home.parse_gardens([]) is False and home.parse_gardens(["--archived"]) is True
    with pytest.raises(CommandError):
        home.parse_gardens(["--wat"])


def test_export_and_import_without_a_path_ask_for_one():
    assert home.needs_path(home.parse_text("garden export a"))
    assert home.needs_path(home.parse_text("garden import b"))
    assert not home.needs_path(home.parse_text("garden import ~/a.json b"))


# ---- acting ----

def test_new_rename_rm_restore(semester):
    assert run("garden new lab").created == "lab" and garden_exists("lab")
    fill("lab", {"name": "Alice"})
    done = run("garden rename lab lab2")
    assert done.renamed == ("lab", "lab2") and garden_exists("lab2") and not garden_exists("lab")
    assert "restore" in run("garden rm lab2").message and not garden_exists("lab2")
    run("garden restore lab2")
    conn = get_connection("lab2")
    assert [m.properties["name"] for m in list_motes(conn)] == ["Alice"]
    conn.close()


def test_the_open_garden_is_not_removed(semester):
    with pytest.raises(CommandError, match="open here"):
        run("garden rm hw4", current="hw4")
    assert garden_exists("hw4")


def test_a_bad_name_and_a_missing_garden(semester):
    with pytest.raises(ValueError):
        run("garden new 'no spaces'")
    with pytest.raises(CommandError, match="no garden"):
        run("garden rename nope other")
    with pytest.raises(LookupError):
        run("garden restore never-removed")


def test_archive_and_unarchive(semester):
    assert "archived" in run("garden archive scratch").message
    assert home.detail("scratch")["archivedBy"]
    run("garden unarchive scratch")
    assert home.detail("scratch")["archivedBy"] is None


def test_export_then_import_round_trips(semester, tmp_path):
    out = tmp_path / "hw4.json"
    assert str(out) in run(f"garden export hw4 {out}").message
    done = run(f"garden import {out} copy")
    assert done.created == "copy" and "1 motes" in done.message
    conn = get_connection("copy")
    assert [m.properties["name"] for m in list_motes(conn)] == ["beam"]
    conn.close()


def test_a_failed_import_leaves_no_garden_behind(semester, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"format": "something-else", "motes": []}))
    with pytest.raises(ValueError, match="format"):
        run(f"garden import {bad} imported")
    assert not garden_exists("imported")
    with pytest.raises(CommandError, match="cannot read"):
        run(f"garden import {tmp_path / 'missing.json'} imported")


def test_sync_switches(semester, sync_dir):
    fill("scratch", {"name": "Alice"})
    assert "syncs with" in run(f"garden sync scratch {sync_dir}").message
    assert get_sync_dir("scratch") == sync_dir and get_sync_dir("hw4") is None
    assert home.detail("scratch")["sync"] == {"url": sync_dir, "auto": True}
    run("garden sync scratch auto off")
    assert get_auto_sync_enabled("scratch") is False
    assert "sent" in run("garden sync scratch now").message
    run("garden sync scratch off")
    assert get_sync_dir("scratch") is None


def test_completion(semester):
    values = [v for v, _ in home.choices("")]
    assert "new" in values and "hw4" in values
    assert [v for v, _ in home.choices("sync hw4 ")] == ["now", "off", "auto"]
    assert home.choices("new ") == []

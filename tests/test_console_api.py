"""console/api.py -- the console's root bridge, against the U1 contract
the shell is built on (effects, layouts, history, garden switching)."""
import inspect
import sqlite3
import sys

import pytest

import api_common
from console import state as state_repo
from console.api import Api
from console.session import Session
from core import motes as motes_repo
from core import undo
from core.gardens import create_garden

TREE = {"split": "row", "sizes": [0.5, 0.5], "children": [{"command": "log"}, {"command": ""}]}

BRIDGE = {
    "boot", "run", "resolve", "complete", "history", "layout_put", "layout_get",
    "layout_delete", "layout_set_active", "working_put", "undo", "redo",
    "refresh", "switch_garden", "shutdown",
}
# api.monolith (U2). The walk collects bare method names.
MONOLITH_BRIDGE = {"table", "rewrite"}


@pytest.fixture
def api():
    create_garden("test")
    instance = Api("test")
    yield instance
    instance.shutdown()


@pytest.fixture
def launched(monkeypatch):
    calls = []
    monkeypatch.setattr(
        api_common, "popen_detached", lambda args, **kwargs: calls.append(args)
    )
    return calls


# ---- boot ----------------------------------------------------------------

def test_boot_shape(api):
    boot = api.boot()
    assert set(boot) == {"garden", "gardens", "apps", "verbs", "layouts",
                         "active", "working", "history"}
    assert boot["garden"] == "test"
    assert boot["gardens"] == ["test"]
    assert boot["apps"][3] == {"suffix": "tion", "name": "Motion",
                               "defaultView": None, "legacy": "motion"}
    assert [a["suffix"] for a in boot["apps"]] == [
        "nolith", "te", "xie", "tion", "ra", "nastery", "narch", "saic"]
    assert boot["apps"][0] == {"suffix": "nolith", "name": "Monolith",
                               "defaultView": "monolith:table", "legacy": None}
    assert boot["verbs"][2] == {"verb": "cd", "usage": "cd ‹garden›",
                                "describe": "switch this workspace's garden"}
    assert (boot["layouts"], boot["active"], boot["working"], boot["history"]) == (
        [], None, None, [])


# ---- the pywebview walk ------------------------------------------------------

def _reachable(obj, seen=None):
    """Mirrors webview/util.py::get_functions: skip `_` names, collect
    methods, recurse into classes and non-callable objects. Returns every
    object the walk touches, plus the exposed function names."""
    seen = seen if seen is not None else {"objects": [], "functions": set()}
    if any(o is obj for o in seen["objects"]):
        return seen
    seen["objects"].append(obj)
    for name in dir(obj):
        if name.startswith("_"):
            continue
        attr = getattr(obj, name)
        if inspect.ismethod(attr) or inspect.isfunction(attr):
            seen["functions"].add(name)
        elif inspect.isclass(attr) or (not callable(attr) and hasattr(attr, "__module__")):
            _reachable(attr, seen)
    return seen


def test_pywebview_walk_reaches_no_connection(api):
    seen = _reachable(api)
    assert seen["functions"] == BRIDGE | MONOLITH_BRIDGE
    for obj in seen["objects"]:
        assert not isinstance(obj, (sqlite3.Connection, Session))
    assert seen["objects"] == [api, api.monolith]


# ---- statements --------------------------------------------------------------

def test_a_statement_writes(api):
    effect = api.run("R1.x = 5 ohm")
    assert effect["effect"] == "statement"
    assert effect["ok"] and effect["created"] and effect["wrote"]
    assert set(effect) == {"effect", "ok", "message", "moteId", "moteName", "created",
                           "needsConfirm", "choices", "wrote"}
    assert effect["moteName"] == "R1"


def test_a_delete_needs_confirm_and_confirm_is_not_new_history(api):
    api.run("R1")
    first = api.run("delete R1")
    assert first["needsConfirm"] and not first["wrote"] and not first["ok"]
    second = api.run("delete R1", confirm=True)
    assert second["ok"] and second["wrote"]
    assert [h["command"] for h in api.history()] == ["R1", "delete R1"]


def test_a_reserved_name_warning_reaches_the_message(api):
    assert "console word" in api.run('"layout"')["message"]


def test_sel_expands_to_the_primary(api):
    mote_id = api.run("R1")["moteId"]
    effect = api.run("@sel.x = 2", [mote_id, "other"])
    assert effect["ok"] and effect["moteId"] == mote_id


def test_sel_with_nothing_selected_is_an_error(api):
    assert api.run("@sel.x = 2")["effect"] == "error"
    assert api.run("@sel.x = 2", ["0123abcd-dead-beef-0000-000000000000"]) == {
        "effect": "error", "message": "the selected mote no longer exists"}


def test_a_failed_line_is_still_in_history(api):
    api.run("layout frob")
    assert api.history()[-1]["command"] == "layout frob"


# ---- verbs and apps ----------------------------------------------------------

def test_log_opens_its_view(api):
    assert api.run("log") == {"effect": "open_view", "view": "log", "command": "log"}


def test_an_argumentless_verb_with_arguments_says_to_quote(api):
    effect = api.run("log -> R1")
    assert effect["effect"] == "error" and '"log"' in effect["message"]


def test_an_unported_app_opens_its_old_window(api, launched):
    effect = api.run("tion")
    assert effect == {"effect": "legacy", "app": "motion",
                      "message": "opened Motion (old window)"}
    assert launched[-1][0] == sys.executable
    assert launched[-1][1].endswith("run_motion.py")
    assert launched[-1][2:] == ["--garden", "test"]


def test_saic_opens_the_gardens_window(api, launched):
    assert api.run("saic")["app"] == "gardens"
    assert launched[-1][1].endswith("run_gardens.py")


def test_te_is_not_available_yet(api, launched):
    assert api.run("te") == {"effect": "error", "message": "Inspector isn't available yet"}
    assert launched == []


def test_resolve(api):
    assert api.resolve("") == {"view": "", "command": ""}
    assert api.resolve("log") == {"view": "log", "command": "log"}
    assert "error" in api.resolve("tion")
    assert api.history() == []


def test_refresh(api):
    assert api.run("refresh") == {"effect": "refresh", "message": "refreshed test"}
    assert api.refresh() == {"message": "refreshed test"}


def test_undo_and_redo_use_the_session_stack(api):
    assert api.run("undo") == {"effect": "undo", "label": None}
    mote_id = api.run("R1")["moteId"]
    api._session.undo.push(undo.for_create_mote(api._session.conn, mote_id, {"name": "R1"}, "add R1"))
    assert api.run("undo") == {"effect": "undo", "label": "add R1"}
    assert motes_repo.get_mote(api._session.conn, mote_id) is None
    assert api.redo() == {"label": "add R1"}
    assert api.undo() == {"label": "add R1"}


# ---- layouts -----------------------------------------------------------------

def test_layout_save_then_put(api):
    assert api.run("layout save two") == {"effect": "layout", "op": "save", "name": "two"}
    assert api.layout_put("two", TREE) == {"layouts": [{"name": "two", "tree": TREE}],
                                           "active": "two"}
    assert api.layout_get("two") == TREE


def test_layout_put_overwrite_keeps_order(api):
    api.layout_put("a", TREE)
    api.layout_put("b", TREE)
    result = api.layout_put("a", {"command": "log"})
    assert [layout["name"] for layout in result["layouts"]] == ["a", "b"]
    assert result["active"] == "a"


def test_layout_load_sets_active(api):
    api.layout_put("a", TREE)
    api.layout_put("b", {"command": "log"})
    assert api.run("layout load a") == {"effect": "layout", "op": "load", "name": "a",
                                        "tree": TREE}
    assert api.boot()["active"] == "a"


def test_layout_list_and_rm(api):
    api.layout_put("a", TREE)
    listed = api.run("layout list")
    assert listed == {"effect": "layout", "op": "list",
                      "layouts": [{"name": "a", "tree": TREE}], "active": "a"}
    assert api.run("layout rm a") == {"effect": "layout", "op": "rm", "name": "a",
                                      "layouts": [], "active": None}


@pytest.mark.parametrize("line", ["layout", "layout save", "layout load nope",
                                  "layout rm nope", "layout load", "layout frob x"])
def test_layout_errors(api, line):
    assert api.run(line)["effect"] == "error"


def test_layout_delete_and_set_active(api):
    api.layout_put("a", TREE)
    api.layout_set_active(None)
    assert api.boot()["active"] is None
    api.layout_set_active("a")
    assert api.layout_delete("a") == {"layouts": [], "active": None}
    assert api.layout_get("a") is None


def test_working_tree_survives_a_restart(api):
    api.working_put(TREE)
    api.layout_put("two", TREE)
    api.run("cd test")
    restarted = Api("test")
    try:
        boot = restarted.boot()
        assert boot["working"] == TREE
        assert boot["layouts"] == [{"name": "two", "tree": TREE}]
        assert boot["history"][-1]["command"] == "cd test"
    finally:
        restarted.shutdown()


# ---- gardens -----------------------------------------------------------------

def test_cd_switches_garden_state_and_undo(api):
    create_garden("other")
    api.run("R1")
    api.layout_put("mine", TREE)
    old_undo = api._session.undo

    effect = api.run("cd other")
    assert effect["effect"] == "garden"
    boot = effect["boot"]
    assert boot["garden"] == "other"
    assert boot["layouts"] == [] and boot["history"] == []
    assert api._session.undo is not old_undo
    assert api.complete("R")["rows"] == []  # the new garden's connection

    api.run("layout list")
    back = api.switch_garden("test")
    assert [h["command"] for h in back["history"]] == ["R1", "cd other"]
    assert back["layouts"] == [{"name": "mine", "tree": TREE}]
    assert [h["command"] for h in state_repo.load("other").history] == ["layout list"]


def test_cd_to_an_unknown_garden_is_an_error(api):
    effect = api.run("cd nowhere")
    assert effect["effect"] == "error"
    assert api.boot()["garden"] == "test"
    assert api.run("cd")["effect"] == "error"


# ---- completion --------------------------------------------------------------

def test_complete_shape(api):
    create_garden("other")
    result = api.complete("cd ot", 5)
    assert result == {
        "hint": "cd ‹garden›",
        "rows": [{"value": "other", "description": "", "dim": False, "current": False}],
        "ghost": "her",
        "replaceFrom": 3,
    }
    assert api.complete("cd ")["rows"][1] == {
        "value": "test", "description": "", "dim": False, "current": True}


def test_complete_layout_names(api):
    api.layout_put("two", TREE)
    assert [r["value"] for r in api.complete("layout load ")["rows"]] == ["two"]


# ---- U2: find and set (api.monolith) -----------------------------------------

@pytest.fixture
def chores(api):
    conn = api._session.conn
    species = motes_repo.create_mote(conn, {"name": "chore"})
    ids = []
    for i in range(4):
        mote = motes_repo.create_mote(conn, {"name": f"chore{i}", "status": "open", "rank": 4 - i})
        from core.relationships import create_relationship
        create_relationship(conn, mote, species, "instance_of")
        ids.append(mote)
    return ids


def _keys(payload):
    return [row["id"] for group in payload["groups"] for row in group["rows"]]


def test_find_opens_the_table_with_the_whole_line_as_its_command(api):
    effect = api.run("find status:open --sort rank")
    assert effect == {"effect": "open_view", "view": "monolith:table",
                      "command": "find status:open --sort rank"}
    assert api.resolve("find status:open") == {"view": "monolith:table",
                                               "command": "find status:open"}
    assert api.run("nolith") == {"effect": "open_view", "view": "monolith:table",
                                 "command": "find"}


def test_table_answers_a_find_line(api, chores):
    payload = api.monolith.table("find type:chore --sort rank")
    assert _keys(payload) == list(reversed(chores))
    assert payload["sort"] == {"key": "property:rank", "dir": "asc"}
    assert payload["group"] is None
    assert payload["matched"] == 4 and payload["total"] == 5
    assert {"columns", "groups", "speciesIds", "problems"} <= set(payload)
    # No --cols: the garden's default columns, species among them.
    assert [c["label"] for c in payload["columns"]][:2] == ["Name", "Species"]
    row = payload["groups"][0]["rows"][0]
    assert {"id", "name", "speciesId", "kinds", "cells"} <= set(row)


def test_table_groups_and_reports_a_bad_line(api, chores):
    grouped = api.monolith.table("find --group type")
    assert grouped["group"] == "graph:instance_of"
    assert any(g["label"] == "chore" and g["count"] == 4 for g in grouped["groups"])
    assert "error" in api.monolith.table('find "unclosed')
    assert "error" in api.monolith.table("find --view nothing")
    assert "error" in api.monolith.table("log")


def test_rewrite_sorts_flips_and_groups(api):
    first = api.monolith.rewrite("find status:open", {"sort": "property:rank"})
    assert first == {"command": "find status:open --sort rank"}
    again = api.monolith.rewrite(first["command"], {"sort": "property:rank"})
    assert again == {"command": "find status:open --sort -rank"}
    grouped = api.monolith.rewrite("find", {"group": "graph:instance_of"})
    assert grouped == {"command": "find --group type"}
    assert api.monolith.rewrite(grouped["command"], {"group": None}) == {"command": "find"}
    assert "error" in api.monolith.rewrite("find", {"sort": "nonsense"})


def test_the_default_species_column_sorts_by_what_it_shows(api, chores):
    payload = api.monolith.table("find")
    species = next(c for c in payload["columns"] if c["label"] == "Species")
    command = api.monolith.rewrite("find", {"sort": species["key"]})["command"]
    assert api.monolith.table(command)["sort"]["key"] == species["key"]


def test_set_at_sel_writes_the_whole_selection_and_one_undo_reverts_it(api, chores):
    chosen = chores[:3]
    effect = api.run("set @sel status done", selection=chosen)
    assert effect["effect"] == "statement" and effect["ok"] and effect["wrote"]
    assert effect["message"] == "set status on 3 motes"
    assert effect["moteId"] is None  # a batch edit leaves the selection alone
    conn = api._session.conn
    assert [motes_repo.get_mote(conn, i).properties["status"] for i in chores] == [
        "done", "done", "done", "open"]
    assert api.run("undo")["label"] == "set status on 3 motes"
    assert all(motes_repo.get_mote(conn, i).properties["status"] == "open" for i in chores)


def test_set_by_id_and_name_reads_values_like_the_command_bar(api, chores):
    api.run(f'set #{chores[0]} name "renamed chore"')
    conn = api._session.conn
    assert motes_repo.get_mote(conn, chores[0]).properties["name"] == "renamed chore"
    api.run("set chore1 length 5 mm")
    props = motes_repo.get_mote(conn, chores[1]).properties
    assert props["length"] == 5 and props["units"]["length"] == "mm"
    cleared = api.run(f"set #{chores[1]} length --clear")
    assert cleared["message"] == "cleared length on 1 mote"
    assert "length" not in motes_repo.get_mote(conn, chores[1]).properties


@pytest.mark.parametrize("line, selection, message", [
    ("set @sel status", [], "usage: set"),
    ("set @sel status done", [], "@sel is empty"),
    ("set #nope status done", [], "no mote #nope"),
    ("set ghost status done", [], "no mote named 'ghost'"),
    ("set chore1 status --clear x", [], "--clear takes no value"),
])
def test_set_says_what_is_wrong(api, chores, line, selection, message):
    effect = api.run(line, selection=selection)
    assert effect["effect"] == "error" and message in effect["message"]

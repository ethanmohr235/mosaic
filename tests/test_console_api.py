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
MONOLITH_BRIDGE = {"table", "rewrite", "inspect"}  # + api.mote (U3)
MOXIE_BRIDGE = {"plot", "rewrite", "sensitivity", "export_csv", "export_png", "fit"}  # U4


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
    assert boot["apps"][2] == {"suffix": "xie", "name": "Moxie",
                               "defaultView": "moxie:plot", "legacy": None}
    assert boot["apps"][3] == {"suffix": "tion", "name": "Motion",
                               "defaultView": None, "legacy": "motion"}
    assert [a["suffix"] for a in boot["apps"]] == [
        "nolith", "te", "xie", "tion", "ra", "nastery", "narch", "saic"]
    assert boot["apps"][0] == {"suffix": "nolith", "name": "Monolith",
                               "defaultView": "monolith:table", "legacy": None}
    assert boot["verbs"][9] == {"verb": "cd", "usage": "cd ‹garden›",
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
    assert seen["functions"] == BRIDGE | MONOLITH_BRIDGE | MOXIE_BRIDGE
    for obj in seen["objects"]:
        assert not isinstance(obj, (sqlite3.Connection, Session))
    assert seen["objects"] == [api, api.monolith, api.mote, api.moxie]


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
    effect = api.run("mo tion")
    assert effect == {"effect": "legacy", "app": "motion",
                      "message": "opened Motion (old window)"}
    assert launched[-1][0] == sys.executable
    assert launched[-1][1].endswith("run_motion.py")
    assert launched[-1][2:] == ["--garden", "test"]


def test_saic_opens_the_gardens_window(api, launched):
    assert api.run("mo saic")["app"] == "gardens"
    assert launched[-1][1].endswith("run_gardens.py")


def test_te_opens_the_props_pane_on_the_selection(api, launched):
    assert api.run("mo te") == {"effect": "open_view", "view": "mote:props", "command": "open @sel"}
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
    assert api.run("mo nolith") == {"effect": "open_view", "view": "monolith:table",
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


def test_a_typed_statement_is_undone_by_undo(api):
    api.run("R1.x = 5 ohm")
    conn = api._session.conn
    (r1,) = motes_repo.list_motes(conn)
    assert r1.properties["x"] == 5
    assert api.run("undo")["label"] == "R1.x = 5 ohm"
    assert motes_repo.list_motes(conn) == []
    api.run("redo")
    assert motes_repo.list_motes(conn)[0].properties["units"] == {"x": "ohm"}


# ---- U3: open, rename, unlink, template ----------------------------------------

def test_open_keeps_at_sel_literal_so_the_pane_follows(api, chores):
    effect = api.run("open @sel", selection=[chores[0]])
    assert effect == {"effect": "open_view", "view": "mote:props", "command": "open @sel"}
    assert api.resolve("open #abc") == {"view": "mote:props", "command": "open #abc"}


def test_rename_moves_value_and_unit_and_undoes(api, chores):
    conn = api._session.conn
    api.run(f"set #{chores[0]} length 5 mm")
    effect = api.run(f"rename #{chores[0]} length span")
    assert effect["ok"] and effect["wrote"]
    props = motes_repo.get_mote(conn, chores[0]).properties
    assert props["span"] == 5 and "length" not in props and props["units"] == {"span": "mm"}
    api.run("undo")
    props = motes_repo.get_mote(conn, chores[0]).properties
    assert props["length"] == 5 and "span" not in props and props["units"] == {"length": "mm"}


def test_rename_across_the_selection_refuses_to_overwrite(api, chores):
    conn = api._session.conn
    api.run(f"set #{chores[1]} done yes")
    effect = api.run("rename @sel status done", selection=chores[:2])
    assert effect["effect"] == "error" and "already has 'done'" in effect["message"]
    # Nothing half-renamed: the first mote was taken back.
    assert motes_repo.get_mote(conn, chores[0]).properties["status"] == "open"


def test_unlink_by_type_and_by_id_and_template(api, chores):
    from core import relationships as rel_repo
    conn = api._session.conn
    api.run("chore0 -> blocks -> chore1")
    assert api.run("unlink chore1 blocks chore0")["message"] == "removed 1 relationship"
    assert not [r for r in rel_repo.list_relationships(conn) if r.type == "blocks"]
    api.run("undo")
    (rel,) = [r for r in rel_repo.list_relationships(conn) if r.type == "blocks"]
    assert api.run(f"unlink chore0 #{rel.id}")["ok"]

    assert api.run("template chore2 chore")["ok"]
    (tmpl,) = [r for r in rel_repo.list_relationships(conn) if r.type == "template_of"]
    assert tmpl.from_id == chores[2]
    api.run("template chore3 chore")
    (tmpl,) = [r for r in rel_repo.list_relationships(conn) if r.type == "template_of"]
    assert tmpl.from_id == chores[3]
    api.run("undo")
    (tmpl,) = [r for r in rel_repo.list_relationships(conn) if r.type == "template_of"]
    assert tmpl.from_id == chores[2]


@pytest.mark.parametrize("line, message", [
    ("rename chore0 status", "usage: rename"),
    ("rename chore0 nothing else", "no 'nothing' to rename"),
    ("unlink chore0 blocks", "usage: unlink"),
    ("unlink chore0 blocks chore1", "no 'blocks' relationship"),
    ("template chore0", "usage: template"),
])
def test_u3_verbs_say_what_is_wrong(api, chores, line, message):
    effect = api.run(line)
    assert effect["effect"] == "error" and message in effect["message"]



def test_mote_inspect_follows_the_selection_or_a_named_mote(api, chores):
    empty = api.mote.inspect("open @sel", [])
    assert "empty" in empty
    followed = api.mote.inspect("open @sel", chores[:3])
    assert followed["id"] == chores[0] and followed["follows"] and followed["selectionCount"] == 3
    assert followed["species"]["label"] == "chore"
    pinned = api.mote.inspect("open chore2", chores[:1])
    assert pinned["id"] == chores[2] and not pinned["follows"]
    assert "error" in api.mote.inspect("open ghost", [])
    assert "error" in api.mote.inspect("find", [])



@pytest.mark.parametrize("line, message", [
    ("mo", "usage: mo"),
    ("mo nope", "no app mo nope"),
    ("mo tion now", "takes nothing after the app"),
])
def test_mo_says_what_is_wrong(api, line, message):
    effect = api.run(line)
    assert effect["effect"] == "error" and message in effect["message"]


def test_a_bare_suffix_is_a_statement_now(api):
    effect = api.run("tion")
    assert effect["effect"] == "statement" and effect["moteName"] == "tion"


# ---- plot (U4) ---------------------------------------------------------------

@pytest.fixture
def divider(api):
    from core.relationships import create_relationship
    from core.rules import APPLIES_TO, FREEDOMS_KEY
    from core.species import set_species
    conn = api._session.conn
    species = motes_repo.create_mote(conn, {"name": "Divider", FREEDOMS_KEY: "current"})
    rule = motes_repo.create_mote(conn, {"name": "series", "equation": "current * (r_a + r_b) == voltage"})
    create_relationship(conn, rule, species, APPLIES_TO)
    circuit = motes_repo.create_mote(conn, {"name": "C1", "voltage": 10, "r_a": 100, "r_b": 200})
    set_species(conn, circuit, species)
    return circuit


LINE = "plot C1.current vs C1.voltage 0:300 --steps 30"


def test_mo_xie_opens_the_plot_pane(api):
    assert api.run("mo xie") == {"effect": "open_view", "view": "moxie:plot", "command": "plot"}
    assert api.resolve(LINE) == {"view": "moxie:plot", "command": LINE}
    assert "error" in api.resolve("plot export csv")


def test_a_plot_line_opens_a_pane_with_the_literal_line(api):
    assert api.run(LINE) == {"effect": "open_view", "view": "moxie:plot", "command": LINE}


def test_the_plot_payload_coarse_then_full(api, divider):
    coarse = api.moxie.plot(LINE, True)
    full = api.moxie.plot(LINE)
    assert coarse["coarse"] and not full["coarse"]
    assert len(coarse["figure"]["series"][0]["points"]) < len(full["figure"]["series"][0]["points"]) == 31
    assert full["series"] == [{"id": "s0", "ref": "C1.current", "hidden": False}]
    assert full["mode"] == "sweep" and not full["xPinned"] and not full["saved"]
    assert api.moxie.plot("plot")["empty"]
    assert "no mote named" in api.moxie.plot("plot C9.x vs C1.voltage 0:1")["error"]


def test_a_plot_writes_nothing(api, divider):
    before = motes_repo.get_mote(api._session.conn, divider).properties
    api.moxie.plot(LINE + " --set C1.r_a=5")
    assert motes_repo.get_mote(api._session.conn, divider).properties == before


def test_rewrite_moves_a_knob_and_writes_a_saved_plot_out(api, divider):
    moved = api.moxie.rewrite(LINE, {"set": {"ref": "C1.r_a", "value": 50}})
    assert moved == {"command": LINE + " --set C1.r_a=50"}
    api.run('plot save Load ' + LINE)
    assert api.moxie.rewrite("plot Load", {"log": True}) == {"command": LINE + " --log"}


def test_plot_save_is_one_undoable_write(api, divider):
    effect = api.run("plot save Load " + LINE)
    assert effect["effect"] == "statement" and effect["wrote"]
    assert effect["message"] == "saved plot 'Load'"
    assert api.moxie.plot("plot Load")["name"] == "Load"
    assert api.undo()["label"] == "save plot"
    assert "error" in api.moxie.plot("plot Load")


@pytest.mark.parametrize("line, action", [
    ("plot save Load", {"action": "save", "name": "Load", "format": None, "path": None}),
    ("plot export png", {"action": "export", "name": None, "format": "png", "path": None}),
])
def test_save_or_export_with_no_line_acts_on_the_focused_pane(api, line, action):
    assert api.run(line) == {"effect": "pane_action", "view": "moxie:plot", **action}


def test_export_csv_to_a_path(api, divider, tmp_path):
    target = tmp_path / "out.csv"
    assert api.moxie.export_csv(LINE, str(target)) == {"path": str(target)}
    lines = target.read_text().splitlines()
    assert lines[0] == "C1.voltage,C1.current" and len(lines) == 32
    assert api.moxie.export_csv(LINE) == {"cancelled": True}  # no window, no dialog


def test_export_png_decodes_the_data_url(api, tmp_path):
    target = tmp_path / "p.png"
    assert api.moxie.export_png("data:image/png;base64,iVBORw0K", "p", str(target)) == {"path": str(target)}
    assert target.read_bytes().startswith(b"\x89PNG")


def test_sensitivity_from_the_bridge(api, divider):
    rows = api.moxie.sensitivity(LINE + " --set C1.r_a=100 --set C1.r_b=200", 150)["rows"]
    assert [r["ref"] for r in rows] == ["C1.r_b", "C1.r_a"]


def test_plot_completion(api, divider):
    rows = [r["value"] for r in api.complete("plot C1.cu")["rows"]]
    assert rows == ["C1.current"]


def test_a_newer_plot_request_cancels_the_older_one(api, divider):
    """A pane's requests share a channel: once generation 2 is claimed,
    generation 1 stops between points and says so."""
    api.moxie._claim("pane", 2)
    assert api.moxie.plot(LINE, False, "pane", 1) == {"cancelled": True}
    assert "figure" in api.moxie.plot(LINE, False, "pane", 3)
    assert "figure" in api.moxie.plot(LINE, False, "other", 1)


def test_plot_save_replaces_its_short_line_in_history(api, divider):
    api.run("plot save Load")
    api.run("plot save Load " + LINE[len("plot "):], replace_history=True)
    assert [h["command"] for h in api.history()] == ["plot save Load " + LINE[len("plot "):]]


# ---- fit ---------------------------------------------------------------------

FIT = "fit C1.current vs C1.voltage --data I vs V --free C1.r_a"


def test_a_fit_line_opens_a_fit_pane(api):
    assert api.run(FIT) == {"effect": "open_view", "view": "moxie:fit", "command": FIT}
    assert api.resolve(FIT) == {"view": "moxie:fit", "command": FIT}
    assert "error" in api.resolve(FIT + " --apply")


def test_fit_apply_writes_and_undoes(api, divider):
    for i, v in enumerate([5, 10, 20]):
        motes_repo.create_mote(api._session.conn, {"name": f"m{i}", "V": v, "I": v / 250})
    effect = api.run(FIT + " --apply")
    assert effect["effect"] == "statement" and effect["wrote"]
    assert effect["message"].startswith("fitted C1.r_a = 50")
    assert motes_repo.get_mote(api._session.conn, divider).properties["r_a"] == pytest.approx(50)
    assert api.undo()["label"] == "fit"
    assert motes_repo.get_mote(api._session.conn, divider).properties["r_a"] == 100


def test_the_fit_payload(api, divider):
    for i, v in enumerate([5, 10, 20]):
        motes_repo.create_mote(api._session.conn, {"name": f"m{i}", "V": v, "I": v / 250})
    p = api.moxie.fit(FIT)
    assert p["params"][0]["ref"] == "C1.r_a" and p["params"][0]["value"] == pytest.approx(50)
    assert [pt["name"] for pt in p["points"]] == ["m0", "m1", "m2"]
    assert p["points"][0]["fitted"] == pytest.approx(5 / 250)
    assert p["converged"] and p["applyCommand"] == FIT + " --apply"
    assert api.moxie.fit("fit")["empty"]
    assert "error" in api.moxie.fit(FIT + " --apply")
    assert motes_repo.get_mote(api._session.conn, divider).properties["r_a"] == 100

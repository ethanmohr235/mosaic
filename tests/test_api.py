import sys
import webbrowser

import pytest

import api_common
import monarch.api
from core import query as q
from core import sync as sync_repo
from core.gardens import create_garden, set_auto_sync_enabled
from monarch.api import Api, _coerce_properties


@pytest.fixture
def api():
    create_garden("test")
    instance = Api("test")
    yield instance
    instance.shutdown()


def test_focus_empty_garden_returns_none(api):
    result = api.focus()
    assert result["focus_id"] is None
    assert result["motes"] == []
    assert result["kinds"] == {}


def test_focus_none_picks_most_recent(api):
    api.create_mote({"name": "Alice"})
    second = api.create_mote({"name": "Bob"})
    result = api.focus()
    assert result["focus_id"] == second["id"]


def test_focus_given_valid_id_uses_it(api):
    first = api.create_mote({"name": "Alice"})
    api.create_mote({"name": "Bob"})
    result = api.focus(first["id"])
    assert result["focus_id"] == first["id"]


def test_focus_given_stale_id_falls_back_to_most_recent(api):
    api.create_mote({"name": "Alice"})
    second = api.create_mote({"name": "Bob"})
    result = api.focus("does-not-exist")
    assert result["focus_id"] == second["id"]


def test_focus_hops_limits_how_far_the_graph_reaches(api):
    a = api.create_mote({"name": "A"})
    b = api.create_mote({"name": "B"})
    c = api.create_mote({"name": "C"})
    api.create_relationship(a["id"], b["id"], "next")
    api.create_relationship(b["id"], c["id"], "next")

    assert len(api.focus(a["id"], 1)["motes"]) == 2
    assert len(api.focus(a["id"], 2)["motes"]) == 3


def test_focus_with_no_hops_limit_returns_the_whole_garden(api):
    a = api.create_mote({"name": "A"})
    b = api.create_mote({"name": "B"})
    api.create_relationship(a["id"], b["id"], "next")
    # Deliberately unconnected: no hop count can ever reach this one, which
    # is the whole reason "whole garden" exists as a separate setting.
    api.create_mote({"name": "Island"})

    result = api.focus(a["id"], None)
    assert len(result["motes"]) == 3
    assert result["focus_id"] == a["id"]


def test_species_with_only_a_template_is_reported_as_a_species(api):
    # The UI drives "New instance" and the species legend off the roles
    # map, so a species set up with a template but no instances yet has
    # to register -- otherwise there is no way to create its first
    # instance. The template registers as one too, which is how the
    # detail panel can say what it is.
    species = api.create_mote({"name": "Resistor"})
    template = api.create_mote({"name": "R", "resistance": "100"})
    api.set_template(template["id"], species["id"])

    result = api.focus(template["id"], None)
    assert result["kinds"][species["id"]] == ["species"]
    assert result["kinds"][template["id"]] == ["template"]


def test_list_species_reports_every_species_in_the_garden(api):
    # Garden-wide, unlike focus()'s roles map, which only classifies
    # the motes in the returned neighborhood.
    species = api.create_mote({"name": "Resistor"})
    instance = api.create_mote({"name": "R1"})
    api.set_species(instance["id"], species["id"])
    api.create_mote({"name": "Unrelated"})

    assert [m["id"] for m in api.list_species()] == [species["id"]]


def test_whole_garden_still_reports_kinds(api):
    species = api.create_mote({"name": "Node"})
    instance = api.create_mote({"name": "N1"})
    api.set_species(instance["id"], species["id"])

    result = api.focus(instance["id"], None)
    assert result["kinds"][species["id"]] == ["species"]


def test_coerce_properties_matches_repl_json_fallback_semantics():
    coerced = _coerce_properties({
        "age": "21",
        "active": "true",
        "name": "blank",
        "tags": "[1, 2]",
    })
    assert coerced == {"age": 21, "active": True, "name": "blank", "tags": [1, 2]}


def test_coerce_properties_passes_through_already_typed_values():
    # Copy/paste (app.js::copySelection/pasteAt) sends a mote's already-
    # coerced properties straight back through create_mote, not raw text
    # -- json.loads() raises TypeError (not JSONDecodeError) on a
    # non-string, so these must be passed through rather than re-parsed.
    coerced = _coerce_properties({"age": 21, "active": True, "tags": [1, 2], "meta": None})
    assert coerced == {"age": 21, "active": True, "tags": [1, 2], "meta": None}


def test_create_mote_accepts_a_copy_pasted_motes_already_typed_properties(api):
    original = api.create_mote({"name": "Box", "age": "21"})
    assert original["properties"]["age"] == 21  # coerced to an int on creation

    # This is exactly what pasteAt() does: spread the source mote's
    # already-typed properties dict and hand it straight to create_mote.
    pasted = api.create_mote(dict(original["properties"]))
    assert pasted["properties"] == original["properties"]


def test_switch_garden_swaps_connection(api):
    create_garden("other")
    api.create_mote({"name": "InTest"})

    api.switch_garden("other")
    assert api.current_garden() == "other"
    assert api.list_motes() == []

    api.switch_garden("test")
    names = [m["properties"]["name"] for m in api.list_motes()]
    assert names == ["InTest"]


# ---- lifecycle sync (open / garden switch / close) ----
#
# Enabling and disabling sync is the Gardens window's now
# (tests/test_gardens_api.py); what is still this window's is when it
# fires on its own. These point the garden at a sync server through
# core/sync.py directly, the way the Gardens window would have.

def _sync_to(garden: str, url) -> None:
    sync_repo.init(garden, str(url), clone=True)


def test_refresh_skips_sync_when_not_configured(api):
    assert api.refresh() == {"synced": False}


def test_refresh_syncs_when_configured(api, sync_dir):
    _sync_to(api.current_garden(), sync_dir)
    api.create_mote({"name": "Alice"})
    result = api.refresh()
    assert result["synced"] is True
    assert result["push"]["pushed"] >= 1


def test_best_effort_sync_skips_when_auto_sync_disabled(api, sync_dir, monkeypatch):
    # _best_effort_sync() runs against self._current_garden *after* a
    # switch completes, so exercising the guard on "test" means switching
    # away and back to it, not just switching away.
    _sync_to(api.current_garden(), sync_dir)
    set_auto_sync_enabled(api.current_garden(), False)
    create_garden("other")

    calls = []
    monkeypatch.setattr(monarch.api.sync_repo, "run", lambda *a, **k: calls.append(1))

    api.switch_garden("other")  # "other" has no sync dir -- guard never reached
    api.switch_garden("test")   # back on "test": sync dir set, auto-sync off
    assert calls == []


def test_best_effort_sync_runs_when_auto_sync_enabled(api, sync_dir, monkeypatch):
    _sync_to(api.current_garden(), sync_dir)
    create_garden("other")

    calls = []
    monkeypatch.setattr(monarch.api.sync_repo, "run", lambda *a, **k: calls.append(1))

    api.switch_garden("other")
    api.switch_garden("test")
    assert calls == [1]


def test_shutdown_skips_sync_when_auto_sync_disabled(sync_dir, monkeypatch):
    # A fresh instance, not the `api` fixture -- the fixture's own
    # teardown calls shutdown() too, which would double-fire this.
    create_garden("shut")
    inst = Api("shut")
    _sync_to("shut", sync_dir)
    set_auto_sync_enabled("shut", False)

    calls = []
    monkeypatch.setattr(monarch.api.sync_repo, "run", lambda *a, **k: calls.append(1))

    inst.shutdown()
    assert calls == []


def test_create_mote_returns_plain_dict(api):
    result = api.create_mote({"name": "Alice"})
    assert isinstance(result, dict)
    assert result["properties"]["name"] == "Alice"


def test_create_relationship_returns_plain_dict(api):
    a = api.create_mote({"name": "Alice"})
    b = api.create_mote({"name": "Bob"})
    result = api.create_relationship(a["id"], b["id"], "knows")
    assert isinstance(result, dict)
    assert result["from_id"] == a["id"]
    assert result["to_id"] == b["id"]


def test_create_mote_with_explicit_id_reuses_it(api):
    # This is how undo restores a deleted mote -- with its original id,
    # so anything else still referencing that id doesn't need remapping.
    result = api.create_mote({"name": "Alice"}, id="fixed-id")
    assert result["id"] == "fixed-id"


def test_create_relationship_with_explicit_id_reuses_it(api):
    a = api.create_mote({"name": "Alice"})
    b = api.create_mote({"name": "Bob"})
    result = api.create_relationship(a["id"], b["id"], "knows", id="fixed-rel-id")
    assert result["id"] == "fixed-rel-id"


# ---- undo ----

def test_creating_a_mote_can_be_undone(api):
    result = api.create_mote({"name": "Alice"})
    assert api.undo()["undone"] is not None
    assert api.get_mote(result["id"]) is None


def test_deleting_a_mote_can_be_undone(api):
    result = api.create_mote({"name": "Alice"})
    api.delete_mote(result["id"])
    assert api.get_mote(result["id"]) is None
    assert api.undo()["undone"] is not None
    assert api.get_mote(result["id"])["properties"]["name"] == "Alice"


def test_creating_a_relationship_can_be_undone(api):
    a = api.create_mote({"name": "Alice"})
    b = api.create_mote({"name": "Bob"})
    rel = api.create_relationship(a["id"], b["id"], "knows")
    assert api.undo()["undone"] is not None
    assert rel["id"] not in {r["id"] for r in api.mote_relationships(a["id"])}


def test_deleting_several_motes_is_undone_in_one_step(api):
    a = api.create_mote({"name": "Alice"})
    b = api.create_mote({"name": "Bob"})
    answer = api.delete_motes([a["id"], b["id"]])
    assert answer["deleted"] == 2
    assert api.undo()["undone"] is not None
    assert api.get_mote(a["id"]) is not None
    assert api.get_mote(b["id"]) is not None


def test_setting_species_can_be_undone(api):
    species = api.create_mote({"name": "Task"})
    mote = api.create_mote({"name": "Write plan"})
    api.set_species(mote["id"], species["id"])
    assert len(api.species_of(mote["id"])) == 1
    assert api.undo()["undone"] is not None
    assert api.species_of(mote["id"]) == []


def test_open_link_launches_a_url(api, monkeypatch):
    calls = []
    monkeypatch.setattr(webbrowser, "open", lambda x: calls.append(x))
    result = api.open_link("https://example.com")
    assert calls == ["https://example.com"]
    assert result == {"opened": "https://example.com"}


def test_open_link_launches_a_local_file_via_the_os_default_app(api, monkeypatch, tmp_path):
    # A local file must NOT go through webbrowser.open() -- that always
    # launches the web browser, which shows a .py file as plain text
    # instead of opening it in whatever the OS has registered for .py.
    f = tmp_path / "note.txt"
    f.write_text("hi")
    calls = []
    monkeypatch.setattr(monarch.api, "_open_with_os_default", lambda p: calls.append(p))
    browser_calls = []
    monkeypatch.setattr(webbrowser, "open", lambda x: browser_calls.append(x))

    api.open_link(str(f))

    assert calls == [str(f.resolve())]
    assert browser_calls == []


class _FakeProc:
    def wait(self):
        return 0


def test_open_with_os_default_prefers_gio_on_linux(monkeypatch):
    # gio detects content-type the same way GTK file managers do; xdg-open
    # uses a cruder heuristic that can disagree and silently land on the
    # wrong app (this is the actual bug that was reported and reproduced).
    monkeypatch.setattr(api_common.platform, "system", lambda: "Linux")
    calls = []
    monkeypatch.setattr(api_common.subprocess, "Popen",
                        lambda args, **kw: calls.append(args) or _FakeProc())

    api_common.open_with_os_default("/tmp/some/file.py")

    assert calls == [["gio", "open", "/tmp/some/file.py"]]


def test_open_with_os_default_falls_back_to_xdg_open_if_gio_is_missing(monkeypatch):
    monkeypatch.setattr(api_common.platform, "system", lambda: "Linux")
    calls = []

    def fake_popen(args, **kwargs):
        calls.append(args)
        if args[0] == "gio":
            raise FileNotFoundError()
        return _FakeProc()

    monkeypatch.setattr(api_common.subprocess, "Popen", fake_popen)

    api_common.open_with_os_default("/tmp/some/file.py")

    assert calls == [["gio", "open", "/tmp/some/file.py"], ["xdg-open", "/tmp/some/file.py"]]


def test_open_link_missing_file_raises(api):
    with pytest.raises(FileNotFoundError):
        api.open_link("/no/such/file/anywhere")


# ---- opening Mora ----

def _mora_commands(monkeypatch):
    """What open_mora would actually launch, without launching it."""
    launched = []
    monkeypatch.setattr(
        monarch.api, "_popen_detached", lambda args, **kwargs: launched.append(args)
    )
    return launched


def test_open_mora_launches_the_editor_on_the_current_garden(api, monkeypatch):
    launched = _mora_commands(monkeypatch)
    assert api.open_mora() == {"garden": "test", "note": None}
    (command,) = launched
    # The venv the app is running in, not a bare `python` off PATH --
    # see docs/ui-setup.md.
    assert command[0] == sys.executable
    assert command[1].endswith("run_mora.py")
    assert command[2:] == ["--garden", "test"]


def test_open_mora_passes_the_mote_whose_note_was_asked_for(api, monkeypatch):
    launched = _mora_commands(monkeypatch)
    mote_id = api.create_mote({"name": "Notebook"})["id"]
    assert api.open_mora(mote_id)["note"] == mote_id
    assert launched[0][-2:] == ["--note", mote_id]


def test_open_mora_follows_a_garden_switch(api, monkeypatch):
    launched = _mora_commands(monkeypatch)
    create_garden("elsewhere")
    api.switch_garden("elsewhere")
    api.open_mora()
    assert launched[0][2:] == ["--garden", "elsewhere"]


# ---- the ⌘J switcher ----
#
# open_mora/open_monastery/open_momentum above stay named the way
# Monarch's own Tools menu calls them; open_app is the app-agnostic
# counterpart the switcher uses instead. Its command-building is
# api_common.open_app's own test (tests/test_api_common.py) -- what's
# worth checking here is only that this method delegates to it.

def test_open_app_delegates_to_api_common(api, monkeypatch):
    import api_common

    calls = []
    monkeypatch.setattr(
        api_common, "popen_detached", lambda args, **kwargs: calls.append(args)
    )
    assert api.open_app("mora") == {"app": "mora", "garden": "test", "focus": None}
    assert calls[0][2:] == ["--garden", "test"]


def test_reserved_names_returns_properties_and_relationship_types(api):
    result = api.reserved_names()
    assert "url" in result["properties"]
    assert "date" not in result["properties"], \
        "when something happens is a value shape now, not a reserved name"
    assert "interval" in result["valueShapes"]
    assert "instance_of" in result["relationshipTypes"]
    assert result["properties"] == sorted(result["properties"]), "should already be sorted for the frontend"


def test_reserved_names_returns_the_full_reference_not_just_names(api):
    """The app used to get a bare name list while an MCP client got the
    meanings, the equation language and every provider's operations --
    the surface with a person behind it was the less informed one."""
    reserved = api.reserved_names()

    assert "instance_of" in reserved["relationshipTypes"]
    assert "provider" in reserved["properties"]
    # ...and now what they mean, for the detail panel's tooltips.
    assert "provider" in reserved["propertyMeanings"]
    assert "instance_of" in reserved["relationshipTypeMeanings"]
    # ...plus what each provider can compute, for the property-model note.
    assert set(reserved["propertyModelProviders"]) == {"ideal_gas", "coolprop"}
    ideal_gas = reserved["propertyModelProviders"]["ideal_gas"]
    assert any(c["operation"] == "pressure" for c in ideal_gas["capabilities"])

# ---- saved view filter ----
#
# A saved view is Monolith's, but the mote it lives on is an ordinary
# mote like any other -- these build one directly with core/query.py
# rather than going through monolith.api.Api.save_view, the same way
# test_monolith_api.py's own query_with() skips the frontend's editor.

def _saved_view(api, name: str, **kwargs) -> dict:
    query = q.Query(**kwargs).to_json()
    return api.create_mote({"name": name, q.QUERY_KEY: query})


def test_view_filter_ids_returns_only_matching_motes(api):
    matching = api.create_mote({"status": "doing"})
    other = api.create_mote({"status": "done"})
    view = _saved_view(
        api, "In flight",
        conditions=(q.Condition(q.Field(q.PROPERTY, "status"), "is", "doing"),),
    )

    ids = api.view_filter_ids(view["id"])

    assert ids == [matching["id"]]
    assert other["id"] not in ids


def test_view_filter_ids_with_no_conditions_matches_everything(api):
    a = api.create_mote({"name": "a"})
    b = api.create_mote({"name": "b"})
    view = _saved_view(api, "Everything")

    ids = api.view_filter_ids(view["id"])

    assert set(ids) == {a["id"], b["id"], view["id"]}


def test_view_filter_ids_returns_none_for_a_mote_with_no_query(api):
    plain = api.create_mote({"name": "not a view"})
    assert api.view_filter_ids(plain["id"]) is None


def test_view_filter_ids_returns_none_for_a_missing_mote(api):
    assert api.view_filter_ids("nonexistent") is None


# ---- annotations (core/annotations.py via AnnotatableApi) ---------------


def test_create_sticky_note_round_trips_through_list(api):
    created = api.create_sticky_note("world", 1.0, 2.0, text="hello")
    assert created["anchorMode"] == "world"
    assert created["text"] == "hello"

    listed = api.list_annotations()
    assert [a["id"] for a in listed] == [created["id"]]


def test_move_and_edit_and_delete_annotation(api):
    note = api.create_sticky_note("world", 0, 0)

    moved = api.move_annotation(note["id"], 9, 9)
    assert (moved["x"], moved["y"]) == (9, 9)

    edited = api.update_annotation_text(note["id"], "new text")
    assert edited["text"] == "new text"

    assert api.delete_annotation(note["id"]) is True
    assert api.list_annotations() == []


def test_annotations_do_not_leak_into_mote_and_focus_payloads(api):
    mote = api.create_mote({"name": "A"})
    api.create_sticky_note("mote", 0, 0, mote_id=mote["id"], text="note on A")

    assert all("annotation" not in m for m in api.list_motes())
    focus = api.focus(mote["id"])
    assert all("annotation" not in m for m in focus["motes"])


def test_annotation_mutations_never_write_to_oplog(api):
    before = api._conn.execute("SELECT COUNT(*) AS n FROM oplog").fetchone()["n"]

    note = api.create_sticky_note("world", 0, 0, text="a")
    api.move_annotation(note["id"], 1, 1)
    api.update_annotation_text(note["id"], "b")
    api.delete_annotation(note["id"])

    after = api._conn.execute("SELECT COUNT(*) AS n FROM oplog").fetchone()["n"]
    assert after == before


def test_create_and_delete_annotation_are_undoable(api):
    note = api.create_sticky_note("world", 0, 0, text="a")
    api.undo()
    assert api.list_annotations() == []

    api.redo()
    assert [a["id"] for a in api.list_annotations()] == [note["id"]]

    api.delete_annotation(note["id"])
    api.undo()
    assert [a["id"] for a in api.list_annotations()] == [note["id"]]

    api.redo()
    assert api.list_annotations() == []


def test_rapid_moves_coalesce_into_one_undo_step(api):
    note = api.create_sticky_note("world", 0, 0)
    api.undo_manager.can_undo()  # sanity: manager exists

    api.move_annotation(note["id"], 1, 1)
    api.move_annotation(note["id"], 2, 2)

    stack_after_moves = len(api.undo_manager._undo_stack)
    api.undo()  # undoes the coalesced move, landing back at the create
    assert api.list_annotations()[0]["x"] == 0

    # the two moves collapsed into one undo step on top of the create
    assert stack_after_moves == 2


def test_move_annotations_moves_a_selection_as_one_undo_step(api):
    a = api.create_sticky_note("world", 0, 0)
    b = api.create_sticky_note("world", 10, 10)
    api.move_annotations([{"id": a["id"], "x": 5, "y": 5}, {"id": b["id"], "x": 15, "y": 15}])
    by_id = {n["id"]: n for n in api.list_annotations()}
    assert (by_id[a["id"]]["x"], by_id[b["id"]]["x"]) == (5, 15)

    api.undo()
    by_id = {n["id"]: n for n in api.list_annotations()}
    assert (by_id[a["id"]]["x"], by_id[b["id"]]["x"]) == (0, 10)


def test_delete_annotations_deletes_a_selection_as_one_undo_step(api):
    a = api.create_sticky_note("world", 0, 0, text="a")
    b = api.create_sticky_note("world", 1, 1, text="b")
    assert api.delete_annotations([a["id"], b["id"], "missing"]) == 2
    assert api.list_annotations() == []

    api.undo()
    assert {n["text"] for n in api.list_annotations()} == {"a", "b"}


def test_resize_annotation_is_undoable(api):
    note = api.create_sticky_note("world", 0, 0)
    resized = api.resize_annotation(note["id"], 20, 30, 200, 150)
    assert (resized["x"], resized["y"], resized["width"], resized["height"]) == (20, 30, 200, 150)

    api.undo()
    restored = api.list_annotations()[0]
    assert (restored["x"], restored["y"], restored["width"], restored["height"]) == (0, 0, None, None)


def test_anchor_annotation_attaches_and_detaches_undoably(api):
    mote = api.create_mote({"name": "A"})
    note = api.create_sticky_note("world", 100, 100)

    attached = api.anchor_annotation(note["id"], "mote", mote["id"], 40, -40)
    assert (attached["anchorMode"], attached["moteId"], attached["x"]) == ("mote", mote["id"], 40)

    detached = api.anchor_annotation(note["id"], "world", mote["id"], 140, 60)
    assert (detached["anchorMode"], detached["moteId"]) == ("world", None)

    api.undo()
    back = api.list_annotations()[0]
    assert (back["anchorMode"], back["moteId"], back["x"]) == ("mote", mote["id"], 40)
    api.undo()
    back = api.list_annotations()[0]
    assert (back["anchorMode"], back["x"], back["y"]) == ("world", 100, 100)

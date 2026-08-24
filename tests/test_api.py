import json
import sys
import webbrowser

import pytest

import ui.api
from core.gardens import create_garden, garden_exists
from core.simulation import SimulationError, run_simulation
from ui.api import Api, _coerce_properties


class _StubWindow:
    """Minimal stand-in for pywebview's window -- just enough of
    create_file_dialog to test export_garden's own logic without a real
    native dialog."""

    def __init__(self, path):
        self._path = path

    def create_file_dialog(self, *args, **kwargs):
        return self._path


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


def test_focus_reports_the_folder_kind(api):
    folder = api.create_mote({"name": "Thermodynamics"})
    member = api.create_mote({"name": "Entropy"})
    api.set_folder(member["id"], folder["id"])

    result = api.focus(member["id"], None)
    assert result["kinds"] == {folder["id"]: ["folder"]}


def test_set_folder_creates_an_in_folder_relationship(api):
    folder = api.create_mote({"name": "Thermodynamics"})
    member = api.create_mote({"name": "Entropy"})
    rel = api.set_folder(member["id"], folder["id"])
    assert rel["type"] == "in_folder"
    assert rel["from_id"] == member["id"]
    assert rel["to_id"] == folder["id"]

    assert [m["id"] for m in api.folders_of(member["id"])] == [folder["id"]]
    assert [m["id"] for m in api.folder_members(folder["id"])] == [member["id"]]


def test_delete_garden_blocks_the_last_garden(api):
    with pytest.raises(ValueError, match="last garden"):
        api.delete_garden("test")


def test_delete_garden_blocks_the_current_garden(api):
    create_garden("other")
    with pytest.raises(ValueError, match="currently in"):
        api.delete_garden("test")
    # Blocked, not partially applied.
    assert "test" in api.list_gardens()


def test_delete_garden_succeeds_for_a_non_current_garden(api):
    create_garden("other")
    result = api.delete_garden("other")
    assert result == {"deleted": "other"}
    assert "other" not in api.list_gardens()


def test_restore_garden_brings_a_deleted_garden_back(api):
    create_garden("other")
    api.delete_garden("other")
    assert "other" not in api.list_gardens()

    result = api.restore_garden("other")

    assert result == {"restored": "other"}
    assert "other" in api.list_gardens()


def test_restore_garden_missing_raises(api):
    with pytest.raises(LookupError):
        api.restore_garden("never-deleted")


def test_list_gardens_hides_the_library_garden(api):
    mote = api.create_mote({"name": "Task"})
    api.save_to_library(mote["id"])
    assert "_library" not in api.list_gardens()


def test_create_garden_rejects_the_library_name(api):
    with pytest.raises(ValueError, match="reserved"):
        api.create_garden("_library")


def test_open_library_lazily_creates_and_switches(api):
    assert not garden_exists("_library")

    result = api.open_library()

    assert result == {"garden": "_library"}
    assert api.current_garden() == "_library"


def test_open_library_switches_into_an_existing_library(api):
    mote = api.create_mote({"name": "Task"})
    api.save_to_library(mote["id"])

    result = api.open_library()

    assert result == {"garden": "_library"}
    assert api.current_garden() == "_library"
    entries = api.list_motes()
    assert any(m["properties"].get("name") == "Task" for m in entries)


def test_library_snapshot_returns_motes_relationships_and_classification(api):
    mote = api.create_mote({"name": "Task"})
    api.save_to_library(mote["id"])

    api.open_library()  # ensures _library exists before reading it back
    api.switch_garden("test")

    result = api.library_snapshot()
    assert any(m["properties"].get("name") == "Task" for m in result["motes"])
    assert result["relationships"] == []
    assert result["kinds"] == {}


def test_library_snapshot_reports_kinds_from_within_the_library(api):
    api.open_library()
    folder = api.create_mote({"name": "Thermodynamics"})
    member = api.create_mote({"name": "Entropy"})
    api.set_folder(member["id"], folder["id"])
    api.switch_garden("test")

    result = api.library_snapshot()
    assert result["kinds"] == {folder["id"]: ["folder"]}


def test_reuse_from_library_copies_an_entry_into_the_current_garden(api):
    mote = api.create_mote({"name": "Standup", "duration": 15})
    entry = api.save_to_library(mote["id"])

    copy = api.reuse_from_library(entry["id"], {"name": "Monday standup"})

    # A fresh, independent mote in the *current* garden -- not the entry,
    # and not linked to it.
    assert copy["id"] not in (mote["id"], entry["id"])
    assert copy["properties"] == {"name": "Monday standup", "duration": 15}
    assert api.get_mote(copy["id"]) is not None


def test_reuse_from_library_coerces_overrides_like_any_other_ui_input(api):
    mote = api.create_mote({"name": "Task"})
    entry = api.save_to_library(mote["id"])

    copy = api.reuse_from_library(entry["id"], {"priority": "3"})

    assert copy["properties"]["priority"] == 3


def test_delete_library_entry_leaves_the_current_garden_alone(api):
    mote = api.create_mote({"name": "Task"})
    entry = api.save_to_library(mote["id"])

    assert api.delete_library_entry(entry["id"]) is True
    assert api.library_snapshot()["motes"] == []
    # The mote it was snapshotted from is untouched, and so is the
    # connection this Api still holds open on "test".
    assert api.current_garden() == "test"
    assert api.get_mote(mote["id"]) is not None


def test_find_portal_target_searches_another_garden(api):
    create_garden("other")
    other_api = Api("other")
    other_api.create_mote({"name": "Reactor"})
    other_api.create_mote({"name": "Pump"})
    other_api.shutdown()

    results = api.find_portal_target("other", "react")

    assert [m["properties"]["name"] for m in results] == ["Reactor"]
    assert api.current_garden() == "test"


def test_find_portal_target_refuses_a_garden_that_does_not_exist(api):
    # Not silently creating it: get_connection() would happily bring an
    # empty garden into being from a typo.
    with pytest.raises(LookupError):
        api.find_portal_target("no-such-garden", "anything")
    assert not garden_exists("no-such-garden")


def test_create_portal_then_jump_switches_gardens(api):
    create_garden("other")
    other_api = Api("other")
    target = other_api.create_mote({"name": "Reactor"})
    other_api.shutdown()

    portal = api.create_portal("other", target["id"], {"name": "Reactor"})
    assert portal["properties"]["portal_garden"] == "other"
    assert portal["properties"]["portal_mote"] == target["id"]

    result = api.jump_to_portal(portal["id"])

    assert result == {"garden": "other", "mote_id": target["id"]}
    assert api.current_garden() == "other"
    # The connection really moved: the target garden's mote is readable
    # and the origin garden's portal mote is not.
    assert api.get_mote(target["id"])["properties"]["name"] == "Reactor"
    assert api.get_mote(portal["id"]) is None


def test_jump_to_portal_rejects_a_mote_that_is_not_a_portal(api):
    plain = api.create_mote({"name": "Plain"})
    with pytest.raises(ValueError):
        api.jump_to_portal(plain["id"])
    assert api.current_garden() == "test"


def test_default_garden_round_trips_and_clears(api):
    assert api.get_default_garden() is None

    api.set_default_garden("test")
    assert api.get_default_garden() == "test"

    api.clear_default_garden()
    assert api.get_default_garden() is None


def test_import_garden_into_new_creates_and_fills_a_garden(api, tmp_path):
    api.create_mote({"name": "Original"})
    out = tmp_path / "export.json"
    api._window = _StubWindow(str(out))
    api.export_garden_to_file()

    api._window = _StubWindow([str(out)])
    result = api.import_garden_into_new("imported")

    assert result["motes"] == 1
    assert garden_exists("imported")
    # Importing does not switch the caller into the new garden -- the
    # frontend does that as a separate step.
    assert api.current_garden() == "test"

    imported = Api("imported")
    try:
        names = [m["properties"].get("name") for m in imported.list_motes()]
    finally:
        imported.shutdown()
    assert names == ["Original"]


def test_export_garden_to_file_by_name_does_not_disturb_the_current_garden(api, tmp_path):
    create_garden("other")
    other_api = Api("other")
    other_api.create_mote({"name": "OtherMote"})
    other_api.shutdown()

    api.create_mote({"name": "CurrentMote"})
    out = tmp_path / "other.json"
    api._window = _StubWindow(str(out))

    result = api.export_garden_to_file("other")

    assert result["path"] == str(out)
    assert api.current_garden() == "test"
    data = json.loads(out.read_text())
    names = [m["properties"].get("name") for m in data["motes"]]
    assert names == ["OtherMote"]


def test_export_garden_to_file_defaults_to_the_current_garden(api, tmp_path):
    api.create_mote({"name": "CurrentMote"})
    out = tmp_path / "test.json"
    api._window = _StubWindow(str(out))

    api.export_garden_to_file()

    data = json.loads(out.read_text())
    names = [m["properties"].get("name") for m in data["motes"]]
    assert names == ["CurrentMote"]


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
    monkeypatch.setattr(ui.api, "_open_with_os_default", lambda p: calls.append(p))
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
    monkeypatch.setattr(ui.api.platform, "system", lambda: "Linux")
    calls = []
    monkeypatch.setattr(ui.api.subprocess, "Popen", lambda args, **kw: calls.append(args) or _FakeProc())

    ui.api._open_with_os_default("/tmp/some/file.py")

    assert calls == [["gio", "open", "/tmp/some/file.py"]]


def test_open_with_os_default_falls_back_to_xdg_open_if_gio_is_missing(monkeypatch):
    monkeypatch.setattr(ui.api.platform, "system", lambda: "Linux")
    calls = []

    def fake_popen(args, **kwargs):
        calls.append(args)
        if args[0] == "gio":
            raise FileNotFoundError()
        return _FakeProc()

    monkeypatch.setattr(ui.api.subprocess, "Popen", fake_popen)

    ui.api._open_with_os_default("/tmp/some/file.py")

    assert calls == [["gio", "open", "/tmp/some/file.py"], ["xdg-open", "/tmp/some/file.py"]]


def test_open_link_missing_file_raises(api):
    with pytest.raises(FileNotFoundError):
        api.open_link("/no/such/file/anywhere")


# ---- opening Mora ----

def _mora_commands(monkeypatch):
    """What open_mora would actually launch, without launching it."""
    launched = []
    monkeypatch.setattr(
        ui.api, "_popen_detached", lambda args, **kwargs: launched.append(args)
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


class _FakeCredentials:
    token = "fake-token"


def test_export_to_google_calendar_creates_events_and_writes_back_ids(api, monkeypatch):
    a = api.create_mote({"name": "Exam", "date": "2026-08-15T09:00:00"})
    b = api.create_mote({"name": "No date"})

    pushed = []

    def fake_push_event(credentials, payload, existing_event_id):
        pushed.append((payload["summary"], existing_event_id))
        return f"event-for-{payload['summary']}"

    monkeypatch.setattr(ui.api.gcal_repo, "get_credentials", lambda: _FakeCredentials())
    monkeypatch.setattr(ui.api.gcal_repo, "list_managed_events", lambda credentials: {})
    monkeypatch.setattr(ui.api.gcal_repo, "push_event", fake_push_event)

    result = api.export_to_google_calendar()

    assert result == {"created": 1, "updated": 0, "skipped": 0, "deleted": 0, "total": 1}
    assert pushed == [("Exam", None)]
    assert api.get_mote(a["id"])["properties"]["gcal_event_id"] == "event-for-Exam"
    assert "gcal_event_id" not in api.get_mote(b["id"])["properties"]


def test_export_to_google_calendar_excludes_a_dated_template_mote(api, monkeypatch):
    species = api.create_mote({"name": "Deadline"})
    template = api.create_mote({"name": "template", "date": "2026-08-15"})
    api.set_template(template["id"], species["id"])
    real = api.create_mote({"name": "Real deadline", "date": "2026-08-20"})

    pushed = []
    monkeypatch.setattr(ui.api.gcal_repo, "get_credentials", lambda: _FakeCredentials())
    monkeypatch.setattr(ui.api.gcal_repo, "list_managed_events", lambda credentials: {})
    monkeypatch.setattr(
        ui.api.gcal_repo, "push_event",
        lambda credentials, payload, existing_event_id: pushed.append(payload["summary"]) or "evt",
    )

    result = api.export_to_google_calendar()

    assert result == {"created": 1, "updated": 0, "skipped": 0, "deleted": 0, "total": 1}
    assert pushed == ["Real deadline"]
    assert "gcal_event_id" not in api.get_mote(template["id"])["properties"]


def test_export_to_google_calendar_reuses_the_existing_event_id_on_a_second_run(api, monkeypatch):
    a = api.create_mote({"name": "Exam", "date": "2026-08-15T09:00:00"})
    calls = []

    def fake_push_event(credentials, payload, existing_event_id):
        calls.append(existing_event_id)
        return "same-event-id"

    monkeypatch.setattr(ui.api.gcal_repo, "get_credentials", lambda: _FakeCredentials())
    monkeypatch.setattr(ui.api.gcal_repo, "list_managed_events", lambda credentials: {})
    monkeypatch.setattr(ui.api.gcal_repo, "push_event", fake_push_event)

    first = api.export_to_google_calendar()
    second = api.export_to_google_calendar()

    assert first == {"created": 1, "updated": 0, "skipped": 0, "deleted": 0, "total": 1}
    assert second == {"created": 0, "updated": 1, "skipped": 0, "deleted": 0, "total": 1}
    assert calls == [None, "same-event-id"]
    assert api.get_mote(a["id"])["properties"]["gcal_event_id"] == "same-event-id"


def test_export_to_google_calendar_skips_a_mote_with_an_unparseable_date(api, monkeypatch):
    api.create_mote({"name": "Bad", "date": "not-a-date"})
    calls = []
    monkeypatch.setattr(ui.api.gcal_repo, "get_credentials", lambda: _FakeCredentials())
    monkeypatch.setattr(ui.api.gcal_repo, "list_managed_events", lambda credentials: {})
    monkeypatch.setattr(ui.api.gcal_repo, "push_event", lambda *a, **k: calls.append(1))

    result = api.export_to_google_calendar()

    assert result == {"created": 0, "updated": 0, "skipped": 1, "deleted": 0, "total": 1}
    assert calls == []


def test_export_to_google_calendar_does_not_write_back_if_the_garden_changed_mid_export(api, monkeypatch):
    create_garden("other")
    a = api.create_mote({"name": "Exam", "date": "2026-08-15T09:00:00"})

    def fake_push_event(credentials, payload, existing_event_id):
        # Simulate the user switching gardens while the network call for
        # this mote was in flight.
        api.switch_garden("other")
        return "event-id"

    monkeypatch.setattr(ui.api.gcal_repo, "get_credentials", lambda: _FakeCredentials())
    monkeypatch.setattr(ui.api.gcal_repo, "list_managed_events", lambda credentials: {})
    monkeypatch.setattr(ui.api.gcal_repo, "push_event", fake_push_event)

    api.export_to_google_calendar()

    api.switch_garden("test")
    assert "gcal_event_id" not in api.get_mote(a["id"])["properties"]


def test_export_to_google_calendar_deletes_an_event_whose_mote_is_gone(api, monkeypatch):
    deleted = []
    monkeypatch.setattr(ui.api.gcal_repo, "get_credentials", lambda: _FakeCredentials())
    monkeypatch.setattr(
        ui.api.gcal_repo, "list_managed_events",
        lambda credentials: {"never-existed-or-deleted": "orphan-event-id"},
    )
    monkeypatch.setattr(ui.api.gcal_repo, "delete_event", lambda credentials, event_id: deleted.append(event_id))

    result = api.export_to_google_calendar()

    assert deleted == ["orphan-event-id"]
    assert result["deleted"] == 1


def test_export_to_google_calendar_deletes_an_event_whose_mote_lost_its_date(api, monkeypatch):
    m = api.create_mote({"name": "No longer dated"})  # never had a date
    deleted = []
    monkeypatch.setattr(ui.api.gcal_repo, "get_credentials", lambda: _FakeCredentials())
    monkeypatch.setattr(
        ui.api.gcal_repo, "list_managed_events",
        lambda credentials: {m["id"]: "stale-event-id"},
    )
    monkeypatch.setattr(ui.api.gcal_repo, "delete_event", lambda credentials, event_id: deleted.append(event_id))

    result = api.export_to_google_calendar()

    assert deleted == ["stale-event-id"]
    assert result["deleted"] == 1


def test_export_to_google_calendar_does_not_delete_a_still_dated_motes_event(api, monkeypatch):
    m = api.create_mote({"name": "Still dated", "date": "2026-08-15T09:00:00"})
    deleted = []
    monkeypatch.setattr(ui.api.gcal_repo, "get_credentials", lambda: _FakeCredentials())
    monkeypatch.setattr(
        ui.api.gcal_repo, "list_managed_events",
        lambda credentials: {m["id"]: "current-event-id"},
    )
    monkeypatch.setattr(ui.api.gcal_repo, "delete_event", lambda credentials, event_id: deleted.append(event_id))
    monkeypatch.setattr(ui.api.gcal_repo, "push_event", lambda credentials, payload, existing_event_id: "current-event-id")

    result = api.export_to_google_calendar()

    assert deleted == []
    assert result["deleted"] == 0


def test_reserved_names_returns_properties_and_relationship_types(api):
    result = api.reserved_names()
    assert "date" in result["properties"]
    assert "url" in result["properties"]
    assert "instance_of" in result["relationshipTypes"]
    assert result["properties"] == sorted(result["properties"]), "should already be sorted for the frontend"


# ---- time-based simulation ----

def _seed_compounding_rule(api):
    """Mirrors tests/test_simulation.py's `compounding` fixture, but
    through the JS-facing Api rather than core.simulation directly."""
    species = api.create_mote({"name": "Account", "unknowns": "balance"})
    rule = api.create_mote({"name": "Growth", "equation": "balance == prev(balance) * 1.05"})
    api.create_relationship(rule["id"], species["id"], "applies_to")
    mote = api.create_mote({"name": "acct1", "balance": 100.0})
    api.set_species(mote["id"], species["id"])
    return mote["id"]


def test_preview_simulation_returns_a_step_per_requested_count(api):
    _seed_compounding_rule(api)
    result = api.preview_simulation(4)
    assert result["failed_at"] is None
    assert result["error"] is None
    assert result["run_id"] is None
    assert [s["step"] for s in result["steps"]] == [0, 1, 2, 3]
    assert result["steps"][0]["changes"][0]["new_value"] == pytest.approx(105.0)


def test_preview_simulation_never_writes(api):
    mote_id = _seed_compounding_rule(api)
    before = api.get_mote(mote_id)
    api.preview_simulation(10)
    assert api.get_mote(mote_id) == before


def test_commit_simulation_writes_the_final_step_and_returns_a_run_id(api):
    mote_id = _seed_compounding_rule(api)
    result = api.commit_simulation(3)
    assert result["run_id"] is not None
    assert api.get_mote(mote_id)["properties"]["balance"] == pytest.approx(115.7625)


def test_preview_simulation_reports_a_failure_without_raising(api):
    species = api.create_mote({"name": "S", "unknowns": "a, b"})
    rule = api.create_mote({"name": "underdetermined", "equation": "a + b == 10"})
    api.create_relationship(rule["id"], species["id"], "applies_to")
    mote = api.create_mote({"name": "m"})
    api.set_species(mote["id"], species["id"])

    result = api.preview_simulation(5)
    assert result["failed_at"] == 0
    assert "underdetermined" in result["error"]
    assert result["steps"] == []


# ---- run history: list_runs / get_run / run_changes ----

def test_list_runs_and_get_run_see_a_committed_run(api):
    mote_id = _seed_compounding_rule(api)
    result = api.commit_simulation(3)

    runs = api.list_runs()
    assert [r["run_id"] for r in runs] == [result["run_id"]]
    assert runs[0]["status"] == "ok"

    run = api.get_run(result["run_id"])
    assert run == runs[0]

    changes = api.run_changes(result["run_id"])
    assert any(c["mote_id"] == mote_id and c["key"] == "balance" for c in changes)


def test_list_runs_respects_limit(api):
    _seed_compounding_rule(api)
    api.commit_simulation(1)
    api.commit_simulation(1)
    assert len(api.list_runs(limit=1)) == 1


def test_get_run_returns_none_for_an_unknown_id(api):
    assert api.get_run("nonexistent") is None


def test_list_runs_includes_a_failed_run_with_no_changes(api):
    species = api.create_mote({"name": "S", "unknowns": "a, b"})
    rule = api.create_mote({"name": "underdetermined", "equation": "a + b == 10"})
    api.create_relationship(rule["id"], species["id"], "applies_to")
    mote = api.create_mote({"name": "m"})
    api.set_species(mote["id"], species["id"])

    # run_simulation (unlike commit_simulation/run_time_series, which never
    # logs a run that fails on its very first step) always logs failures --
    # exercised directly here since Api has no bare run_simulation wrapper.
    with pytest.raises(SimulationError):
        run_simulation(api._conn)

    [run] = api.list_runs()
    assert run["status"] == "error"
    assert "underdetermined" in run["detail"]
    assert api.run_changes(run["run_id"]) == []


# ---- saving a simulation without applying it ----

def test_save_simulation_logs_without_writing_the_graph(api):
    mote_id = _seed_compounding_rule(api)
    before = api.get_mote(mote_id)

    result = api.save_simulation(3)

    assert result["run_id"] is not None
    assert api.get_mote(mote_id) == before  # graph untouched

    run = api.get_run(result["run_id"])
    assert run["committed"] is False

    ticks = api.run_ticks(result["run_id"])
    assert [s["step"] for s in ticks["steps"]] == [0, 1, 2]


def test_committed_run_has_committed_true(api):
    _seed_compounding_rule(api)
    result = api.commit_simulation(1)
    run = api.get_run(result["run_id"])
    assert run["committed"] is True


# ---- entering a run: run_ticks ----

def test_run_ticks_backs_a_multi_tick_scrub_session(api):
    mote_id = _seed_compounding_rule(api)
    result = api.commit_simulation(3)

    ticks = api.run_ticks(result["run_id"])
    steps = ticks["steps"]
    assert [s["step"] for s in steps] == [0, 1, 2]
    balances = [
        next(c["new_value"] for c in s["changes"] if c["mote_id"] == mote_id and c["key"] == "balance")
        for s in steps
    ]
    assert balances == pytest.approx([105.0, 110.25, 115.7625])


def test_run_ticks_for_a_one_step_run_has_one_tick(api):
    _seed_compounding_rule(api)
    result = api.commit_simulation(1)
    ticks = api.run_ticks(result["run_id"])
    assert [s["step"] for s in ticks["steps"]] == [0]


# ---- sparklines: mote_history ----

def test_mote_history_tracks_a_property_across_committed_runs(api):
    mote_id = _seed_compounding_rule(api)
    api.commit_simulation(1)
    api.commit_simulation(1)

    history = api.mote_history(mote_id)
    assert history["balance"] == pytest.approx([105.0, 110.25])


def test_mote_history_excludes_saved_runs(api):
    mote_id = _seed_compounding_rule(api)
    api.save_simulation(2)
    assert api.mote_history(mote_id) == {}


def test_reserved_names_returns_the_full_reference_not_just_names(api):
    """The app used to get a bare name list while an MCP client got the
    meanings, the equation language and every provider's operations --
    the surface with a person behind it was the less informed one."""
    reserved = api.reserved_names()

    assert "in_folder" in reserved["relationshipTypes"]
    assert "provider" in reserved["properties"]
    # ...and now what they mean, for the detail panel's tooltips.
    assert "provider" in reserved["propertyMeanings"]
    assert "in_folder" in reserved["relationshipTypeMeanings"]
    # ...plus what each provider can compute, for the property-model note.
    assert set(reserved["propertyModelProviders"]) == {"ideal_gas", "coolprop"}
    ideal_gas = reserved["propertyModelProviders"]["ideal_gas"]
    assert any(c["operation"] == "pressure" for c in ideal_gas["capabilities"])

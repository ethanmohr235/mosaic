import json
import webbrowser

import pytest

import ui.api
from core.gardens import create_garden
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
    # The UI drives "New instance" and the species legend off species_ids,
    # so a species set up with a template but no instances yet has to
    # register -- otherwise there is no way to create its first instance.
    species = api.create_mote({"name": "Resistor"})
    template = api.create_mote({"name": "R", "resistance": "100"})
    api.set_template(template["id"], species["id"])

    result = api.focus(template["id"], None)
    assert result["species_ids"] == [species["id"]]


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


def test_export_garden_by_name_does_not_disturb_the_current_garden(api, tmp_path):
    create_garden("other")
    other_api = Api("other")
    other_api.create_mote({"name": "OtherMote"})
    other_api.shutdown()

    api.create_mote({"name": "CurrentMote"})
    out = tmp_path / "other.json"
    api._window = _StubWindow(str(out))

    result = api.export_garden("other")

    assert result["path"] == str(out)
    assert api.current_garden() == "test"
    data = json.loads(out.read_text())
    names = [m["properties"].get("name") for m in data["motes"]]
    assert names == ["OtherMote"]


def test_export_garden_defaults_to_the_current_garden(api, tmp_path):
    api.create_mote({"name": "CurrentMote"})
    out = tmp_path / "test.json"
    api._window = _StubWindow(str(out))

    api.export_garden()

    data = json.loads(out.read_text())
    names = [m["properties"].get("name") for m in data["motes"]]
    assert names == ["CurrentMote"]


def test_whole_garden_still_reports_species_ids(api):
    species = api.create_mote({"name": "Node"})
    instance = api.create_mote({"name": "N1"})
    api.set_species(instance["id"], species["id"])

    result = api.focus(instance["id"], None)
    assert result["species_ids"] == [species["id"]]


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
    monkeypatch.setattr(ui.api.gcal_repo, "push_event", fake_push_event)

    result = api.export_to_google_calendar()

    assert result == {"created": 1, "updated": 0, "skipped": 0, "total": 1}
    assert pushed == [("Exam", None)]
    assert api.get_mote(a["id"])["properties"]["gcal_event_id"] == "event-for-Exam"
    assert "gcal_event_id" not in api.get_mote(b["id"])["properties"]


def test_export_to_google_calendar_reuses_the_existing_event_id_on_a_second_run(api, monkeypatch):
    a = api.create_mote({"name": "Exam", "date": "2026-08-15T09:00:00"})
    calls = []

    def fake_push_event(credentials, payload, existing_event_id):
        calls.append(existing_event_id)
        return "same-event-id"

    monkeypatch.setattr(ui.api.gcal_repo, "get_credentials", lambda: _FakeCredentials())
    monkeypatch.setattr(ui.api.gcal_repo, "push_event", fake_push_event)

    first = api.export_to_google_calendar()
    second = api.export_to_google_calendar()

    assert first == {"created": 1, "updated": 0, "skipped": 0, "total": 1}
    assert second == {"created": 0, "updated": 1, "skipped": 0, "total": 1}
    assert calls == [None, "same-event-id"]
    assert api.get_mote(a["id"])["properties"]["gcal_event_id"] == "same-event-id"


def test_export_to_google_calendar_skips_a_mote_with_an_unparseable_date(api, monkeypatch):
    api.create_mote({"name": "Bad", "date": "not-a-date"})
    calls = []
    monkeypatch.setattr(ui.api.gcal_repo, "get_credentials", lambda: _FakeCredentials())
    monkeypatch.setattr(ui.api.gcal_repo, "push_event", lambda *a, **k: calls.append(1))

    result = api.export_to_google_calendar()

    assert result == {"created": 0, "updated": 0, "skipped": 1, "total": 1}
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
    monkeypatch.setattr(ui.api.gcal_repo, "push_event", fake_push_event)

    api.export_to_google_calendar()

    api.switch_garden("test")
    assert "gcal_event_id" not in api.get_mote(a["id"])["properties"]

import json

import pytest

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

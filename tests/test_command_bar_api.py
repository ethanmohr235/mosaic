"""The bridge contract the shared "/" command bar speaks -- mirrors
tests/test_inspector_api.py's shape for the same reason: api_common.
CommandBarApi is mixed into every window's Api, so `command_run` behaves
the same wherever it runs."""
import pytest

import monarch.api
from api_common import CommandBarApi
from core.gardens import create_garden


@pytest.fixture
def api():
    create_garden("test")
    instance = monarch.api.Api("test")
    yield instance
    instance.shutdown()


def test_sets_a_property_and_creates_the_mote(api):
    result = api.command_run("R1.resistance = 500 ohms")
    assert result["ok"]
    mote = api.inspect_mote(result["moteId"])
    assert mote["properties"]["resistance"] == 500
    assert mote["properties"]["units"] == {"resistance": "ohm"}


def test_ambiguous_name_reports_choices(api):
    api.create_mote({"name": "R1"})
    api.create_mote({"name": "R1"})
    result = api.command_run("R1.resistance = 500 ohms")
    assert not result["ok"]
    assert len(result["choices"]) == 2


def test_suggest_completes_mote_and_property_names(api):
    api.command_run("R1.resistance = 500 ohms")
    assert api.command_suggest("R") == ["R1"]
    assert api.command_suggest("R1.res") == ["resistance"]


def test_delete_a_mote_needs_confirmation(api):
    api.command_run("R1.resistance = 500 ohms")
    first = api.command_run("delete R1")
    assert not first["ok"] and first["needsConfirm"]
    second = api.command_run("delete R1", confirm=True)
    assert second["ok"]


@pytest.mark.parametrize("module", [
    monarch.api,
])
def test_every_window_speaks_the_contract(module):
    assert issubclass(module.Api, CommandBarApi)
    assert callable(getattr(module.Api, "command_run", None))

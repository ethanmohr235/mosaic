"""The bridge contract the shared "/" command bar speaks -- mirrors
tests/test_inspector_api.py's shape for the same reason: api_common.
CommandBarApi is mixed into every window's Api, so `command_run` behaves
the same wherever it runs."""
import pytest

import monarch.api
import monastery.api
import monolith.api
import mora.api
import motion.api
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
    monarch.api, motion.api, monolith.api, mora.api, monastery.api,
])
def test_every_window_speaks_the_contract(module):
    assert issubclass(module.Api, CommandBarApi)
    assert callable(getattr(module.Api, "command_run", None))


def test_motion_overrides_command_run_for_scenarios():
    """Motion's override has to replace the shared method, not sit beside
    it, or a selected scenario would still let a command land on the
    garden's own state instead of the override."""
    assert motion.api.Api.command_run is not CommandBarApi.command_run
    assert monarch.api.Api.command_run is CommandBarApi.command_run


def test_motion_blocks_the_command_bar_while_a_scenario_is_active(tmp_path):
    create_garden("motion-test")
    instance = motion.api.Api("motion-test")
    try:
        made = instance.command_run("R1.resistance = 500 ohms")
        assert made["ok"]
        scenario = instance.create_mote({"name": "Scenario A"})["id"]
        instance.set_inspector_scenario(scenario)
        blocked = instance.command_run("R1.resistance = 700 ohms")
        assert not blocked["ok"]
        assert "scenario" in blocked["message"]

        instance.set_inspector_scenario(None)
        allowed = instance.command_run("R1.resistance = 700 ohms")
        assert allowed["ok"]
    finally:
        instance.shutdown()

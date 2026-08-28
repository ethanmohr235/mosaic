"""Monastery's JS bridge.

What earns a test here is behavior this layer adds -- which bindings a
window with no preference starts on, what a new mote's date is written
as, that templates and overrides stay off the grid, and the payload shape
the frontend is written against. core/calendar.py's own reading is tested
in tests/test_calendar.py and not re-asserted here. Same line
tests/test_mora_api.py draws.
"""
import pytest

from core.db import get_connection, initialize_database
from core.gardens import create_garden, set_sync_dir
from core.motes import create_mote, get_mote, update_mote
from core.relationships import create_relationship
from core.species import set_species
from monastery.api import Api
from monastery.app import DEFAULT_HTTP_PORT
from window_launcher import choose_port

WEEK_START = "2026-08-24T00:00:00"
WEEK_END = "2026-08-31T00:00:00"


@pytest.fixture
def api():
    create_garden("test")
    instance = Api("test")
    yield instance
    instance.shutdown()


@pytest.fixture
def week(api):
    """A garden whose motes are dated three different ways, which is the
    case Monastery exists for: a one-key reader would show only one of
    these."""
    conn = api._conn
    species = create_mote(conn, {"name": "Meeting"})
    standup = create_mote(conn, {"name": "Standup", "when": "2026-08-24 09:15/PT20M"})
    create_relationship(conn, standup, species, "instance_of")
    create_mote(conn, {"name": "Runbook rewrite", "due": "2026-08-28"})
    create_mote(conn, {"name": "Read-only window", "starts": "2026-08-26/2026-08-28"})
    create_mote(conn, {"name": "Rina Kovac", "role": "engineer"})
    return {"species": species, "standup": standup}


def _calendar(api, bindings=None):
    return api.calendar(WEEK_START, WEEK_END, bindings)


# ---- reading ----

def test_a_window_with_no_preference_starts_on_every_binding(api, week):
    answer = _calendar(api)
    assert {b["key"] for b in answer["bindings"]} == {"when", "due", "starts"}
    assert all(b["active"] for b in answer["bindings"])
    assert {o["name"] for o in answer["occurrences"]} == {
        "Standup", "Runbook rewrite", "Read-only window",
    }


def test_bindings_come_back_most_used_first(api, week):
    conn = api._conn
    create_mote(conn, {"name": "Second due", "due": "2026-08-25"})
    keys = [b["key"] for b in _calendar(api)["bindings"]]
    assert keys[0] == "due"


def test_a_binding_the_frontend_leaves_out_places_nothing(api, week):
    answer = _calendar(api, ["due"])
    assert {o["name"] for o in answer["occurrences"]} == {"Runbook rewrite"}
    # Every binding is still *offered*, so the sidebar can turn it back on.
    assert {b["key"] for b in answer["bindings"]} == {"when", "due", "starts"}
    assert [b["key"] for b in answer["bindings"] if b["active"]] == ["due"]


def test_an_occurrence_carries_the_binding_that_placed_it(api, week):
    answer = _calendar(api, ["when"])
    assert answer["occurrences"][0]["binding"] == "when"


def test_an_occurrence_is_coloured_by_the_species_the_canvas_uses(api, week):
    standup = next(o for o in _calendar(api)["occurrences"] if o["name"] == "Standup")
    assert standup["speciesId"] == week["species"]
    assert standup["speciesName"] == "Meeting"


def test_a_recurring_mote_expands_but_stays_one_mote(api):
    mote_id = create_mote(api._conn, {
        "name": "Daily", "date": "2026-08-24T09:00:00\nRRULE:FREQ=DAILY",
    })
    occurrences = _calendar(api)["occurrences"]
    assert len(occurrences) == 7
    assert {o["id"] for o in occurrences} == {mote_id}
    assert len({o["key"] for o in occurrences}) == 7, "each occurrence needs its own DOM key"


def test_a_template_is_never_placed(api, week):
    """Templates hold starting values, not real data -- the same
    exclusion Calendar export applies."""
    conn = api._conn
    template = create_mote(conn, {"name": "Meeting template", "when": "2026-08-25 10:00"})
    create_relationship(conn, template, week["species"], "template_of")
    assert "Meeting template" not in {o["name"] for o in _calendar(api)["occurrences"]}


def test_a_scenario_override_is_never_placed(api, week):
    conn = api._conn
    override = create_mote(conn, {"name": "Winter override", "when": "2026-08-25 10:00"})
    create_relationship(conn, override, week["standup"], "overrides")
    assert "Winter override" not in {o["name"] for o in _calendar(api)["occurrences"]}


def test_the_species_list_backs_the_draft_picker(api, week):
    assert [s["name"] for s in _calendar(api)["species"]] == ["Meeting"]


def test_the_status_bar_counts_motes_with_no_date_at_all(api, week):
    """A garden-level fact, which is why it comes from the bridge -- the
    frontend only ever sees one window's worth of occurrences."""
    assert _calendar(api)["stats"]["undated"] == 2, "the person, and the species mote itself"


def test_a_mote_outside_the_window_is_not_placed(api, week):
    create_mote(api._conn, {"name": "Far future", "due": "2027-01-01"})
    assert "Far future" not in {o["name"] for o in _calendar(api)["occurrences"]}


# ---- the detail panel ----

def test_focus_returns_relationships_with_their_direction(api, week):
    panel = api.focus(week["standup"])
    assert panel["name"] == "Standup"
    assert [r["type"] for r in panel["relationships"]] == ["instance_of"]
    assert panel["relationships"][0]["outgoing"] is True


def test_focus_marks_which_properties_are_the_calendars_own(api, week):
    """By *value*, not by key. There is no list of calendar property
    names left to check against -- a key holding an interval is the
    calendar's under whatever name the garden gave it, which is why
    `when` is marked here and a reserved name is not what marks it."""
    panel = api.focus(week["standup"])
    assert set(panel["calendarKeys"]) == {"when"}


def test_focus_on_a_missing_mote_returns_nothing(api):
    assert api.focus("nope") is None


# ---- creating ----

def test_a_new_mote_is_dated_with_the_reserved_key(api, week):
    """Reading is deliberately general, but a *new* date has one right
    home -- the key Calendar export already uses."""
    created = api.create_mote({"name": "Retro", "date": "2026-08-27T15:00:00", "duration": "45"})
    assert created["properties"]["date"] == "2026-08-27T15:00:00"
    assert created["properties"]["duration"] == 45, "a typed number is stored as one"


def test_a_new_mote_can_be_declared_an_instance(api, week):
    created = api.create_mote({"name": "Retro", "date": "2026-08-27"}, week["species"])
    assert [r["type"] for r in created["relationships"]] == ["instance_of"]
    assert created["speciesName"] == "Meeting"


def test_a_new_mote_shows_up_on_the_next_read(api, week):
    api.create_mote({"name": "Retro", "date": "2026-08-27T15:00:00"})
    assert "Retro" in {o["name"] for o in _calendar(api)["occurrences"]}


def test_updating_a_date_moves_the_mote(api, week):
    api.update_mote(week["standup"], {"when": "2026-08-26 09:15/PT20M"})
    standup = next(o for o in _calendar(api)["occurrences"] if o["name"] == "Standup")
    assert standup["start"].startswith("2026-08-26")


def test_deleting_a_date_property_unplaces_the_mote(api, week):
    api.delete_property(week["standup"], "when")
    assert "Standup" not in {o["name"] for o in _calendar(api)["occurrences"]}


def test_delete_mote_removes_it(api, week):
    assert api.delete_mote(week["standup"]) is True
    assert get_mote(api._conn, week["standup"]) is None


# ---- editing an occurrence ----
#
# A rectangle on the grid is a placement, not a mote: one mote can draw
# fifty of them. What earns a test here is the choice this layer makes
# between the two -- what "the whole series" writes, what "just this one"
# splits off, and that undo puts back what was there.


@pytest.fixture
def series(api):
    """A weekly standup: four Mondays in the garden, one mote."""
    return create_mote(api._conn, {
        "name": "Standup",
        "when": "2026-08-24 09:15/PT20M\nRRULE:FREQ=WEEKLY;BYDAY=MO;COUNT=4",
    })


def test_a_move_writes_the_binding_that_placed_it(api, week):
    api.place_occurrence(
        week["standup"], "when", "2026-08-24T09:15:00", "2026-08-26T11:00:00", "2026-08-26T11:20:00",
    )
    properties = get_mote(api._conn, week["standup"]).properties
    assert properties["when"] == "2026-08-26 11:00/PT20M"
    assert "date" not in properties, "a drag edits what placed it, not a second key"


def test_moving_a_series_shifts_every_occurrence(api, series):
    api.place_occurrence(
        series, "when", "2026-08-31T09:15:00", "2026-09-01T09:15:00", "2026-09-01T09:35:00",
    )
    starts = [o["start"] for o in _calendar(api)["occurrences"]]
    assert starts == ["2026-08-25T09:15:00"], "the whole series moved to Tuesdays"
    assert get_mote(api._conn, series).properties["when"] == \
        "2026-08-25 09:15/PT20M\nRRULE:FREQ=WEEKLY;BYDAY=TU;COUNT=4"


def test_moving_one_occurrence_splits_it_off_as_its_own_mote(api, series, week):
    set_species(api._conn, series, week["species"])

    panel = api.place_occurrence(
        series, "when", "2026-08-31T09:15:00", "2026-09-01T11:00:00", "2026-09-01T11:20:00",
        scope="occurrence",
    )
    assert panel["id"] != series, "the moved occurrence is a mote of its own now"
    assert panel["properties"]["when"] == "2026-09-01 11:00/PT20M", \
        "it lands where it was dropped, keeping the span it was drawn with"
    assert "RRULE" not in panel["properties"]["when"], "one event, not a second series"
    assert panel["speciesName"] == "Meeting", "and the colour it was drawn in"
    assert "EXDATE:20260831T091500" in get_mote(api._conn, series).properties["when"]


def test_a_resize_writes_the_span_into_the_bindings_own_value(api, week):
    api.place_occurrence(
        week["standup"], "when", "2026-08-24T09:15:00", "2026-08-24T09:15:00", "2026-08-24T10:15:00",
    )
    assert get_mote(api._conn, week["standup"]).properties["when"] == "2026-08-24 09:15/PT1H"


def test_a_resize_of_a_ranged_mote_moves_its_end(api, week):
    mote_id = next(
        o["id"] for o in _calendar(api)["occurrences"] if o["name"] == "Read-only window"
    )
    api.place_occurrence(
        mote_id, "starts", "2026-08-26T00:00:00", "2026-08-26T00:00:00", "2026-08-30T00:00:00",
    )
    assert get_mote(api._conn, mote_id).properties["starts"] == "2026-08-26/2026-08-30"


def test_a_move_that_keeps_its_length_does_not_invent_a_span(api, week):
    """A mote that never declared how long it lasts should not acquire a
    span just because you dragged it an hour later."""
    mote_id = create_mote(api._conn, {"name": "Sync", "when": "2026-08-25 14:00"})
    api.place_occurrence(mote_id, "when", "2026-08-25T14:00:00", "2026-08-25T15:00:00", "2026-08-25T16:00:00")
    assert get_mote(api._conn, mote_id).properties["when"] == "2026-08-25 15:00"


def test_dragging_the_top_edge_moves_the_start_and_keeps_the_end(api, week):
    """One gesture, one write: the top edge is a move and a resize at
    once, which is why there is one method rather than two."""
    api.place_occurrence(
        week["standup"], "when", "2026-08-24T09:15:00", "2026-08-24T09:00:00", "2026-08-24T09:35:00",
    )
    properties = get_mote(api._conn, week["standup"]).properties
    assert properties["when"] == "2026-08-24 09:00/PT35M"


def test_deleting_one_occurrence_leaves_the_series_standing(api, series):
    answer = api.delete_occurrence(series, "when", "2026-08-31T09:15:00", scope="occurrence")
    assert answer["scope"] == "occurrence"
    assert get_mote(api._conn, series) is not None
    assert [o["start"] for o in _calendar(api, ["when"])["occurrences"]] == ["2026-08-24T09:15:00"]


def test_deleting_a_series_deletes_the_mote(api, series):
    answer = api.delete_occurrence(series, "when", "2026-08-24T09:15:00")
    assert answer["scope"] == "series"
    assert get_mote(api._conn, series) is None


def test_deleting_the_only_placement_of_a_plain_mote_deletes_it(api, week):
    """A mote that does not recur *is* its placement, so "just this one"
    can only mean the mote."""
    api.delete_occurrence(week["standup"], "when", "2026-08-24T09:15:00", scope="occurrence")
    assert get_mote(api._conn, week["standup"]) is None


def test_undo_puts_back_a_deleted_occurrence(api, series):
    answer = api.delete_occurrence(series, "when", "2026-08-31T09:15:00", scope="occurrence")
    api.undo_delete(answer["undo"])
    assert get_mote(api._conn, series).properties["when"] == \
        "2026-08-24 09:15/PT20M\nRRULE:FREQ=WEEKLY;BYDAY=MO;COUNT=4"


def test_undo_puts_back_a_deleted_mote_with_its_id_and_arrows(api, week):
    answer = api.delete_occurrence(week["standup"], "when", "2026-08-24T09:15:00")
    panel = api.undo_delete(answer["undo"])
    assert panel["id"] == week["standup"], "the same mote, not a lookalike"
    assert panel["properties"]["when"] == "2026-08-24 09:15/PT20M"
    assert [r["type"] for r in panel["relationships"]] == ["instance_of"]


def test_editing_an_occurrence_of_a_vanished_mote_says_nothing_happened(api, week):
    """Another window can delete the mote between the paint and the drop
    -- Monastery polls rather than being told."""
    api.delete_mote(week["standup"])
    assert api.place_occurrence(
        week["standup"], "when", "2026-08-24T09:15:00", "2026-08-26T09:15:00", "2026-08-26T09:35:00"
    ) is None


# ---- gardens ----

def test_switch_garden_moves_the_connection(api, week):
    create_garden("other")
    assert api.switch_garden("other") == {"garden": "other"}
    assert api.current_garden() == "other"
    assert _calendar(api)["occurrences"] == []


# ---- app switcher (⌘J) ----
#
# The command-building itself is api_common.open_app's own test (see
# tests/test_api_common.py); what's worth checking here is only that
# this app's method actually delegates to it with this app's garden.

def test_open_app_delegates_to_api_common(api, monkeypatch):
    import api_common

    calls = []
    monkeypatch.setattr(
        api_common, "popen_detached", lambda args, **kwargs: calls.append(args)
    )
    assert api.open_app("mora") == {"app": "mora", "garden": "test", "focus": None}
    assert calls[0][2:] == ["--garden", "test"]


# ---- best-effort sync ----
#
# run_monastery.py is its own launch path, not only ever opened from the
# graph window -- so a garden pointed at a sync folder must converge from
# a Monastery-only session too, exactly as Mora's does.

def test_opening_monastery_pushes_pending_local_changes(tmp_path):
    create_garden("synced")
    conn = get_connection("synced")
    initialize_database(conn)
    create_mote(conn, {"name": "written before Monastery opened"})
    conn.close()

    sync_dir = tmp_path / "shared"
    set_sync_dir("synced", str(sync_dir))

    instance = Api("synced")
    instance.shutdown()

    assert any((sync_dir / "synced" / "ops").glob("*.jsonl"))


# ---- manual refresh ----

def test_refresh_skips_sync_when_not_configured(api):
    assert api.refresh() == {"synced": False}


def test_refresh_syncs_when_configured(api, tmp_path):
    set_sync_dir("test", str(tmp_path))
    create_mote(api._conn, {"name": "Alice"})
    result = api.refresh()
    assert result["synced"] is True
    assert result["push"]["pushed"] >= 1


# ---- the window's own port ----
#
# Monarch takes pywebview's default 42001 and Mora takes 42002.
# A third app asking for either is not a near miss: the loser fails to
# bind but still points its window at that address, loading the winner's
# page against its own bridge. That shipped once already (mora/app.py).

def test_monastery_does_not_ask_for_another_windows_port():
    assert DEFAULT_HTTP_PORT not in (42001, 42002)


def test_the_preferred_port_is_used_when_it_is_free():
    port = _a_free_port()
    assert choose_port("Monastery", port) == port


def test_a_taken_port_falls_back_instead_of_colliding():
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as held:
        held.bind(("127.0.0.1", 0))
        held.listen(1)
        taken = held.getsockname()[1]
        chosen = choose_port("Monastery", taken)
        assert chosen != taken
        assert 1024 <= chosen <= 65535


def _a_free_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]

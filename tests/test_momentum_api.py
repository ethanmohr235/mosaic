"""Momentum's JS bridge.

What earns a test here is behavior this layer adds -- which key a window
with no preference reads as status, what dropping a task on a day does
to its properties, that a logged session is an ordinary mote, and the
payload shape the frontend is written against. core/calendar.py's
reading and core/task_candidates.py's discovery are tested in
tests/test_calendar.py and tests/test_task_candidates.py and not
re-asserted here. Same line tests/test_monastery_api.py draws.
"""
import pytest

from core.db import get_connection
from core.gardens import create_garden
from core.motes import create_mote, get_mote
from core.relationships import create_relationship
from core.species import set_species
from momentum.api import Api
from momentum.app import DEFAULT_HTTP_PORT
from window_launcher import choose_port

WEEK_START = "2026-08-24T00:00:00"
WEEK_END = "2026-08-31T00:00:00"


@pytest.fixture
def api():
    create_garden("test-momentum")
    instance = Api("test-momentum")
    yield instance
    instance.shutdown()


@pytest.fixture
def board(api):
    """A garden whose tasks say `status`, `estimate` and `priority`, with
    a meeting already on the calendar -- the case Momentum exists for."""
    conn = api._conn
    person = create_mote(conn, {"name": "Dev Anand", "role": "Data engineer"})
    schema = create_mote(conn, {
        "name": "Schema translation", "status": "doing", "estimate": "5h",
        "priority": "high",
        "subtasks": [{"text": "Map fields", "done": True}, {"text": "Review", "done": False}],
    })
    backfill = create_mote(conn, {"name": "Backfill dry run", "status": "todo", "estimate": "3h"})
    create_mote(conn, {"name": "Sign-off", "status": "done", "estimate": "1h"})
    create_relationship(conn, schema, person, "assigned_to")
    standup = create_mote(conn, {"name": "Standup", "date": "2026-08-26 09:00/PT30M"})
    return {"person": person, "schema": schema, "backfill": backfill, "standup": standup}


# ---- which key means what ----

def test_a_window_with_no_preference_discovers_its_keys(api, board):
    """No reserved names: the keys come from what the garden actually
    holds, the same way Monastery discovers its date bindings."""
    keys = api.board()["keys"]
    assert keys["status"] == "status"
    assert keys["priority"] == "priority"
    assert keys["estimate"] == "estimate"


def test_a_garden_that_names_it_something_else_still_gets_a_board(api):
    conn = api._conn
    create_mote(conn, {"name": "One", "stage": "icebox"})
    create_mote(conn, {"name": "Two", "stage": "shipped"})
    answer = api.board()
    assert answer["keys"]["status"] == "stage"
    assert {c["label"] for c in answer["columns"]} == {"icebox", "shipped"}


def test_the_window_can_override_the_guess(api, board):
    """`False` is how the frontend says "switched off", which None
    cannot express -- None means "you decide"."""
    assert api.board(status_key="priority")["keys"]["status"] == "priority"
    assert api.board(status_key=False)["keys"]["status"] is None


def test_every_candidate_is_still_offered_so_the_sidebar_can_switch(api, board):
    offered = {c["key"] for c in api.board()["candidates"]["enum"]}
    assert {"status", "priority"} <= offered


# ---- the board ----

def test_motes_the_status_key_says_nothing_about_still_get_a_column(api, board):
    """A board that silently dropped them would be hiding work."""
    columns = {c["label"]: [x["name"] for x in c["cards"]] for c in api.board()["columns"]}
    assert "Dev Anand" in columns["no status"]
    assert columns["doing"] == ["Schema translation"]


def test_a_column_carries_its_own_load(api, board):
    columns = {c["label"]: c["minutes"] for c in api.board()["columns"]}
    assert columns["doing"] == 300, "5h"
    assert columns["todo"] == 180, "3h"


def test_a_card_carries_its_owner_as_a_real_relationship(api, board):
    card = _card(api.board(), "Schema translation")
    assert [o["name"] for o in card["owners"]] == ["Dev Anand"]


def test_a_card_carries_its_subtask_progress(api, board):
    card = _card(api.board(), "Schema translation")
    assert card["subtasksDone"] == 1
    assert len(card["subtasks"]) == 2


def test_a_template_is_never_a_task(api, board):
    conn = api._conn
    species = create_mote(conn, {"name": "Task"})
    set_species(conn, board["schema"], species)
    template = create_mote(conn, {"name": "Task template", "status": "todo"})
    create_relationship(conn, template, species, "template_of")
    assert "Task template" not in _names(api.board())


def test_a_species_is_never_a_task(api, board):
    """A species declares what a kind of thing is. `Work Session` is a
    type, not an afternoon's work -- and this window creates that mote
    itself, so leaving it in would mean Momentum boarding its own
    plumbing."""
    api.log_session(board["schema"], 25)
    assert "Work Session" not in _names(api.board())


# ---- scheduling ----

def test_dropping_a_task_on_a_day_finds_the_earliest_free_slot(api, board):
    """The standup holds 09:00-09:30, so a task dropped on that day
    starts when the standup ends -- read through the same occurrences
    Monastery draws."""
    answer = api.schedule(board["backfill"], "2026-08-26")
    assert answer["start"] == "2026-08-26T09:30:00"
    assert answer["slotFull"] is False


def test_scheduling_writes_the_key_the_garden_already_uses(api, board):
    api.schedule(board["backfill"], "2026-08-26")
    properties = get_mote(api._conn, board["backfill"]).properties
    assert properties["date"] == "2026-08-26T09:30/PT3H", "the garden's dominant binding"


def test_scheduling_reuses_the_binding_a_task_already_carries(api):
    """An edit should move the date you can see, not leave it standing
    and add a second one beside it."""
    conn = api._conn
    task = create_mote(conn, {"name": "Runbook", "due": "2026-08-28 14:00"})
    api.schedule(task, "2026-08-27")
    properties = get_mote(conn, task).properties
    assert properties["due"].startswith("2026-08-27")
    assert "date" not in properties


def test_an_estimate_becomes_the_span_the_calendar_draws(api, board):
    """One write to one key: the span rides inside the value, so
    scheduling no longer has to decide whether it may also touch a
    separate `duration`."""
    api.schedule(board["backfill"], "2026-08-27")
    assert get_mote(api._conn, board["backfill"]).properties["date"] == "2026-08-27T09:00/PT3H"


def test_a_rule_the_mote_already_carried_survives_scheduling(api):
    conn = api._conn
    task = create_mote(conn, {
        "name": "Weekly review", "estimate": "1h",
        "date": "2026-08-20T09:00/PT1H\nRRULE:FREQ=WEEKLY",
    })
    api.schedule(task, "2026-08-27")
    assert get_mote(conn, task).properties["date"] == \
        "2026-08-27T09:00/PT1H\nRRULE:FREQ=WEEKLY"


def test_a_full_day_still_schedules_and_says_so(api):
    """Refusing to schedule something is not this window's decision."""
    conn = api._conn
    create_mote(conn, {"name": "All day meeting", "date": "2026-08-26 09:00/PT8H"})
    task = create_mote(conn, {"name": "Squeeze", "estimate": "2h"})
    answer = api.schedule(task, "2026-08-26")
    assert answer["slotFull"] is True
    assert answer["start"] == "2026-08-26T09:00:00"


def test_working_hours_default_to_nine_to_five(api):
    days = api.days(WEEK_START, WEEK_END)
    assert days["workStart"] == "09:00"
    assert days["workEnd"] == "17:00"


def test_setting_working_hours_moves_where_a_task_lands(api):
    """The rail's earliest/latest inputs, round-tripped: a garden that
    narrows its day should see schedule() honor the new window rather
    than the 9-5 default."""
    answer = api.set_working_hours("10:00", "15:00")
    assert answer == {"workStart": "10:00", "workEnd": "15:00"}

    task = create_mote(api._conn, {"name": "Squeeze", "estimate": "1h"})
    scheduled = api.schedule(task, "2026-08-26")
    assert scheduled["start"] == "2026-08-26T10:00:00"

    days = api.days(WEEK_START, WEEK_END)
    assert days["workStart"] == "10:00"
    assert days["workEnd"] == "15:00"


def test_an_unparseable_working_hour_keeps_the_one_it_had(api):
    api.set_working_hours("10:00", "15:00")
    answer = api.set_working_hours("not-a-time", "16:00")
    assert answer == {"workStart": "10:00", "workEnd": "16:00"}


def test_unscheduling_puts_a_task_back_in_the_pool(api, board):
    api.schedule(board["backfill"], "2026-08-26")
    api.unschedule(board["backfill"])
    assert "date" not in get_mote(api._conn, board["backfill"]).properties
    assert "Backfill dry run" in {c["name"] for c in api.days(WEEK_START, WEEK_END)["pool"]}


def test_a_scheduled_task_lands_on_its_day_in_the_week(api, board):
    api.schedule(board["backfill"], "2026-08-26")
    days = {d["day"]: [c["name"] for c in d["cards"]] for d in api.days(WEEK_START, WEEK_END)["days"]}
    assert "Backfill dry run" in days["2026-08-26"]


def test_a_days_load_ignores_what_is_already_done(api):
    """A day whose work is done is not a full day."""
    conn = api._conn
    create_mote(conn, {
        "name": "Finished", "status": "done", "estimate": "4h", "date": "2026-08-26 09:00",
    })
    day = _day(api.days(WEEK_START, WEEK_END), "2026-08-26")
    assert day["minutes"] == 0
    assert [c["name"] for c in day["cards"]] == ["Finished"]


# ---- the timer ----

def test_a_logged_session_is_an_ordinary_dated_mote(api, board):
    """Which is exactly why it turns up in Monastery without either
    window knowing the other exists."""
    answer = api.log_session(board["schema"], 25)
    session = get_mote(api._conn, answer["id"])
    assert session.properties["name"] == "Schema translation"
    assert session.properties["date"].endswith("/PT25M"), \
        "an ordinary interval, which is why Monastery draws it for free"


def test_a_session_points_at_the_task_it_was_about(api, board):
    api.log_session(board["schema"], 25)
    panel = api.focus(board["schema"])
    about = [r for r in panel["relationships"] if r["type"] == "about"]
    assert len(about) == 1
    assert about[0]["outgoing"] is False


def test_a_session_shows_up_in_the_log_with_its_subject(api, board):
    api.log_session(board["schema"], 25)
    sessions = api.focus_tab()["sessions"]
    assert [(s["minutes"], s["subject"]["name"]) for s in sessions] == [(25.0, "Schema translation")]


def test_an_empty_session_is_never_written(api, board):
    assert api.log_session(board["schema"], 0)["logged"] is False


def test_a_session_logged_today_starts_a_streak(api, board):
    api.log_session(board["schema"], 25)
    assert api.focus_tab()["streak"] == 1


def test_the_focus_queue_is_what_is_scheduled_for_today(api, board):
    from datetime import date

    api.schedule(board["backfill"], date.today().isoformat())
    assert "Backfill dry run" in {c["name"] for c in api.focus_tab()["queue"]}


# ---- editing ----

def test_moving_a_card_writes_the_key_the_window_is_reading(api, board):
    api.set_status(board["backfill"], "status", "doing")
    assert get_mote(api._conn, board["backfill"]).properties["status"] == "doing"


def test_a_created_task_starts_in_the_column_it_was_made_in(api, board):
    answer = api.create_task("New thing", "status", "todo")
    assert answer["status"] == "todo"
    assert answer["name"] == "New thing"


def test_a_subtask_toggles_in_place(api, board):
    answer = api.set_subtask(board["schema"], 1, True)
    assert [s["done"] for s in answer["subtasks"]] == [True, True]


def test_a_subtask_index_out_of_range_changes_nothing(api, board):
    answer = api.set_subtask(board["schema"], 9, True)
    assert [s["done"] for s in answer["subtasks"]] == [True, False]


def test_the_panel_marks_which_properties_this_window_is_reading(api, board):
    panel = api.focus(board["schema"])
    assert set(panel["taskKeys"]) == {"status", "priority", "estimate", "subtasks"}


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
    assert api.open_app("monarch") == {"app": "monarch", "garden": "test-momentum", "focus": None}
    assert calls[0][2:] == ["--garden", "test-momentum"]


# ---- ports ----
#
# The same collision Monastery guards against: a fourth window asking
# for a port another already holds does not fail visibly -- it points at
# the winner's address and calls the wrong bridge.

def test_momentum_does_not_ask_for_another_windows_port():
    assert DEFAULT_HTTP_PORT not in (42001, 42002, 42003)


def test_the_preferred_port_is_used_when_it_is_free():
    port = _a_free_port()
    assert choose_port("Momentum", port) == port


def test_a_taken_port_falls_back_instead_of_colliding():
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as held:
        held.bind(("127.0.0.1", 0))
        held.listen(1)
        taken = held.getsockname()[1]
        chosen = choose_port("Momentum", taken)
        assert chosen != taken
        assert 1024 <= chosen <= 65535


# ---- helpers ----

def _names(answer) -> set[str]:
    return {c["name"] for column in answer["columns"] for c in column["cards"]}


def _card(answer, name) -> dict:
    for column in answer["columns"]:
        for card in column["cards"]:
            if card["name"] == name:
                return card
    raise AssertionError(f"no card named {name!r}")


def _day(answer, day) -> dict:
    return next(d for d in answer["days"] if d["day"] == day)


def _a_free_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]

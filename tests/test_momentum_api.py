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
from momentum.api import ESTIMATE_FLOOR_MINUTES, Api
from momentum.app import DEFAULT_HTTP_PORT
from window_launcher import choose_port

WEEK_START = "2026-08-24T00:00:00"
WEEK_END = "2026-08-31T00:00:00"


def _freeze_today(monkeypatch, day):
    """Pin momentum.api's notion of "today" to a day inside this file's
    fixed August week, so _roll_forward_overdue() does not read that
    week as overdue simply because real time has since moved past it --
    the tests below are about _auto_place()/scheduling, not rollover."""
    from datetime import date as real_date

    import momentum.api as momentum_api

    class _FrozenDate(real_date):
        @classmethod
        def today(cls):
            return real_date.fromisoformat(day)

    monkeypatch.setattr(momentum_api, "date", _FrozenDate)


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


def test_scheduling_only_writes_a_binding_the_board_is_showing(api, board):
    """The board is bound to `scheduled`, not the garden's dominant
    `date` -- a drop should fill in the property the board displays,
    never invent one it isn't even bound to."""
    api.schedule(board["backfill"], "2026-08-26", bindings=["scheduled"])
    properties = get_mote(api._conn, board["backfill"]).properties
    assert "scheduled" in properties
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


def test_working_hours_reject_an_end_at_or_before_the_start(api):
    """A bare `time` carries no day of its own, so "00:00" meant as
    "open until midnight" reads identically to "the start of this same
    day" once combined with a date -- an inverted window every
    gap-search downstream would silently treat as always full rather
    than raising. Regression for a real incident: this corrupted a
    garden's working hours and made every placement land at work_start
    with no real search happening at all."""
    api.set_working_hours("09:00", "17:00")
    answer = api.set_working_hours("09:00", "00:00")
    assert answer == {"workStart": "09:00", "workEnd": "17:00"}

    answer = api.set_working_hours("09:00", "09:00")
    assert answer == {"workStart": "09:00", "workEnd": "17:00"}


def test_unscheduling_puts_a_task_back_in_the_pool(api, board):
    api.schedule(board["backfill"], "2026-08-26")
    api.unschedule(board["backfill"])
    assert "date" not in get_mote(api._conn, board["backfill"]).properties
    assert "Backfill dry run" in {c["name"] for c in api.days(WEEK_START, WEEK_END)["pool"]}


def test_a_scheduled_task_lands_on_its_day_in_the_week(api, board, monkeypatch):
    _freeze_today(monkeypatch, "2026-08-24")
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


def test_a_session_points_at_the_task_it_was_for(api, board):
    api.log_session(board["schema"], 25)
    panel = api.focus(board["schema"])
    for_rel = [r for r in panel["relationships"] if r["type"] == "for"]
    assert len(for_rel) == 1
    assert for_rel[0]["outgoing"] is False


def test_a_session_shows_up_in_the_log_with_its_subject(api, board):
    api.log_session(board["schema"], 25)
    sessions = api.focus_tab()["sessions"]
    assert [(s["minutes"], s["subject"]["name"]) for s in sessions] == [(25.0, "Schema translation")]


def test_an_empty_session_is_never_written(api, board):
    assert api.log_session(board["schema"], 0)["logged"] is False


def test_a_session_logged_today_starts_a_streak(api, board):
    api.log_session(board["schema"], 25)
    assert api.focus_tab()["streak"] == 1


# ---- estimate accounting and status presets ----

def test_logging_a_session_deducts_it_from_the_estimate(api, board):
    """Schema translation carries a 5h (300m) estimate; 25 logged
    minutes are work that no longer remains to do."""
    api.log_session(board["schema"], 25)
    assert get_mote(api._conn, board["schema"]).properties["estimate"] == "275m"


def test_a_deducted_estimate_is_written_in_the_unit_it_was_read_in(api, board):
    """`5h` less an hour is `4h`, not `240` -- the courtesy a drag pays a
    date (core/interval.py::format_like), paid to an estimate."""
    api.log_session(board["schema"], 60)
    assert get_mote(api._conn, board["schema"]).properties["estimate"] == "4h"


def test_a_logged_session_does_not_push_the_estimate_below_the_floor(api):
    """A task with 20 minutes left that gets a 30 minute session is not
    yet claiming to owe -10 minutes -- it is still open, so the floor
    holds."""
    task = create_mote(api._conn, {"name": "Almost done", "estimate": 20})
    api.log_session(task, 30)
    assert get_mote(api._conn, task).properties["estimate"] == ESTIMATE_FLOOR_MINUTES


def test_completing_via_the_timer_ignores_the_floor(api):
    """The timer's own complete button is the one write allowed to push
    the estimate all the way to zero and past what a normal session
    would have floored at."""
    task = create_mote(api._conn, {"name": "Wrap up", "status": "doing", "estimate": "20"})
    api.log_session(task, 30, complete=True)
    mote = get_mote(api._conn, task)
    assert mote.properties["estimate"] == 0
    assert mote.properties["status"] == "complete"


def test_completing_a_task_with_no_timer_logs_the_remaining_estimate(api):
    """The days view's check button: whatever is left becomes one last
    logged session rather than vanishing when the task is marked done."""
    task = create_mote(api._conn, {"name": "Loose end", "status": "todo", "estimate": "45"})
    answer = api.complete_task(task)
    mote = get_mote(api._conn, task)
    assert mote.properties["estimate"] == 0
    assert mote.properties["status"] == "complete"
    sessions = api.focus_tab()["sessions"]
    assert [(s["minutes"], s["subject"]["name"]) for s in sessions] == [(45.0, "Loose end")]
    assert answer["status"] == "complete"


def test_completing_an_already_closed_out_task_logs_nothing_more(api):
    """Completing a task twice is not two closing sessions -- the first
    call already zeroed the estimate, so the second has nothing left to
    log."""
    task = create_mote(api._conn, {"name": "Loose end", "status": "todo", "estimate": "45"})
    api.complete_task(task)
    api.complete_task(task)
    assert len(api.focus_tab()["sessions"]) == 1


def test_marking_a_task_done_on_the_board_closes_out_its_estimate(api):
    """Dragging a card to a done column with no timer running means the
    same thing as the days view's check button."""
    task = create_mote(api._conn, {"name": "Shipped it", "status": "doing", "estimate": "40"})
    api.set_status(task, "status", "shipped")
    mote = get_mote(api._conn, task)
    assert mote.properties["estimate"] == 0
    assert mote.properties["status"] == "shipped", "the garden's own vocabulary is kept"
    sessions = api.focus_tab()["sessions"]
    assert [round(s["minutes"]) for s in sessions] == [40]


def test_moving_between_open_columns_never_touches_the_estimate(api):
    task = create_mote(api._conn, {"name": "Still going", "status": "todo", "estimate": "40"})
    api.set_status(task, "status", "doing")
    assert get_mote(api._conn, task).properties["estimate"] == "40"


def test_a_task_already_done_is_not_closed_out_twice(api):
    """Re-labeling an already-finished task with another done word is
    not a second closing session."""
    task = create_mote(api._conn, {"name": "Finished twice", "status": "done", "estimate": "40"})
    api.set_status(task, "status", "shipped")
    assert get_mote(api._conn, task).properties["estimate"] == "40"
    assert api.focus_tab()["sessions"] == []


# ---- auto-placement of a bare date ----

def test_a_bare_date_is_placed_in_the_earliest_free_slot_by_the_days_view(api, board, monkeypatch):
    """A mote whose date was set with nothing else -- through the MCP
    tools, say, not a drag in this window -- still lands at a real time
    once the days view is read, the same earliest gap a drag would find.
    The standup holds 09:00-09:30, so this lands right after it."""
    _freeze_today(monkeypatch, "2026-08-24")
    task = create_mote(api._conn, {
        "name": "Set elsewhere", "estimate": "1h", "date": "2026-08-26",
    })
    api.days(WEEK_START, WEEK_END)
    assert get_mote(api._conn, task).properties["date"] == "2026-08-26T09:30/PT1H"


def test_a_multi_day_all_day_span_is_left_alone(api, board):
    """An all-day value lasting more than a day is a booking someone
    declared, not an undated intention -- folding a three-day conference
    into an hour at 09:00 would delete what the value said."""
    booking = create_mote(api._conn, {
        "name": "Conference", "status": "todo", "date": "2026-08-26/2026-08-28",
    })
    api.days(WEEK_START, WEEK_END)
    assert get_mote(api._conn, booking).properties["date"] == "2026-08-26/2026-08-28"


def test_a_bare_date_outside_the_window_is_left_alone(api, board):
    """Auto-placement runs on a read, so it may only write where that
    read is looking -- a task next March is not this week's business."""
    later = create_mote(api._conn, {
        "name": "Next year", "status": "todo", "estimate": "2h", "date": "2027-03-04",
    })
    api.days(WEEK_START, WEEK_END)
    assert get_mote(api._conn, later).properties["date"] == "2027-03-04"


def test_a_finished_task_is_not_given_a_slot(api, board, monkeypatch):
    """A done task has no work left to find a gap for, and placing one
    would spend the morning's room on it -- pushing the open task that
    shares its day later down the day."""
    _freeze_today(monkeypatch, "2026-08-24")
    create_mote(api._conn, {
        "name": "Old finished", "status": "done", "estimate": "1h", "date": "2026-08-27",
    })
    open_task = create_mote(api._conn, {
        "name": "Still open", "status": "todo", "estimate": "1h", "date": "2026-08-27",
    })
    api.days(WEEK_START, WEEK_END)
    assert get_mote(api._conn, open_task).properties["date"] == "2026-08-27T09:00/PT1H"


def test_a_closing_session_is_not_back_dated_off_today(api):
    """A session is filed under the day it starts, so a fortnight-sized
    estimate must not land its closing entry on some day last week --
    crediting the streak and the heatmap for work recorded today."""
    from datetime import date

    task = create_mote(api._conn, {"name": "Long haul", "status": "todo", "estimate": "40h"})
    api.complete_task(task)
    logged = api.focus_tab()["sessions"]
    assert [s["day"] for s in logged] == [date.today().strftime("%Y-%m-%d")]


def test_a_timed_placement_is_left_alone(api, board, monkeypatch):
    """Only a bare, all-day date is auto-placed -- a task already given a
    real time (by a drag, or written by hand) keeps it."""
    _freeze_today(monkeypatch, "2026-08-24")
    api.schedule(board["backfill"], "2026-08-27")
    before = get_mote(api._conn, board["backfill"]).properties["date"]
    api.days(WEEK_START, WEEK_END)
    assert get_mote(api._conn, board["backfill"]).properties["date"] == before


def test_the_focus_queue_is_what_is_scheduled_for_today(api, board):
    from datetime import date

    api.schedule(board["backfill"], date.today().isoformat())
    assert "Backfill dry run" in {c["name"] for c in api.focus_tab()["queue"]}


# ---- rolling forward and auto-scheduling ----

def test_an_overdue_open_task_rolls_forward_to_today(api, board, monkeypatch):
    _freeze_today(monkeypatch, "2026-08-27")
    task = create_mote(api._conn, {
        "name": "Slipped", "status": "todo", "estimate": "1h", "date": "2026-08-24",
    })
    api.days(WEEK_START, WEEK_END)
    assert get_mote(api._conn, task).properties["date"].startswith("2026-08-27")


def test_a_finished_overdue_task_is_left_where_it_was(api, monkeypatch):
    _freeze_today(monkeypatch, "2026-08-27")
    task = create_mote(api._conn, {
        "name": "Already done", "status": "done", "estimate": "1h", "date": "2020-01-01",
    })
    api.days(WEEK_START, WEEK_END)
    assert get_mote(api._conn, task).properties["date"] == "2020-01-01"


def test_a_completed_tasks_scheduled_slot_is_released(api, monkeypatch):
    """A task marked done keeps whatever `scheduled` block it already
    had until something notices -- days() is what notices, freeing the
    slot the same read that rolls overdue work forward already runs
    on."""
    _freeze_today(monkeypatch, "2026-08-27")
    task = create_mote(api._conn, {
        "name": "Wrapped up", "status": "done", "estimate": 0, "scheduled": "2026-08-26 09:00/PT1H",
    })
    api.days(WEEK_START, WEEK_END)
    assert "scheduled" not in get_mote(api._conn, task).properties


def test_a_zeroed_estimate_releases_the_scheduled_slot_without_a_status(api, monkeypatch):
    """The same release, but for a garden with no status property at
    all -- a plain `0` is what a spent-down estimate looks like
    (format_estimate_like()), and reads as finished on its own. The key
    is passed explicitly the way the frontend already would once a
    board read has settled on it -- a lone zeroed task has nothing
    else to auto-discover `estimate` from, since a parsed `0` does not
    count as an estimate-shaped value on its own."""
    _freeze_today(monkeypatch, "2026-08-27")
    task = create_mote(api._conn, {
        "name": "Spent down", "estimate": 0, "scheduled": "2026-08-27 09:00/PT1H",
    })
    api.days(WEEK_START, WEEK_END, estimate_key="estimate")
    assert "scheduled" not in get_mote(api._conn, task).properties


def test_an_open_task_with_no_estimate_keeps_its_scheduled_slot(api, monkeypatch):
    """No estimate at all is not the same as a spent-down `0` -- only
    the explicit zero reads as finished."""
    _freeze_today(monkeypatch, "2026-08-27")
    task = create_mote(api._conn, {
        "name": "Still open", "status": "todo", "scheduled": "2026-08-27 09:00/PT1H",
    })
    api.days(WEEK_START, WEEK_END)
    assert get_mote(api._conn, task).properties["scheduled"] == "2026-08-27 09:00/PT1H"


def test_a_multi_day_booking_never_rolls_forward(api, monkeypatch):
    """A three-day conference dated last week is not a slipped task --
    rolling it onto today would delete what the span said."""
    _freeze_today(monkeypatch, "2026-08-27")
    booking = create_mote(api._conn, {
        "name": "Conference", "status": "todo", "date": "2026-08-24/2026-08-26",
    })
    api.days(WEEK_START, WEEK_END)
    assert get_mote(api._conn, booking).properties["date"] == "2026-08-24/2026-08-26"


def test_a_recurring_placement_never_rolls_forward(api, monkeypatch):
    """A weekly standup anchored last month is not overdue -- the series
    is still producing this week's occurrence on its own."""
    _freeze_today(monkeypatch, "2026-08-27")
    series = create_mote(api._conn, {
        "name": "Weekly sync", "status": "todo", "estimate": "30m",
        "date": "2026-08-03T09:00/PT30M\nRRULE:FREQ=WEEKLY",
    })
    api.days(WEEK_START, WEEK_END)
    assert get_mote(api._conn, series).properties["date"] == \
        "2026-08-03T09:00/PT30M\nRRULE:FREQ=WEEKLY"


def test_schedule_selected_untangles_a_three_way_pileup(api, monkeypatch):
    from datetime import datetime

    from core.calendar import conflicts as calendar_conflicts
    from core.entities import Mote

    _freeze_today(monkeypatch, "2026-08-24")
    conn = api._conn
    a = create_mote(conn, {"name": "A", "status": "todo", "estimate": "1h", "scheduled": "2026-08-26 09:00"})
    b = create_mote(conn, {"name": "B", "status": "todo", "estimate": "1h", "scheduled": "2026-08-26 09:15"})
    c = create_mote(conn, {"name": "C", "status": "todo", "estimate": "1h", "scheduled": "2026-08-26 09:30"})

    answer = api.schedule_selected([a, b, c])
    assert {m["name"] for m in answer["moved"]} <= {"A", "B", "C"}

    motes = [Mote(properties=get_mote(conn, mid).properties, id=mid) for mid in (a, b, c)]
    remaining = calendar_conflicts(
        motes, bindings=["scheduled"],
        window_start=datetime.fromisoformat(WEEK_START), window_end=datetime.fromisoformat(WEEK_END),
    )
    assert remaining == []


def test_schedule_selected_only_touches_the_ids_it_was_given(api, board, monkeypatch):
    """The old Auto-schedule button had to guess who was allowed to
    move; this one doesn't ask -- whatever wasn't in the selection stays
    exactly where it was, conflict or not."""
    from core.motes import update_mote

    _freeze_today(monkeypatch, "2026-08-24")
    conn = api._conn
    update_mote(conn, board["backfill"], {"scheduled": "2026-08-26T09:30:00/PT3H"})
    before = get_mote(conn, board["backfill"]).properties["scheduled"]
    overlapping = create_mote(conn, {
        "name": "Collides", "status": "todo", "estimate": "1h", "scheduled": "2026-08-26 10:00",
    })

    answer = api.schedule_selected([overlapping])
    assert get_mote(conn, board["backfill"]).properties["scheduled"] == before
    assert any(m["name"] == "Collides" for m in answer["moved"])


def test_schedule_selected_moves_a_task_off_a_fixed_calendar_event(api, board, monkeypatch):
    """Standup is bound under `date`, so it is never itself a candidate
    to move -- but a selected `scheduled` task dropped on top of it
    still has to be recognized as busy and moved clear of it."""
    from core.motes import update_mote

    _freeze_today(monkeypatch, "2026-08-24")
    conn = api._conn
    # Standup runs 2026-08-26 09:00-09:30; force the task directly on top
    # of it rather than through schedule(), which would dodge the gap.
    update_mote(conn, board["backfill"], {"scheduled": "2026-08-26 09:00/PT30M"})

    answer = api.schedule_selected([board["backfill"]])
    assert any(m["name"] == "Backfill dry run" for m in answer["moved"])
    assert get_mote(conn, board["backfill"]).properties["scheduled"] != "2026-08-26 09:00/PT30M"


def test_schedule_selected_releases_a_completed_tasks_slot_instead_of_moving_it(api, board, monkeypatch):
    """A done task is never something this moves out of someone else's
    way -- its slot is simply given back."""
    from core.motes import update_mote

    _freeze_today(monkeypatch, "2026-08-24")
    conn = api._conn
    update_mote(conn, board["backfill"], {
        "status": "done", "scheduled": "2026-08-26 09:00/PT30M",
    })

    answer = api.schedule_selected([board["backfill"]])
    assert answer["moved"] == []
    assert "scheduled" not in get_mote(conn, board["backfill"]).properties


def test_schedule_selected_skips_an_undated_mote(api, monkeypatch):
    """A selected mote that was never dropped on a day has no placement
    to resolve -- schedule() is the drag that invents one, not this."""
    _freeze_today(monkeypatch, "2026-08-24")
    conn = api._conn
    undated = create_mote(conn, {"name": "Undated", "status": "todo", "estimate": "1h"})

    answer = api.schedule_selected([undated])
    assert answer["moved"] == []
    assert "scheduled" not in get_mote(conn, undated).properties


def test_schedule_selected_packs_two_movers_tightly_instead_of_leaving_a_gap(api, monkeypatch):
    """When two selected tasks both need to move off the same fixed
    class, the second one placed must dodge the *first one's real new
    slot*, not the stale position it still sits at until its own turn --
    otherwise a gap opens up that a tighter packing would have used."""
    _freeze_today(monkeypatch, "2026-08-24")
    conn = api._conn
    create_mote(conn, {"name": "Lecture", "date": "2026-08-26 09:00/PT1H15M"})
    a = create_mote(conn, {
        "name": "A", "status": "todo", "estimate": "45m", "scheduled": "2026-08-26 09:00",
    })
    b = create_mote(conn, {
        "name": "B", "status": "todo", "estimate": "1h", "scheduled": "2026-08-26 09:45",
    })

    api.schedule_selected([a, b])

    from core import interval as interval_repo
    ivs = sorted(
        (interval_repo.parse(get_mote(conn, mid).properties["scheduled"]) for mid in (a, b)),
        key=lambda iv: iv.start,
    )
    # Both land back-to-back right after the lecture ends at 10:15 --
    # no 30-minute gap left unclaimed between them.
    assert ivs[0].start.strftime("%H:%M") == "10:15"
    assert ivs[1].start == ivs[0].end


def test_reorder_day_re_times_cards_to_match_the_given_order(api, monkeypatch):
    _freeze_today(monkeypatch, "2026-08-24")
    conn = api._conn
    a = create_mote(conn, {"name": "A", "status": "todo", "estimate": "1h", "scheduled": "2026-08-26 09:00"})
    b = create_mote(conn, {"name": "B", "status": "todo", "estimate": "1h", "scheduled": "2026-08-26 10:00"})

    api.reorder_day("2026-08-26", [b, a])

    from core import interval as interval_repo
    iv_a = interval_repo.parse(get_mote(conn, a).properties["scheduled"])
    iv_b = interval_repo.parse(get_mote(conn, b).properties["scheduled"])
    assert iv_b.start < iv_a.start
    assert iv_b.start.strftime("%H:%M") == "09:00"
    assert iv_a.start == iv_b.end


def test_reorder_day_only_touches_cards_actually_on_that_day(api, monkeypatch):
    _freeze_today(monkeypatch, "2026-08-24")
    conn = api._conn
    a = create_mote(conn, {"name": "A", "status": "todo", "estimate": "1h", "scheduled": "2026-08-26 09:00"})
    elsewhere = create_mote(conn, {"name": "Elsewhere", "status": "todo", "estimate": "1h", "scheduled": "2026-08-27 09:00"})
    before = get_mote(conn, elsewhere).properties["scheduled"]

    answer = api.reorder_day("2026-08-26", [a, elsewhere])
    assert not any(m["id"] == elsewhere for m in answer["moved"])
    assert get_mote(conn, elsewhere).properties["scheduled"] == before


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


# ---- undo ----

def test_deleting_a_task_can_be_undone(api, board):
    api.delete_mote(board["schema"])
    assert get_mote(api._conn, board["schema"]) is None
    assert api.undo()["undone"] is not None
    assert get_mote(api._conn, board["schema"]) is not None


def test_creating_a_task_can_be_undone(api, board):
    created = api.create_task("New thing")
    assert api.undo()["undone"] is not None
    assert get_mote(api._conn, created["id"]) is None


def test_setting_status_can_be_undone(api, board):
    api.set_status(board["backfill"], "status", "doing")
    assert get_mote(api._conn, board["backfill"]).properties["status"] == "doing"
    assert api.undo()["undone"] is not None
    assert get_mote(api._conn, board["backfill"]).properties["status"] == "todo"


def test_scheduling_a_task_can_be_undone(api, board, monkeypatch):
    _freeze_today(monkeypatch, "2026-08-24")
    before = get_mote(api._conn, board["backfill"]).properties
    answer = api.schedule(board["backfill"], "2026-08-26")
    binding = answer["binding"]
    assert get_mote(api._conn, board["backfill"]).properties[binding] != before.get(binding)
    assert api.undo()["undone"] is not None
    assert get_mote(api._conn, board["backfill"]).properties.get(binding) == before.get(binding)


def test_a_subtask_toggle_can_be_undone(api, board):
    api.set_subtask(board["schema"], 1, True)
    assert api.undo()["undone"] is not None
    assert get_mote(api._conn, board["schema"]).properties["subtasks"][1]["done"] is False


def test_logging_a_session_can_be_undone(api, board):
    answer = api.log_session(board["schema"], 30)
    session_id = answer["id"]
    assert get_mote(api._conn, session_id) is not None
    assert api.undo()["undone"] is not None
    assert get_mote(api._conn, session_id) is None
    # the estimate deduction folds into the same undo step
    assert get_mote(api._conn, board["schema"]).properties["estimate"] == "5h"


def test_linking_two_motes_can_be_undone(api, board):
    answer = api.link(board["schema"], board["backfill"], "blocks")
    rel_id = answer["relationships"][-1]["id"]
    assert api.undo()["undone"] is not None
    assert all(r["id"] != rel_id for r in api.focus(board["schema"])["relationships"])


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

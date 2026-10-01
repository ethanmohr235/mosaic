"""The calendar panes' core (core/agenda.py): the grammar of `agenda`,
`week` and `month`, their ranges and rewrites, rows and bars, and the
`set` lines and `detach` a move writes. Today is pinned to Wednesday
2026-10-07 throughout."""
from datetime import date

import pytest

from core import agenda, commands
from core.command_bar import CommandError
from core.motes import create_mote, get_mote, list_motes, update_mote
from core.relationships import create_relationship
from core.species import set_species

TODAY = date(2026, 10, 7)


def run(conn, text):
    return agenda.run(conn, agenda.parse_text(text), today=TODAY)


@pytest.fixture
def week(conn):
    task = create_mote(conn, {"name": "task"})
    lecture = create_mote(conn, {"name": "ME 210", "date": "2026-10-05T09:00/PT50M\nRRULE:FREQ=WEEKLY;BYDAY=MO,WE,FR"})
    hw = create_mote(conn, {"name": "HW4", "due": "2026-10-09", "start": "2026-10-02", "status": "todo"})
    set_species(conn, hw, task)
    trip = create_mote(conn, {"name": "Field trip", "date": "2026-10-06/2026-10-09"})
    lab = create_mote(conn, {"name": "Lab", "when": "2026-10-08T13:00/PT2H", "modoc": "notes"})
    return {"task": task, "lecture": lecture, "hw": hw, "trip": trip, "lab": lab}


# ---- grammar ----

def test_lines_round_trip():
    for text in ("agenda", "agenda 2026-10-01 2026-10-10 --keys due,date --type task status:todo",
                 "week today", "month 2026-11-01"):
        assert agenda.parse_text(text).text() == text


@pytest.mark.parametrize("text, message", [
    ("week 2026-10-01 2026-10-02", "one date"),
    ("agenda --wat x", "takes"),
    ("agenda --keys", "needs a value"),
])
def test_a_bad_line_says_what_is_wrong(text, message):
    with pytest.raises(CommandError, match=message):
        agenda.parse_text(text)


def test_windows():
    assert agenda.window(agenda.parse_text("week"), TODAY) == (date(2026, 10, 5), date(2026, 10, 12))
    assert agenda.window(agenda.parse_text("agenda"), TODAY) == (TODAY, date(2026, 10, 21))
    assert agenda.window(agenda.parse_text("agenda 2026-10-01 2026-10-03"), TODAY) == (
        date(2026, 10, 1), date(2026, 10, 4))
    first, last = agenda.window(agenda.parse_text("month"), TODAY)
    assert first == date(2026, 9, 28) and (last - first).days == 42
    assert agenda.window(agenda.parse_text("week 7d"), TODAY)[0] == date(2026, 10, 12)


def test_paging_and_today_rewrite_the_date():
    week = agenda.parse_text("week")
    assert agenda.rewrite(week, {"page": 1}, TODAY).text() == "week 2026-10-12"
    month = agenda.parse_text("month 2026-12-15")
    assert agenda.rewrite(month, {"page": 1}, TODAY).text() == "month 2027-01-01"
    assert agenda.rewrite(month, {"today": True}, TODAY).text() == "month"
    span = agenda.parse_text("agenda 2026-10-01 2026-10-03")
    assert agenda.rewrite(span, {"page": -1}, TODAY).text() == "agenda 2026-09-28 2026-09-30"


def test_key_toggles_narrow_and_restore():
    line = agenda.parse_text("week")
    off = agenda.rewrite(line, {"key": "due", "all_keys": ["date", "due"]}, TODAY)
    assert off.text() == "week --keys date"
    assert agenda.rewrite(off, {"key": "due", "all_keys": ["date", "due"]}, TODAY).text() == "week"
    assert agenda.rewrite(off, {"keys": None}, TODAY).text() == "week"


# ---- the answer ----

def test_a_week_places_rows_and_bars(conn, week):
    out = run(conn, "week")
    lectures = [r for r in out["rows"] if r["name"] == "ME 210"]
    assert [r["start"][:10] for r in lectures] == ["2026-10-05", "2026-10-07", "2026-10-09"]
    assert all(r["recurring"] for r in lectures)
    bars = {b["name"]: b for b in out["bars"]}
    assert bars["Field trip"]["source"] == "span"
    assert (bars["Field trip"]["start"], bars["Field trip"]["end"]) == ("2026-10-06", "2026-10-09")
    assert bars["HW4"]["source"] == "due" and bars["HW4"]["end"] == "2026-10-10"
    assert not any(r["name"] == "Field trip" for r in out["rows"])
    lab = next(r for r in out["rows"] if r["name"] == "Lab")
    assert lab["note"] and lab["binding"] == "when"
    assert out["title"] == "Oct 5 – Oct 11"


def test_filters(conn, week):
    assert {r["name"] for r in run(conn, "week --keys when")["rows"]} == {"Lab"}
    assert {r["name"] for r in run(conn, "week --type task")["rows"]} == {"HW4"}
    assert {r["name"] for r in run(conn, "week status:todo")["rows"]} == {"HW4"}
    assert run(conn, "week --keys nope")["problems"] == ["no mote has a date under 'nope'"]
    keys = {k["key"]: k["active"] for k in run(conn, "week --keys when")["keys"]}
    assert keys == {"date": False, "due": False, "start": False, "when": True}


def test_a_project_bar_starts_when_its_garden_was(conn):
    owner = create_mote(conn, {"name": "Thesis", "due": "2026-10-20", "garden": "thesis"})
    conn.execute("UPDATE mote_properties SET updated_at = '2026-10-01T10:00:00' WHERE mote_id = ? AND key = 'garden'",
                 (owner,))
    (bar,) = run(conn, "month")["bars"]
    assert (bar["start"], bar["end"], bar["garden"]) == ("2026-10-01", "2026-10-21", "thesis")


def test_templates_are_never_placed(conn, week):
    template = create_mote(conn, {"name": "task template", "date": "2026-10-07"})
    create_relationship(conn, template, week["task"], "template_of")
    assert not any(r["name"] == "task template" for r in run(conn, "week")["rows"])


# ---- edits ----

def test_moving_a_series_is_one_set_line(conn, week):
    line = agenda.edit_line(conn, week["lecture"], "date", "2026-10-07T09:00",
                            "2026-10-07T10:00", "2026-10-07T10:50")
    assert line.startswith(f"set #{week['lecture']} date ")
    result = commands.run_set(conn, commands.parse(line).args, [])
    value = get_mote(conn, week["lecture"]).properties["date"]
    assert value.startswith("2026-10-05T10:00") and "RRULE" in value
    result.command.undo()
    assert get_mote(conn, week["lecture"]).properties["date"].startswith("2026-10-05T09:00")


def test_resizing_writes_the_span(conn, week):
    line = agenda.edit_line(conn, week["lab"], "when", "2026-10-08T13:00",
                            "2026-10-08T13:00", "2026-10-08T16:00")
    commands.run_set(conn, commands.parse(line).args, [])
    assert get_mote(conn, week["lab"]).properties["when"] == "2026-10-08T13:00/PT3H"
    assert agenda.edit_line(conn, week["lab"], "when", "2026-10-08T13:00",
                            "2026-10-08T13:00", "2026-10-08T16:00") is None


def test_skipping_one_occurrence_is_a_set(conn, week):
    line = agenda.skip_line(conn, week["lecture"], "date", "2026-10-07T09:00")
    commands.run_set(conn, commands.parse(line).args, [])
    out = run(conn, "week --keys date")
    assert [r["start"][:10] for r in out["rows"] if r["name"] == "ME 210"] == ["2026-10-05", "2026-10-09"]
    assert agenda.skip_line(conn, week["lab"], "when", "2026-10-08T13:00") is None


def test_detaching_one_occurrence_is_one_undo_step(conn, week):
    before = len(list_motes(conn))
    message, command = agenda.detach(conn, [f"#{week['lecture']}", "date", "2026-10-07T09:00",
                                            "2026-10-07T14:00", "2026-10-07T14:50"])
    assert "out of its series" in message
    starts = sorted(r["start"] for r in run(conn, "week --keys date")["rows"] if r["name"] == "ME 210")
    assert starts == ["2026-10-05T09:00:00", "2026-10-07T14:00:00", "2026-10-09T09:00:00"]
    command.undo()
    assert len(list_motes(conn)) == before
    with pytest.raises(CommandError, match="doesn't repeat"):
        agenda.detach(conn, [f"#{week['lab']}", "when", "2026-10-08T13:00", "2026-10-08T14:00",
                             "2026-10-08T15:00"])


def test_slot_values():
    assert agenda.slot_value("2026-10-07T09:00", "2026-10-07T10:00", False) == "2026-10-07T09:00/PT1H"
    assert agenda.slot_value("2026-10-07T00:00", "2026-10-08T00:00", True) == "2026-10-07"


def test_a_mote_gone_is_refused(conn, week):
    update_mote(conn, week["lab"], {"when": "not a date"})
    with pytest.raises(CommandError, match="no longer holds a date"):
        agenda.edit_line(conn, week["lab"], "when", "2026-10-08T13:00", "2026-10-08T14:00", "2026-10-08T15:00")

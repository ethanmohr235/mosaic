"""core/calendar.py -- placing motes on a calendar.

Everything here is pure: motes in, occurrences out. No connection, no
window. That is the point of the module living in core/ rather than in
monastery/, and these tests are what it buys.
"""
from datetime import datetime

from core.calendar import (
    DEFAULT_TIMED_MINUTES,
    Occurrence,
    binding_counts,
    default_bindings,
    occurrences,
    parse_date_value,
    undated_count,
)
from core.entities import Mote


def _mote(properties, id="m1"):
    return Mote(properties=properties, id=id)


WEEK_START = datetime(2026, 8, 24)
WEEK_END = datetime(2026, 8, 31)


def _in_week(motes, bindings):
    return occurrences(motes, bindings=bindings, window_start=WEEK_START, window_end=WEEK_END)


# ---- parsing ----

def test_parse_date_value_reads_a_bare_date_as_all_day():
    assert parse_date_value("2026-08-24") == (datetime(2026, 8, 24), True)


def test_parse_date_value_reads_an_iso_datetime_as_timed():
    assert parse_date_value("2026-08-24T09:15:00") == (datetime(2026, 8, 24, 9, 15), False)


def test_parse_date_value_accepts_a_space_instead_of_a_t():
    """What people actually type into a property field."""
    assert parse_date_value("2026-08-24 09:15") == (datetime(2026, 8, 24, 9, 15), False)


def test_parse_date_value_rejects_non_dates():
    for value in ["", "   ", "not-a-date", "2026-13-45", None, 12345, [], {}]:
        assert parse_date_value(value) is None


def test_parse_date_value_rejects_booleans():
    # True is an int in Python; a mote with done:true must not be dated.
    assert parse_date_value(True) is None
    assert parse_date_value(False) is None


# ---- bindings ----

def test_binding_counts_finds_every_date_shaped_key():
    motes = [
        _mote({"name": "Standup", "when": "2026-08-24 09:15"}, id="a"),
        _mote({"name": "Audit", "due": "2026-08-25"}, id="b"),
        _mote({"name": "Sweep", "due": "2026-08-26"}, id="c"),
        _mote({"name": "Rina", "role": "engineer"}, id="d"),
    ]
    assert binding_counts(motes) == {"when": 1, "due": 2}


def test_binding_counts_excludes_end_keys():
    """`ends` qualifies a start rather than being one -- offering it as a
    binding would place the tail of a range as its own event."""
    motes = [_mote({"starts": "2026-08-24 09:00", "ends": "2026-08-24 11:00"})]
    assert binding_counts(motes) == {"starts": 1}


def test_default_bindings_are_all_of_them_most_used_first():
    motes = [
        _mote({"due": "2026-08-24"}, id="a"),
        _mote({"due": "2026-08-25"}, id="b"),
        _mote({"when": "2026-08-26"}, id="c"),
    ]
    assert default_bindings(motes) == ["due", "when"]


# ---- placing ----

def test_a_plain_dated_mote_places_once():
    motes = [_mote({"name": "Deadline", "due": "2026-08-26"})]
    placed = _in_week(motes, ["due"])
    assert len(placed) == 1
    assert placed[0].start == datetime(2026, 8, 26)
    assert placed[0].all_day is True
    assert placed[0].binding == "due"


def test_a_timed_mote_defaults_to_the_same_span_the_export_uses():
    motes = [_mote({"name": "1:1", "when": "2026-08-24 15:00"})]
    placed = _in_week(motes, ["when"])
    assert (placed[0].end - placed[0].start).total_seconds() / 60 == DEFAULT_TIMED_MINUTES


def test_a_timed_mote_honours_duration_in_minutes():
    motes = [_mote({"when": "2026-08-24 09:15", "duration": 20})]
    placed = _in_week(motes, ["when"])
    assert placed[0].end == datetime(2026, 8, 24, 9, 35)


def test_an_all_day_mote_honours_duration_in_days():
    motes = [_mote({"starts": "2026-08-24", "duration": 3})]
    placed = _in_week(motes, ["starts"])
    assert placed[0].end == datetime(2026, 8, 27)


def test_an_explicit_end_wins_over_duration():
    motes = [_mote({"starts": "2026-08-24 09:00", "ends": "2026-08-24 12:30", "duration": 20})]
    placed = _in_week(motes, ["starts"])
    assert placed[0].end == datetime(2026, 8, 24, 12, 30)


def test_an_all_day_end_is_inclusive_as_people_write_it():
    """"Away Aug 25 to Aug 27" means the 27th is an away day, so the
    exclusive end this module works in is the 28th."""
    motes = [_mote({"starts": "2026-08-25", "ends": "2026-08-27"})]
    placed = _in_week(motes, ["starts"])
    assert placed[0].end == datetime(2026, 8, 28)


def test_an_end_before_its_start_falls_back_to_the_default_span():
    motes = [_mote({"when": "2026-08-24 15:00", "ends": "2026-08-24 09:00"})]
    placed = _in_week(motes, ["when"])
    assert placed[0].end == datetime(2026, 8, 24, 16, 0)


def test_a_mote_outside_the_window_is_not_placed():
    motes = [_mote({"due": "2026-09-30"})]
    assert _in_week(motes, ["due"]) == []


def test_a_span_starting_before_the_window_still_shows_inside_it():
    """The week is Aug 24-31. An away-week that began on the 22nd and
    runs to the 26th is very much part of this week, and a reader that
    only asked "does it start here?" would drop it entirely."""
    motes = [_mote({"starts": "2026-08-22", "ends": "2026-08-26"})]
    placed = _in_week(motes, ["starts"])
    assert len(placed) == 1
    assert placed[0].start == datetime(2026, 8, 22)
    assert placed[0].end == datetime(2026, 8, 27)


def test_a_timed_event_running_past_midnight_into_the_window_shows():
    motes = [_mote({"when": "2026-08-23 22:00", "duration": 240})]
    assert len(_in_week(motes, ["when"])) == 1


def test_a_span_ending_exactly_at_the_window_start_is_already_over():
    motes = [_mote({"starts": "2026-08-20", "ends": "2026-08-23"})]
    assert _in_week(motes, ["starts"]) == []


def test_a_recurrence_whose_occurrence_starts_before_the_window_still_shows():
    motes = [_mote({
        "starts": "2026-08-01", "duration": 4, "recurrence": "FREQ=WEEKLY;BYDAY=SA",
    })]
    # The Saturday before this week is Aug 22; +4 days runs to the 26th.
    placed = _in_week(motes, ["starts"])
    assert [o.start.day for o in placed] == [22, 29]


def test_a_disabled_binding_places_nothing():
    motes = [_mote({"due": "2026-08-26", "when": "2026-08-24 09:00"})]
    placed = _in_week(motes, ["when"])
    assert [o.binding for o in placed] == ["when"]


def test_one_mote_under_two_bindings_places_twice():
    """Not deduplicated on purpose: a task whose `starts` and `due` are
    different days genuinely occupies both."""
    motes = [_mote({"name": "Audit", "starts": "2026-08-24", "due": "2026-08-27"})]
    placed = _in_week(motes, ["starts", "due"])
    assert [(o.binding, o.start.day) for o in placed] == [("starts", 24), ("due", 27)]


def test_placements_come_back_in_time_order():
    motes = [
        _mote({"when": "2026-08-26 09:00"}, id="late"),
        _mote({"when": "2026-08-24 09:00"}, id="early"),
    ]
    assert [o.mote.id for o in _in_week(motes, ["when"])] == ["early", "late"]


def test_excluded_ids_are_never_placed():
    """Templates and scenario overrides hold replacement values, not real
    data -- the same exclusion exportable_motes() applies."""
    motes = [
        _mote({"name": "Real", "due": "2026-08-25"}, id="real"),
        _mote({"name": "Template", "due": "2026-08-26"}, id="template"),
    ]
    placed = occurrences(
        motes, bindings=["due"], window_start=WEEK_START, window_end=WEEK_END,
        exclude_ids={"template"},
    )
    assert [o.mote.id for o in placed] == ["real"]


# ---- recurrence ----

def test_a_daily_rule_expands_across_the_window():
    motes = [_mote({"name": "Standup", "when": "2026-08-24 09:15", "recurrence": "FREQ=DAILY"})]
    placed = _in_week(motes, ["when"])
    assert [o.start.day for o in placed] == [24, 25, 26, 27, 28, 29, 30]
    assert all(o.mote.id == "m1" for o in placed), "every occurrence is the same mote"


def test_a_weekly_rule_expands_on_its_named_days():
    motes = [_mote({"when": "2026-08-24 09:00", "recurrence": "FREQ=WEEKLY;BYDAY=MO,WE"})]
    placed = _in_week(motes, ["when"])
    assert [o.start.day for o in placed] == [24, 26]


def test_a_rule_carries_its_span_onto_every_occurrence():
    motes = [_mote({"when": "2026-08-24 09:15", "ends": "2026-08-24 09:35", "recurrence": "FREQ=DAILY"})]
    placed = _in_week(motes, ["when"])
    assert all((o.end - o.start).total_seconds() / 60 == 20 for o in placed)


def test_a_rule_starting_before_the_window_still_shows_inside_it():
    motes = [_mote({"when": "2026-06-01 09:00", "recurrence": "FREQ=WEEKLY;BYDAY=MO"})]
    placed = _in_week(motes, ["when"])
    assert [o.start.day for o in placed] == [24]


def test_a_rule_stops_at_its_until():
    motes = [_mote({"when": "2026-08-24 09:00", "recurrence": "FREQ=DAILY;UNTIL=20260826T235959"})]
    assert [o.start.day for o in _in_week(motes, ["when"])] == [24, 25, 26]


def test_a_leading_rrule_prefix_is_stripped():
    motes = [_mote({"when": "2026-08-24 09:00", "recurrence": "RRULE:FREQ=DAILY;COUNT=2"})]
    assert [o.start.day for o in _in_week(motes, ["when"])] == [24, 25]


def test_a_broken_rule_still_places_the_mote_on_its_own_date():
    """The same permissiveness a broken note gets: appear looking wrong,
    rather than vanish."""
    motes = [_mote({"when": "2026-08-24 09:00", "recurrence": "FREQ=NONSENSE"})]
    placed = _in_week(motes, ["when"])
    assert [o.start.day for o in placed] == [24]


# ---- the status bar ----

def test_undated_count_is_motes_with_no_date_shaped_property():
    motes = [
        _mote({"name": "Rina", "role": "engineer"}, id="a"),
        _mote({"name": "Dev"}, id="b"),
        _mote({"name": "Audit", "due": "2026-08-25"}, id="c"),
    ]
    assert undated_count(motes) == 2


def test_undated_count_ignores_which_bindings_are_toggled_on():
    """A mote hidden because you toggled `due` off is still a dated mote
    -- counting it as undated would move the number for the wrong
    reason."""
    motes = [_mote({"due": "2026-08-25"})]
    assert undated_count(motes) == 0

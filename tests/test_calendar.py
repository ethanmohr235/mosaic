"""core/calendar.py -- placing motes on a calendar.

Everything here is pure: motes in, occurrences out. No connection, no
window. That is the point of the module living in core/ rather than in
Monastery (the console calendar panes), and these tests are what it buys.
"""
from datetime import date, datetime, time, timedelta

from core.calendar import (
    DEFAULT_TIMED_MINUTES,
    Occurrence,
    binding_counts,
    conflicts,
    default_bindings,
    detached_properties,
    earliest_free_slot,
    is_recurring,
    moved_properties,
    occurrences,
    resized_properties,
    span_of,
    with_exception,
)
from core.entities import Mote


def _mote(properties, id="m1"):
    return Mote(properties=properties, id=id)


WEEK_START = datetime(2026, 8, 24)
WEEK_END = datetime(2026, 8, 31)


def _in_week(motes, bindings):
    return occurrences(motes, bindings=bindings, window_start=WEEK_START, window_end=WEEK_END)


# ---- parsing ----

# ---- bindings ----

def test_binding_counts_finds_every_date_shaped_key():
    motes = [
        _mote({"name": "Standup", "when": "2026-08-24 09:15"}, id="a"),
        _mote({"name": "Audit", "due": "2026-08-25"}, id="b"),
        _mote({"name": "Sweep", "due": "2026-08-26"}, id="c"),
        _mote({"name": "Rina", "role": "engineer"}, id="d"),
    ]
    assert binding_counts(motes) == {"when": 1, "due": 2}


def test_every_interval_shaped_key_is_a_binding_now():
    """`ends`/`end` used to be excluded here, because a span written in a
    separate property qualified a start rather than being one. A span
    now rides inside its own start's value, so there is no such key to
    special-case -- and a garden that still writes one gets an ordinary
    binding it can toggle off, rather than a name this module knows."""
    motes = [_mote({"starts": "2026-08-24 09:00/PT2H"})]
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


def test_a_timed_mote_honours_the_duration_in_its_interval():
    motes = [_mote({"when": "2026-08-24 09:15/PT20M"})]
    placed = _in_week(motes, ["when"])
    assert placed[0].end == datetime(2026, 8, 24, 9, 35)


def test_an_all_day_mote_honours_a_day_duration():
    motes = [_mote({"starts": "2026-08-24/P3D"})]
    placed = _in_week(motes, ["starts"])
    assert placed[0].end == datetime(2026, 8, 27)


def test_an_interval_may_name_its_end_instead_of_its_length():
    motes = [_mote({"starts": "2026-08-24 09:00/2026-08-24T12:30"})]
    placed = _in_week(motes, ["starts"])
    assert placed[0].end == datetime(2026, 8, 24, 12, 30)


def test_an_all_day_interval_end_is_exclusive_as_iso_means_it():
    """"Away Aug 25 to Aug 27" is stored as 25/28 -- the end this module
    has always computed internally, now written down. A picker is what
    shows a person the inclusive last day they actually think in."""
    motes = [_mote({"starts": "2026-08-25/2026-08-28"})]
    placed = _in_week(motes, ["starts"])
    assert placed[0].end == datetime(2026, 8, 28)


def test_an_end_before_its_start_falls_back_to_the_default_span():
    motes = [_mote({"when": "2026-08-24 15:00/2026-08-24T09:00"})]
    placed = _in_week(motes, ["when"])
    assert placed[0].end == datetime(2026, 8, 24, 16, 0)


def test_a_mote_outside_the_window_is_not_placed():
    motes = [_mote({"due": "2026-09-30"})]
    assert _in_week(motes, ["due"]) == []


def test_a_span_starting_before_the_window_still_shows_inside_it():
    """The week is Aug 24-31. An away-week that began on the 22nd and
    runs to the 26th is very much part of this week, and a reader that
    only asked "does it start here?" would drop it entirely."""
    motes = [_mote({"starts": "2026-08-22/2026-08-27"})]
    placed = _in_week(motes, ["starts"])
    assert len(placed) == 1
    assert placed[0].start == datetime(2026, 8, 22)
    assert placed[0].end == datetime(2026, 8, 27)


def test_a_timed_event_running_past_midnight_into_the_window_shows():
    motes = [_mote({"when": "2026-08-23 22:00/PT4H"})]
    assert len(_in_week(motes, ["when"])) == 1


def test_a_span_ending_exactly_at_the_window_start_is_already_over():
    motes = [_mote({"starts": "2026-08-20/2026-08-23"})]
    assert _in_week(motes, ["starts"]) == []


def test_a_recurrence_whose_occurrence_starts_before_the_window_still_shows():
    motes = [_mote({"starts": "2026-08-01/P4D\nRRULE:FREQ=WEEKLY;BYDAY=SA"})]
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
    motes = [_mote({"name": "Standup", "when": "2026-08-24 09:15\nRRULE:FREQ=DAILY"})]
    placed = _in_week(motes, ["when"])
    assert [o.start.day for o in placed] == [24, 25, 26, 27, 28, 29, 30]
    assert all(o.mote.id == "m1" for o in placed), "every occurrence is the same mote"


def test_a_weekly_rule_expands_on_its_named_days():
    motes = [_mote({"when": "2026-08-24 09:00\nRRULE:FREQ=WEEKLY;BYDAY=MO,WE"})]
    placed = _in_week(motes, ["when"])
    assert [o.start.day for o in placed] == [24, 26]


def test_a_rule_carries_its_span_onto_every_occurrence():
    motes = [_mote({"when": "2026-08-24 09:15/PT20M\nRRULE:FREQ=DAILY"})]
    placed = _in_week(motes, ["when"])
    assert all((o.end - o.start).total_seconds() / 60 == 20 for o in placed)


def test_a_rule_starting_before_the_window_still_shows_inside_it():
    motes = [_mote({"when": "2026-06-01 09:00\nRRULE:FREQ=WEEKLY;BYDAY=MO"})]
    placed = _in_week(motes, ["when"])
    assert [o.start.day for o in placed] == [24]


def test_a_rule_stops_at_its_until():
    motes = [_mote({"when": "2026-08-24 09:00\nRRULE:FREQ=DAILY;UNTIL=20260826T235959"})]
    assert [o.start.day for o in _in_week(motes, ["when"])] == [24, 25, 26]


def test_a_rule_line_may_omit_its_rrule_tag():
    motes = [_mote({"when": "2026-08-24 09:00\nFREQ=DAILY;COUNT=2"})]
    assert [o.start.day for o in _in_week(motes, ["when"])] == [24, 25]


def test_a_broken_rule_still_places_the_mote_on_its_own_date():
    """The same permissiveness a broken note gets: appear looking wrong,
    rather than vanish."""
    motes = [_mote({"when": "2026-08-24 09:00\nRRULE:FREQ=NONSENSE"})]
    placed = _in_week(motes, ["when"])
    assert [o.start.day for o in placed] == [24]


# ---- exceptions ----
#
# One occurrence taken out of a series is an EXDATE line inside the same
# `recurrence` property. No new reserved property, because RFC 5545 --
# and dateutil -- could already say it.

def test_an_excepted_occurrence_is_not_placed():
    motes = [_mote({"when": "2026-08-24 09:00\nRRULE:FREQ=DAILY\nEXDATE:20260826T090000"})]
    assert [o.start.day for o in _in_week(motes, ["when"])] == [24, 25, 27, 28, 29, 30]


def test_an_all_day_exception_uses_the_date_form():
    motes = [_mote({"starts": "2026-08-24\nRRULE:FREQ=DAILY\nEXDATE;VALUE=DATE:20260826"})]
    assert [o.start.day for o in _in_week(motes, ["starts"])] == [24, 25, 27, 28, 29, 30]


def test_excepting_the_anchor_itself_works():
    """RFC 5545 allows an EXDATE on DTSTART, and Monastery needs it: the
    first occurrence of a series is as deletable as any other. The mote
    keeps its `date` and goes on expanding from it."""
    motes = [_mote({"when": "2026-08-24 09:00\nRRULE:FREQ=DAILY;COUNT=3\nEXDATE:20260824T090000"})]
    assert [o.start.day for o in _in_week(motes, ["when"])] == [25, 26]


def test_a_rule_with_an_exception_still_carries_its_span():
    motes = [_mote({"when": "2026-08-24 09:00/PT30M\nRRULE:FREQ=DAILY\nEXDATE:20260825T090000"})]
    placed = _in_week(motes, ["when"])
    assert all(o.end - o.start == timedelta(minutes=30) for o in placed)


def test_with_exception_appends_one_line_to_the_bindings_own_value():
    mote = _mote({"when": "2026-08-24 09:00\nRRULE:FREQ=WEEKLY"})
    assert with_exception(mote, "when", datetime(2026, 8, 31, 9, 15)) == {
        "when": "2026-08-24 09:00\nRRULE:FREQ=WEEKLY\nEXDATE:20260831T091500"
    }


def test_excepting_the_same_occurrence_twice_changes_nothing():
    mote = _mote({"when": "2026-08-24 09:00\nRRULE:FREQ=WEEKLY\nEXDATE:20260831T091500"})
    assert with_exception(mote, "when", datetime(2026, 8, 31, 9, 15)) == {}


def test_a_mote_that_does_not_recur_has_nothing_to_except():
    assert with_exception(_mote({"when": "2026-08-24"}), "when", datetime(2026, 8, 24)) == {}


def test_a_placement_says_whether_it_came_from_a_rule():
    plain = _in_week([_mote({"when": "2026-08-24 09:00"})], ["when"])
    assert [o.recurring for o in plain] == [False]
    series = _in_week([_mote({"when": "2026-08-24 09:00\nRRULE:FREQ=DAILY;COUNT=2"})], ["when"])
    assert [o.recurring for o in series] == [True, True]


def test_a_broken_rule_does_not_count_as_recurring():
    """It already draws as one placement on its own date, so offering to
    except an occurrence out of it would be offering to edit something
    the calendar never showed."""
    assert is_recurring(_mote({"when": "2026-08-24\nRRULE:FREQ=NONSENSE"}), "when") is False
    assert is_recurring(_mote({"when": "2026-08-24\nRRULE:FREQ=DAILY"}), "when") is True


# ---- moving ----

def test_a_move_rewrites_the_binding_that_placed_it():
    """Not `date`. A task on the grid by `due` is a task whose `due` you
    just dragged; writing `date` instead would leave `due` where it was
    and place the mote twice."""
    mote = _mote({"due": "2026-08-28", "name": "Runbook"})
    moved = moved_properties(mote, "due", datetime(2026, 8, 28), datetime(2026, 8, 30))
    assert moved == {"due": "2026-08-30"}


def test_a_move_keeps_the_shape_the_date_was_written_in():
    mote = _mote({"when": "2026-08-24 09:15"})
    moved = moved_properties(mote, "when", datetime(2026, 8, 24, 9, 15), datetime(2026, 8, 25, 10, 0))
    assert moved["when"] == "2026-08-25 10:00"


def test_a_move_takes_an_explicit_end_with_it():
    """The end rides inside the value now, so a move is one write rather
    than two -- and cannot leave the two halves disagreeing."""
    mote = _mote({"starts": "2026-08-26/2026-08-28"})
    moved = moved_properties(mote, "starts", datetime(2026, 8, 26), datetime(2026, 8, 28))
    assert moved == {"starts": "2026-08-28/2026-08-30"}


def test_moving_one_occurrence_of_a_series_shifts_the_whole_anchor():
    """Dragging the second Monday of a weekly meeting to Tuesday moves
    every Monday: one mote is the whole series, so a series move is a
    shift by the same delta."""
    mote = _mote({"when": "2026-08-24 09:15\nRRULE:FREQ=WEEKLY;BYDAY=MO"})
    moved = moved_properties(mote, "when", datetime(2026, 8, 31, 9, 15), datetime(2026, 9, 1, 10, 0))
    assert moved["when"].splitlines()[0] == "2026-08-25 10:00"


def test_a_series_moved_across_days_takes_its_byday_with_it():
    """Otherwise the rule overrules the shifted anchor and the drag
    silently does nothing."""
    mote = _mote({"when": "2026-08-24 09:15\nRRULE:FREQ=WEEKLY;BYDAY=MO,WE,FR"})
    moved = moved_properties(mote, "when", datetime(2026, 8, 24, 9, 15), datetime(2026, 8, 25, 9, 15))
    assert moved["when"] == "2026-08-25 09:15\nRRULE:FREQ=WEEKLY;BYDAY=TU,WE,FR"


def test_a_series_moved_within_a_day_leaves_its_rule_alone():
    mote = _mote({"when": "2026-08-24 09:15\nRRULE:FREQ=WEEKLY;BYDAY=MO"})
    moved = moved_properties(mote, "when", datetime(2026, 8, 24, 9, 15), datetime(2026, 8, 24, 11, 0))
    assert moved["when"] == "2026-08-24 11:00\nRRULE:FREQ=WEEKLY;BYDAY=MO"


def test_a_move_that_moves_nothing_writes_nothing():
    mote = _mote({"when": "2026-08-24 09:15"})
    assert moved_properties(mote, "when", datetime(2026, 8, 24, 9, 15), datetime(2026, 8, 24, 9, 15)) == {}


# ---- resizing ----

def test_a_resize_rewrites_the_bindings_own_value():
    mote = _mote({"when": "2026-08-24 09:00/PT20M"})
    assert resized_properties(
        mote, "when", datetime(2026, 8, 24, 9, 0), datetime(2026, 8, 24, 10, 30)
    ) == {"when": "2026-08-24 09:00/PT1H30M"}


def test_a_resize_keeps_the_notation_the_mote_already_used():
    """An edit changes what you were shown rather than reformatting it --
    a value written as start/end goes on saying start/end."""
    mote = _mote({"starts": "2026-08-26/2026-08-28"})
    resized = resized_properties(mote, "starts", datetime(2026, 8, 26), datetime(2026, 8, 30))
    assert resized == {"starts": "2026-08-26/2026-08-30"}


def test_an_all_day_resize_writes_a_day_duration():
    mote = _mote({"starts": "2026-08-26"})
    assert resized_properties(
        mote, "starts", datetime(2026, 8, 26), datetime(2026, 8, 29)
    ) == {"starts": "2026-08-26/P3D"}


def test_resizing_one_occurrence_of_a_series_measures_from_that_occurrence():
    mote = _mote({"when": "2026-08-24 09:00/PT30M\nRRULE:FREQ=WEEKLY"})
    resized = resized_properties(
        mote, "when", datetime(2026, 8, 31, 9, 0), datetime(2026, 8, 31, 10, 0)
    )
    assert resized == {"when": "2026-08-24 09:00/PT1H\nRRULE:FREQ=WEEKLY"}


def test_a_resize_that_would_end_before_it_starts_writes_nothing():
    mote = _mote({"when": "2026-08-24 09:00"})
    assert resized_properties(
        mote, "when", datetime(2026, 8, 24, 9, 0), datetime(2026, 8, 24, 8, 0)
    ) == {}


# ---- detaching ----

def test_a_detached_occurrence_lands_where_it_was_dropped_without_the_rule():
    """A copy that kept the rule would go on recurring, so the rule is
    dropped here rather than by the caller."""
    mote = _mote({"when": "2026-08-24 09:00/PT30M\nRRULE:FREQ=WEEKLY"})
    detached = detached_properties(
        mote, "when", datetime(2026, 9, 7, 11, 0), datetime(2026, 9, 7, 11, 45)
    )
    assert detached == {"when": "2026-09-07 11:00/PT45M"}


def test_a_detached_all_day_occurrence_keeps_the_notation_it_had():
    mote = _mote({"starts": "2026-08-26/2026-08-28\nRRULE:FREQ=WEEKLY"})
    detached = detached_properties(
        mote, "starts", datetime(2026, 9, 2), datetime(2026, 9, 4)
    )
    assert detached == {"starts": "2026-09-02/2026-09-04"}


def test_span_of_answers_what_every_placement_lasts():
    assert span_of(_mote({"when": "2026-08-24 09:00/PT20M"}), "when") == timedelta(minutes=20)
    assert span_of(_mote({"when": "2026-08-24 09:00"}), "when") == timedelta(minutes=DEFAULT_TIMED_MINUTES)
    assert span_of(_mote({"name": "Rina"}), "when") is None


# ---- finding a free slot ----
#
# The earliest gap on a chosen day that fits a duration. Reads the same
# occurrences the calendar draws, so "busy" means one thing in this app
# rather than two.

def test_an_empty_day_offers_the_start_of_the_working_day():
    assert earliest_free_slot(
        [], day=date(2026, 8, 26), duration=timedelta(minutes=60), bindings=["when"],
    ) == datetime(2026, 8, 26, 9, 0)


def test_a_slot_starts_when_the_meeting_before_it_ends():
    motes = [_mote({"when": "2026-08-26 09:00/PT30M"})]
    assert earliest_free_slot(
        motes, day=date(2026, 8, 26), duration=timedelta(minutes=60), bindings=["when"],
    ) == datetime(2026, 8, 26, 9, 30)


def test_a_gap_between_meetings_is_used_when_it_is_wide_enough():
    motes = [
        _mote({"when": "2026-08-26 09:00/PT1H"}, id="a"),
        _mote({"when": "2026-08-26 11:00/PT1H"}, id="b"),
    ]
    assert earliest_free_slot(
        motes, day=date(2026, 8, 26), duration=timedelta(minutes=60), bindings=["when"],
    ) == datetime(2026, 8, 26, 10, 0)


def test_a_gap_too_narrow_is_skipped_for_the_next_one():
    motes = [
        _mote({"when": "2026-08-26 09:00/PT1H"}, id="a"),
        _mote({"when": "2026-08-26 10:30/PT1H"}, id="b"),
    ]
    assert earliest_free_slot(
        motes, day=date(2026, 8, 26), duration=timedelta(minutes=60), bindings=["when"],
    ) == datetime(2026, 8, 26, 11, 30)


def test_overlapping_meetings_close_one_gap_rather_than_two():
    motes = [
        _mote({"when": "2026-08-26 09:00/PT2H"}, id="a"),
        _mote({"when": "2026-08-26 09:30/PT30M"}, id="b"),
    ]
    assert earliest_free_slot(
        motes, day=date(2026, 8, 26), duration=timedelta(minutes=30), bindings=["when"],
    ) == datetime(2026, 8, 26, 11, 0)


def test_an_all_day_mote_marks_a_day_without_occupying_it():
    """A task due Thursday does not make Thursday unschedulable."""
    motes = [_mote({"due": "2026-08-26"})]
    assert earliest_free_slot(
        motes, day=date(2026, 8, 26), duration=timedelta(minutes=60), bindings=["due"],
    ) == datetime(2026, 8, 26, 9, 0)


def test_a_day_with_no_room_left_answers_none():
    motes = [_mote({"when": "2026-08-26 09:00/PT8H"})]
    assert earliest_free_slot(
        motes, day=date(2026, 8, 26), duration=timedelta(minutes=60), bindings=["when"],
    ) is None


def test_something_longer_than_the_working_day_never_fits():
    assert earliest_free_slot(
        [], day=date(2026, 8, 26), duration=timedelta(hours=9), bindings=["when"],
    ) is None


# ---- finding conflicts ----
#
# What the Auto-schedule button asks first: which placements collide.
# Reads occurrences() the same as earliest_free_slot() does, so a
# collision here means exactly what a rectangle drawn on top of another
# rectangle means in Monastery.

def test_two_motes_with_no_overlap_have_no_conflicts():
    motes = [
        _mote({"when": "2026-08-26 09:00/PT1H"}, id="a"),
        _mote({"when": "2026-08-26 10:00/PT1H"}, id="b"),
    ]
    assert conflicts(motes, bindings=["when"], window_start=WEEK_START, window_end=WEEK_END) == []


def test_two_overlapping_motes_are_reported_as_a_pair():
    a = _mote({"when": "2026-08-26 09:00/PT1H"}, id="a")
    b = _mote({"when": "2026-08-26 09:30/PT1H"}, id="b")
    found = conflicts([a, b], bindings=["when"], window_start=WEEK_START, window_end=WEEK_END)
    assert len(found) == 1
    assert {found[0][0].mote.id, found[0][1].mote.id} == {"a", "b"}


def test_a_conflict_can_span_two_different_bindings():
    """A task `due` at the same time as a meeting `when` collides with it
    just as readily as two tasks would -- there is no second notion of
    busy for events versus tasks."""
    task = _mote({"due": "2026-08-26 09:00/PT1H"}, id="task")
    meeting = _mote({"when": "2026-08-26 09:15/PT1H"}, id="meeting")
    found = conflicts(
        [task, meeting], bindings=["due", "when"], window_start=WEEK_START, window_end=WEEK_END,
    )
    assert len(found) == 1


def test_all_day_placements_never_conflict():
    """A task due Thursday marks the day rather than occupying it, the
    same rule earliest_free_slot() applies to busy time."""
    motes = [
        _mote({"due": "2026-08-26"}, id="a"),
        _mote({"due": "2026-08-26"}, id="b"),
    ]
    assert conflicts(motes, bindings=["due"], window_start=WEEK_START, window_end=WEEK_END) == []


def test_three_overlapping_motes_report_every_pair():
    a = _mote({"when": "2026-08-26 09:00/PT2H"}, id="a")
    b = _mote({"when": "2026-08-26 09:30/PT2H"}, id="b")
    c = _mote({"when": "2026-08-26 10:30/PT2H"}, id="c")
    found = conflicts([a, b, c], bindings=["when"], window_start=WEEK_START, window_end=WEEK_END)
    pairs = {frozenset((x.mote.id, y.mote.id)) for x, y in found}
    assert pairs == {frozenset(("a", "b")), frozenset(("b", "c")), frozenset(("a", "c"))}


def test_excluded_motes_are_never_part_of_a_conflict():
    a = _mote({"when": "2026-08-26 09:00/PT1H"}, id="a")
    b = _mote({"when": "2026-08-26 09:30/PT1H"}, id="b")
    found = conflicts(
        [a, b], bindings=["when"], window_start=WEEK_START, window_end=WEEK_END,
        exclude_ids={"b"},
    )
    assert found == []


def test_the_working_window_is_the_callers_to_decide():
    motes = [_mote({"when": "2026-08-26 09:00/PT1H"})]
    assert earliest_free_slot(
        motes, day=date(2026, 8, 26), duration=timedelta(minutes=60), bindings=["when"],
        work_start=time(7, 0), work_end=time(19, 0),
    ) == datetime(2026, 8, 26, 7, 0)


def test_a_binding_that_is_switched_off_does_not_make_a_day_busy():
    motes = [_mote({"when": "2026-08-26 09:00/PT2H"})]
    assert earliest_free_slot(
        motes, day=date(2026, 8, 26), duration=timedelta(minutes=60), bindings=["due"],
    ) == datetime(2026, 8, 26, 9, 0)

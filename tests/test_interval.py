"""core/interval.py -- when something happens, as one ISO 8601 string.

Pure string <-> structure: no Mote, no connection, not even a property
key. That is the point of the module living below core/calendar.py, and
these tests are what it buys.

The load-bearing group is "reading what gardens already hold": every
value form that was readable before this module existed has to stay
readable, because a garden is migrated on open and a garden that has not
been opened yet is still a garden.
"""
from datetime import datetime, timedelta

import pytest

from core.interval import (
    DEFAULT_TIMED_MINUTES,
    Interval,
    format,
    format_datetime,
    format_duration,
    format_like,
    moved_to,
    parse,
    parse_datetime,
    parse_duration,
    with_span,
)


# ---- reading what gardens already hold ----

def test_a_bare_datetime_is_an_interval_with_the_default_span():
    found = parse("2026-09-01T09:00")
    assert found.start == datetime(2026, 9, 1, 9, 0)
    assert found.end == datetime(2026, 9, 1, 10, 0)
    assert found.all_day is False


def test_a_bare_date_is_an_all_day_interval_of_one_day():
    found = parse("2026-09-01")
    assert found.start == datetime(2026, 9, 1)
    assert found.end == datetime(2026, 9, 2)
    assert found.all_day is True


def test_a_space_instead_of_a_t_still_reads():
    """What people actually type into a property field."""
    assert parse("2026-09-01 09:15").start == datetime(2026, 9, 1, 9, 15)


def test_seconds_are_accepted():
    assert parse("2026-09-01T09:15:30").start == datetime(2026, 9, 1, 9, 15, 30)


# ---- intervals ----

def test_a_duration_gives_the_end():
    assert parse("2026-09-01T09:00/PT1H30M").end == datetime(2026, 9, 1, 10, 30)


def test_an_explicit_end_gives_the_end():
    assert parse("2026-09-01T09:00/2026-09-01T10:30").end == datetime(2026, 9, 1, 10, 30)


def test_an_all_day_span_is_exclusive():
    """Sep 1 and Sep 2 -- as ISO 8601 means it, and as core/calendar.py
    has always computed it internally."""
    found = parse("2026-09-01/2026-09-03")
    assert found.end == datetime(2026, 9, 3)
    assert found.all_day is True


def test_the_span_is_available_directly():
    assert parse("2026-09-01T09:00/PT90M").span == timedelta(minutes=90)


# ---- rule lines ----

def test_rule_lines_ride_beneath_the_interval():
    found = parse("2026-09-01T09:00/PT1H\nRRULE:FREQ=WEEKLY;BYDAY=TU")
    assert found.rule_lines == ("RRULE:FREQ=WEEKLY;BYDAY=TU",)
    assert found.recurring is True


def test_an_exception_line_is_carried_too():
    found = parse(
        "2026-09-01T09:00/PT1H\nRRULE:FREQ=WEEKLY\nEXDATE:20260908T090000"
    )
    assert len(found.rule_lines) == 2
    assert found.recurring is True


def test_an_interval_with_no_rule_is_not_recurring():
    assert parse("2026-09-01T09:00/PT1H").recurring is False


def test_blank_lines_are_ignored():
    assert parse("2026-09-01\n\nRRULE:FREQ=DAILY\n").rule_lines == ("RRULE:FREQ=DAILY",)


# ---- what is not an interval ----

@pytest.mark.parametrize("value", ["", "   ", "not a date", "soon", "30", None, 30, [], {}])
def test_things_that_do_not_say_when_are_rejected(value):
    assert parse(value) is None


def test_a_boolean_is_never_an_interval():
    """`True` is an int in Python, and a mote with `done: true` has not
    said when it happens."""
    assert parse(True) is None
    assert parse_datetime(True) is None


# ---- permissiveness: a typo must not hide a mote ----

def test_an_unreadable_end_still_leaves_a_usable_start():
    found = parse("2026-09-01T09:00/lunchtime")
    assert found.start == datetime(2026, 9, 1, 9, 0)
    assert found.span == timedelta(minutes=DEFAULT_TIMED_MINUTES)


def test_an_end_before_the_start_falls_back_to_the_default_span():
    found = parse("2026-09-01T09:00/2026-08-01T09:00")
    assert found.end > found.start


# ---- durations ----

@pytest.mark.parametrize("text,expected", [
    ("PT1H30M", timedelta(minutes=90)),
    ("PT45M", timedelta(minutes=45)),
    ("PT2H", timedelta(hours=2)),
    ("P2D", timedelta(days=2)),
    ("P1DT6H", timedelta(days=1, hours=6)),
])
def test_iso_durations_parse(text, expected):
    assert parse_duration(text) == expected


@pytest.mark.parametrize("text", ["P1Y", "P3M", "P2W", "P", "PT", "", "90m", None])
def test_durations_without_a_fixed_length_are_refused(text):
    """Years and months have no fixed length, so an event lasting "one
    month" has no single answer. Weeks are simply spelled P14D."""
    assert parse_duration(text) is None


@pytest.mark.parametrize("span,all_day,expected", [
    (timedelta(minutes=90), False, "PT1H30M"),
    (timedelta(hours=2), False, "PT2H"),
    (timedelta(minutes=45), False, "PT45M"),
    (timedelta(days=2), True, "P2D"),
])
def test_durations_format(span, all_day, expected):
    assert format_duration(span, all_day=all_day) == expected


# ---- writing ----

def test_an_interval_round_trips():
    assert format(parse("2026-09-01T09:00/PT1H30M")) == "2026-09-01T09:00/PT1H30M"


def test_an_explicit_end_can_be_asked_for():
    written = format(parse("2026-09-01T09:00/PT1H30M"), explicit_end=True)
    assert written == "2026-09-01T09:00/2026-09-01T10:30"


def test_rule_lines_survive_a_round_trip():
    source = "2026-09-01T09:00/PT1H\nRRULE:FREQ=WEEKLY;BYDAY=TU"
    assert format(parse(source)) == source


# ---- format_like: an edit moves a value, it does not reformat it ----

def test_a_value_written_with_a_space_keeps_its_space():
    """A mote whose start grows a `T` every time you nudge it is a mote
    whose history is noise."""
    moved = moved_to(parse("2026-09-01 09:15"), datetime(2026, 9, 2, 11, 0))
    assert format_like("2026-09-01 09:15", moved) == "2026-09-02 11:00"


def test_a_bare_start_stays_bare_while_its_span_is_the_default():
    moved = moved_to(parse("2026-09-01"), datetime(2026, 9, 4))
    assert format_like("2026-09-01", moved) == "2026-09-04"


def test_a_bare_start_grows_a_span_once_it_has_one():
    resized = with_span(parse("2026-09-01T09:00"), timedelta(minutes=90))
    assert format_like("2026-09-01T09:00", resized) == "2026-09-01T09:00/PT1H30M"


def test_a_value_written_with_an_explicit_end_keeps_one():
    resized = with_span(parse("2026-09-01T09:00/2026-09-01T10:00"), timedelta(hours=3))
    assert format_like("2026-09-01T09:00/2026-09-01T10:00", resized) == \
        "2026-09-01T09:00/2026-09-01T12:00"


def test_a_value_written_with_a_duration_keeps_a_duration():
    resized = with_span(parse("2026-09-01T09:00/PT1H"), timedelta(hours=3))
    assert format_like("2026-09-01T09:00/PT1H", resized) == "2026-09-01T09:00/PT3H"


def test_seconds_are_kept_when_the_original_had_them():
    moved = moved_to(parse("2026-09-01T09:15:00"), datetime(2026, 9, 2, 11, 0))
    assert format_like("2026-09-01T09:15:00", moved) == "2026-09-02T11:00:00"


def test_rule_lines_survive_an_edit():
    source = "2026-09-01T09:00/PT1H\nRRULE:FREQ=WEEKLY"
    moved = moved_to(parse(source), datetime(2026, 9, 8, 9, 0))
    assert format_like(source, moved) == "2026-09-08T09:00/PT1H\nRRULE:FREQ=WEEKLY"


# ---- editing helpers ----

def test_moving_carries_the_span_along():
    moved = moved_to(parse("2026-09-01T09:00/PT90M"), datetime(2026, 9, 2, 14, 0))
    assert moved.start == datetime(2026, 9, 2, 14, 0)
    assert moved.span == timedelta(minutes=90)


def test_a_non_positive_span_is_refused_rather_than_inverted():
    """An event cannot end before it begins, and silently inverting it
    would be worse than ignoring the request."""
    original = parse("2026-09-01T09:00/PT1H")
    assert with_span(original, timedelta(0)) == original
    assert with_span(original, timedelta(minutes=-30)) == original


def test_format_datetime_respects_all_day():
    assert format_datetime(datetime(2026, 9, 1, 9, 0), all_day=True) == "2026-09-01"
    assert format_datetime(datetime(2026, 9, 1, 9, 0), all_day=False) == "2026-09-01T09:00"

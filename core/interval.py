# core/interval.py
"""When something happens, as one self-describing string.

An event used to need three properties: `date` for the start, `duration`
(or `ends`/`end`) for the span, and `recurrence` for the rule. Only the
first of those has a shape you can recognize -- a bare `30` is not
distinguishable from any other number, which is why the other two had to
be recognized by *name*, and why they were the last things standing
between MonOSS and a calendar with no reserved names in it at all.

ISO 8601 already solves this. An interval carries its own end:

    2026-09-01T09:00/PT1H30M          timed, 90 minutes
    2026-09-01T09:00/2026-09-01T10:30 timed, explicit end
    2026-09-01                        all-day, one day
    2026-09-01/2026-09-03             all-day, Sep 1 and 2

A repetition is appended as RFC 5545 lines beneath the interval, which is
where `recurrence` already kept them:

    2026-09-01T09:00/PT1H
    RRULE:FREQ=WEEKLY;BYDAY=TU
    EXDATE:20260908T090000

So a mote says when it happens in *one* property, under whatever key the
garden likes, and the calendar recognizes it the way it already
recognizes a date: by looking at the value. See docs/architecture.md.

**Ends are exclusive**, as ISO 8601 means them. That is not a new rule
here -- core/calendar.py has always converted an inclusive `ends` to an
exclusive end internally the moment it read one. Storage now matches what
the engine always computed, and a picker is free to show people the
inclusive last day they actually think in.

Pure string <-> structure. No Mote, no connection, not even a notion of a
property key -- core/calendar.py stays about *placement* and this stays
about *notation*.
"""
import re
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta

# What a mote with a start but no declared span lasts. Restated from
# core/calendar.py rather than imported, because this module is the lower
# of the two and must not depend on it.
DEFAULT_TIMED_MINUTES = 60
DEFAULT_ALL_DAY_DAYS = 1

# Lines an interval may carry beneath itself. EXDATE is the one Monastery
# writes: taking a single occurrence out of a series is what RFC 5545
# already calls an exception, so "delete this one" needs no new property.
RULE_PREFIXES = ("RRULE", "EXDATE", "RDATE", "EXRULE")

_DURATION = re.compile(
    r"^P(?:(?P<days>\d+(?:\.\d+)?)D)?"
    r"(?:T(?:(?P<hours>\d+(?:\.\d+)?)H)?"
    r"(?:(?P<minutes>\d+(?:\.\d+)?)M)?"
    r"(?:(?P<seconds>\d+(?:\.\d+)?)S)?)?$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Interval:
    """One span of time, and the rule that repeats it.

    `end` is exclusive and always present -- a value that declared no
    span gets the default one, resolved here rather than left for every
    reader to re-derive. `all_day` is a property of how it was *written*
    (a date with no time), which is what decides how it draws and how it
    is written back.
    """
    start: datetime
    end: datetime
    all_day: bool
    rule_lines: tuple[str, ...] = ()

    @property
    def span(self) -> timedelta:
        return self.end - self.start

    @property
    def recurring(self) -> bool:
        return any(line.upper().startswith("RRULE") for line in self.rule_lines)


def parse_datetime(value) -> tuple[datetime, bool] | None:
    """(when, all_day) for one ISO date or datetime, else None.

    Accepts a space between date and time as well as a `T`, because that
    is what people actually type into a property field. Booleans are
    rejected before the isoformat parsers see them: `True` is an int in
    Python, and a mote with `done: true` has not said when it happens.
    """
    if isinstance(value, bool) or not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if " " in text and "T" not in text:
        text = text.replace(" ", "T", 1)
    if "T" in text:
        try:
            return datetime.fromisoformat(text), False
        except ValueError:
            return None
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        return None
    return datetime(parsed.year, parsed.month, parsed.day), True


def parse_duration(text: str) -> timedelta | None:
    """An ISO 8601 duration (`PT1H30M`, `P2D`) as a timedelta, or None.

    Deliberately not the whole grammar: years and months are rejected,
    because neither has a fixed length and an event lasting "one month"
    has no single answer. Weeks (`P2W`) are likewise left out -- write
    `P14D`, which means exactly the same thing and always will.
    """
    if not isinstance(text, str):
        return None
    match = _DURATION.match(text.strip())
    if match is None or match.group(0) in ("P", "PT", ""):
        return None
    parts = {k: float(v) for k, v in match.groupdict().items() if v is not None}
    if not parts:
        return None
    return timedelta(
        days=parts.get("days", 0), hours=parts.get("hours", 0),
        minutes=parts.get("minutes", 0), seconds=parts.get("seconds", 0),
    )


def format_duration(span: timedelta, *, all_day: bool = False) -> str:
    """A timedelta as ISO 8601. An all-day span is whole days (`P2D`);
    anything else is a time duration (`PT1H30M`), minutes being the
    finest unit anyone schedules in."""
    if all_day:
        return f"P{max(1, round(span.days))}D"
    total = max(60, round(span.total_seconds()))
    hours, seconds = divmod(int(total), 3600)
    minutes = seconds // 60
    if hours and minutes:
        return f"PT{hours}H{minutes}M"
    if hours:
        return f"PT{hours}H"
    return f"PT{minutes}M"


def _split_lines(value: str) -> tuple[str, tuple[str, ...]]:
    """The interval line, and the rule lines beneath it. Blank lines are
    dropped; anything that is not a recognized RFC 5545 prefix is kept
    anyway, because dateutil is the one that gets to reject it -- this
    module refusing first would just mean a rule nobody could see."""
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    if not lines:
        return "", ()
    return lines[0], tuple(lines[1:])


def parse(value) -> Interval | None:
    """The shape test: an Interval for anything that says when it
    happens, else None.

    A bare date or datetime is a perfectly good interval with a default
    span, which is what keeps every garden written before this existed
    readable without a migration having to run first.
    """
    if isinstance(value, bool) or not isinstance(value, str):
        return None
    head, rule_lines = _split_lines(value)
    if not head:
        return None

    left, slash, right = head.partition("/")
    parsed = parse_datetime(left)
    if parsed is None:
        return None
    start, all_day = parsed

    if not slash:
        span = (timedelta(days=DEFAULT_ALL_DAY_DAYS) if all_day
                else timedelta(minutes=DEFAULT_TIMED_MINUTES))
        return Interval(start, start + span, all_day, rule_lines)

    right = right.strip()
    duration = parse_duration(right)
    if duration is not None and duration > timedelta(0):
        return Interval(start, start + duration, all_day, rule_lines)

    ended = parse_datetime(right)
    if ended is None:
        # A slash with nothing usable after it is still a start. Refusing
        # the whole value would hide a mote over a typo in its end.
        span = (timedelta(days=DEFAULT_ALL_DAY_DAYS) if all_day
                else timedelta(minutes=DEFAULT_TIMED_MINUTES))
        return Interval(start, start + span, all_day, rule_lines)
    end = ended[0]
    if end <= start:
        span = (timedelta(days=DEFAULT_ALL_DAY_DAYS) if all_day
                else timedelta(minutes=DEFAULT_TIMED_MINUTES))
        return Interval(start, start + span, all_day, rule_lines)
    return Interval(start, end, all_day, rule_lines)


def format_datetime(when: datetime, *, all_day: bool, seconds: bool = False) -> str:
    if all_day:
        return when.strftime("%Y-%m-%d")
    return when.strftime("%Y-%m-%dT%H:%M:%S" if seconds else "%Y-%m-%dT%H:%M")


def format(interval: Interval, *, explicit_end: bool = False) -> str:
    """An Interval as the string a garden stores.

    `explicit_end` writes `start/end` instead of `start/duration`. Both
    are ISO and both round-trip; a duration reads better for a meeting
    ("an hour"), an explicit end for a span of days ("through Friday"),
    which is why the caller chooses rather than this guessing.
    """
    head = format_datetime(interval.start, all_day=interval.all_day)
    if explicit_end:
        head = f"{head}/{format_datetime(interval.end, all_day=interval.all_day)}"
    else:
        head = f"{head}/{format_duration(interval.span, all_day=interval.all_day)}"
    return "\n".join([head, *interval.rule_lines])


def format_like(original: str, interval: Interval, *, force_span: bool = False) -> str:
    """`interval`, written the way `original` was written.

    A garden holds strings people typed, and an edit should move a value
    rather than reformat it -- a mote whose start grows a `:00` every
    time you nudge it is a mote whose history (core/oplog.py records one
    row per changed key) is noise. So a value that said `start/end` goes
    on saying `start/end`, one that gave a bare start with no span keeps
    giving one while the span is still the default, and a `2026-09-01
    09:00` keeps its space instead of gaining a `T`.

    `force_span` writes the span even when it happens to equal the
    default. A bare start and an explicit default-length span mean the
    same thing *today*, so eliding is safe for an edit -- but not for the
    migration that folds an old `duration` in, where the number was
    something a person actually wrote down. Recording it keeps that mote
    the length it was declared to be rather than the length the default
    happens to be later.
    """
    head, _ = _split_lines(original)
    left, slash, right = head.partition("/")
    default = (timedelta(days=DEFAULT_ALL_DAY_DAYS) if interval.all_day
               else timedelta(minutes=DEFAULT_TIMED_MINUTES))

    if not slash and interval.span == default and not force_span:
        written = _match_separator(left, interval.start, interval.all_day)
        return "\n".join([written, *interval.rule_lines])

    explicit = slash and parse_duration(right.strip()) is None
    start_text = _match_separator(left, interval.start, interval.all_day)
    if explicit:
        end_text = _match_separator(right.strip(), interval.end, interval.all_day)
        return "\n".join([f"{start_text}/{end_text}", *interval.rule_lines])
    tail = format_duration(interval.span, all_day=interval.all_day)
    return "\n".join([f"{start_text}/{tail}", *interval.rule_lines])


def _match_separator(original: str, when: datetime, all_day: bool) -> str:
    """One endpoint, keeping the separator and the seconds the original
    used -- ` ` vs `T`, `09:15` vs `09:15:00`."""
    if all_day:
        return when.strftime("%Y-%m-%d")
    text = original.strip()
    separator = " " if " " in text and "T" not in text else "T"
    seconds = text.count(":") >= 2
    stamp = when.strftime("%H:%M:%S" if seconds else "%H:%M")
    return f"{when.strftime('%Y-%m-%d')}{separator}{stamp}"


def with_span(interval: Interval, span: timedelta) -> Interval:
    """The same start, lasting `span`. A non-positive span is refused by
    keeping the one it had -- an event cannot end before it begins, and
    silently inverting it would be worse than ignoring the request."""
    if span <= timedelta(0):
        return interval
    return replace(interval, end=interval.start + span)


def moved_to(interval: Interval, start: datetime) -> Interval:
    """The same span, beginning at `start`."""
    return replace(interval, start=start, end=start + interval.span)


def with_rule_lines(interval: Interval, lines) -> Interval:
    return replace(interval, rule_lines=tuple(lines))

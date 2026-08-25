# core/calendar.py
"""Motes placed on a calendar.

Where core/google_calendar.py pushes the reserved `date` property out to
Google, this reads *any* date-shaped property back in. That difference is
the whole point: an export has to pick one key to be authoritative,
because Google needs one; a view does not, and a garden where meetings
say `when`, tasks say `due` and events say `starts` is a garden a
one-key reader shows almost nothing of.

So a **binding** is any property key whose value parses as a date --
discovered per garden rather than declared, and toggled per key in the
UI. Nothing is stored to make a key a binding, exactly as nothing is
stored to make a mote a species (core/kinds.py): it is answered by
looking.

Pure by construction -- motes in, occurrences out, no connection. That
is what core/scenarios.py::apply() and core/incidence.py already do, and
it is what lets the whole calendar be tested without a window or a
database.
"""
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from dateutil.rrule import rrulestr

from core.entities import Mote
from core.google_calendar import RESERVED_KEYS

# `ends`/`end` pair with a start rather than placing a mote of their own:
# an event that runs Tuesday to Thursday is one occurrence, not two. They
# are ordinary property names, recognized only by this module -- unlike
# `date`/`duration`/`recurrence`, they are not reserved anywhere else.
END_KEYS = ("ends", "end")

# What core/google_calendar.py::event_payload assumes when a mote gives
# no duration, restated here so the calendar and the export never show a
# meeting as different lengths.
DEFAULT_TIMED_MINUTES = 60
DEFAULT_ALL_DAY_DAYS = 1

# A rule with no UNTIL/COUNT is infinite, so an expansion is always
# clipped to the window being drawn. This bounds the pathological case --
# a per-minute rule across a month view -- rather than the normal one.
MAX_OCCURRENCES_PER_MOTE = 750


@dataclass(frozen=True)
class Occurrence:
    """One placement of one mote on the calendar.

    A recurring mote produces many of these, all carrying the same
    `mote`: clicking any occurrence opens the mote itself, the same "one
    mote -> one event -> the whole series" model docs/google-calendar.md
    already describes for the export side.
    """
    mote: Mote
    binding: str
    start: datetime
    end: datetime
    all_day: bool


def parse_date_value(value) -> tuple[datetime, bool] | None:
    """(when, all_day) for a value that looks like a date, else None.

    Accepts what core/google_calendar.py accepts -- ISO 8601, a bare date
    meaning all-day -- plus a space between date and time, which is what
    people actually type into a property field. Booleans are rejected
    before the isoformat parsers see them: `True` is an int in Python,
    and a mote with `done: true` should not land on a calendar.
    """
    if not isinstance(value, str):
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


def binding_counts(motes: list[Mote]) -> dict[str, int]:
    """Every property key holding a date somewhere in the garden, with
    how many motes carry it that way.

    `ends`/`end` are excluded: they qualify a start rather than being
    one, so offering them as bindings would place the tail of a range as
    an event in its own right. Counts drive the sidebar's ordering -- the
    key a garden actually organizes itself by should sort to the top
    without anyone configuring that.
    """
    counts: dict[str, int] = {}
    for mote in motes:
        for key, value in mote.properties.items():
            if key in END_KEYS:
                continue
            if parse_date_value(value) is not None:
                counts[key] = counts.get(key, 0) + 1
    return counts


def default_bindings(motes: list[Mote]) -> list[str]:
    """Which bindings a window with no saved preference starts on: all of
    them, most-used first. Showing everything and letting the sidebar
    subtract is the right default for a view whose purpose is finding
    what you forgot was dated."""
    counts = binding_counts(motes)
    return sorted(counts, key=lambda k: (-counts[k], k))


def _duration_for(properties: dict, all_day: bool) -> timedelta:
    """The span a mote declares, read exactly as event_payload() reads it:
    `duration` is minutes for a timed mote and days for an all-day one."""
    duration = properties.get("duration")
    if isinstance(duration, bool) or not isinstance(duration, (int, float)):
        return timedelta(days=DEFAULT_ALL_DAY_DAYS) if all_day else timedelta(minutes=DEFAULT_TIMED_MINUTES)
    if duration <= 0:
        return timedelta(days=DEFAULT_ALL_DAY_DAYS) if all_day else timedelta(minutes=DEFAULT_TIMED_MINUTES)
    return timedelta(days=duration) if all_day else timedelta(minutes=duration)


def _explicit_end(properties: dict, start: datetime, all_day: bool) -> datetime | None:
    """An `ends`/`end` property, if it gives a usable end for this start.

    An all-day end is *inclusive* as people write it -- "away Aug 12 to
    Aug 14" means the 14th is an away day -- and is converted to the
    exclusive end the rest of this module uses. The Calendar API makes
    the same conversion for the same reason (event_payload()'s comment on
    it); this is that convention read in the other direction.
    """
    for key in END_KEYS:
        if key not in properties:
            continue
        parsed = parse_date_value(properties[key])
        if parsed is None:
            continue
        end, end_all_day = parsed
        if all_day and end_all_day:
            end = end + timedelta(days=1)
        return end if end > start else None
    return None


def _end_for(mote: Mote, start: datetime, all_day: bool) -> datetime:
    """Where an occurrence beginning at `start` ends: its own `ends`/`end`
    if it has a usable one, else what its `duration` implies."""
    end = _explicit_end(mote.properties, start, all_day)
    return end if end is not None else start + _duration_for(mote.properties, all_day)


def _recurrence_rule(properties: dict) -> str | None:
    """The mote's RRULE, normalized the way event_payload() normalizes it
    -- a leading `RRULE:` stripped, since RRULE-builder tools hand you
    the whole line as often as just the rule."""
    raw = properties.get("recurrence")
    if not isinstance(raw, str) or not raw.strip():
        return None
    rule = raw.strip()
    return rule[len("RRULE:"):] if rule.upper().startswith("RRULE:") else rule


def _starts(
    mote: Mote,
    start: datetime,
    span: timedelta,
    window_start: datetime,
    window_end: datetime,
) -> list[datetime]:
    """Every start this mote has that could reach the window -- one, or
    many if it recurs. Overlap with the window is decided by the caller,
    which is why these are not clipped here.

    An unparseable rule yields the single underlying start rather than
    nothing: the same permissiveness core/modoc_bind.py applies to a
    broken note, and for the same reason. A mote whose recurrence you
    typo'd should appear on its own date looking wrong, not vanish.
    """
    rule = _recurrence_rule(mote.properties)
    if rule is None:
        return [start]
    try:
        expansion = rrulestr(rule, dtstart=start)
    except (ValueError, TypeError):
        return [start]
    # Widened by the span before clipping: an occurrence that *began*
    # before the window can still run into it, and a query starting at
    # window_start would drop the three-day event that started the day
    # before this week.
    return list(expansion.between(
        window_start - span, window_end, inc=True
    ))[:MAX_OCCURRENCES_PER_MOTE]


def occurrences(
    motes: list[Mote],
    *,
    bindings: list[str],
    window_start: datetime,
    window_end: datetime,
    exclude_ids: set[str] | None = None,
) -> list[Occurrence]:
    """Every placement *overlapping* [window_start, window_end).

    Overlapping rather than starting inside: an event that began last
    week and runs into this one belongs on the days it covers, and a
    reader that only asked where things start would drop it entirely.

    One mote can appear more than once legitimately -- under two
    bindings (a task both starting and due), or as several occurrences of
    one rule. Neither is deduplicated: the calendar is showing what the
    properties say, and a task whose `starts` and `due` are different
    days genuinely occupies both.

    `exclude_ids` is the same exclusion core/google_calendar.py::
    exportable_motes applies -- templates and scenario overrides hold
    replacement values rather than real data, so a dated one must not
    draw as something that happened.
    """
    excluded = exclude_ids or set()
    enabled = [b for b in bindings if b not in END_KEYS]
    found: list[Occurrence] = []

    for mote in motes:
        if mote.id in excluded:
            continue
        for binding in enabled:
            if binding not in mote.properties:
                continue
            parsed = parse_date_value(mote.properties[binding])
            if parsed is None:
                continue
            start, all_day = parsed
            span = _end_for(mote, start, all_day) - start
            for occurrence_start in _starts(mote, start, span, window_start, window_end):
                # Overlap, not containment: an event that began before
                # this week and runs into it belongs on the days it
                # covers. Half-open at both ends, so a placement ending
                # exactly at window_start is already over.
                if occurrence_start >= window_end or occurrence_start + span <= window_start:
                    continue
                found.append(Occurrence(
                    mote=mote,
                    binding=binding,
                    start=occurrence_start,
                    end=occurrence_start + span,
                    all_day=all_day,
                ))

    return sorted(found, key=lambda o: (o.start, not o.all_day, o.mote.id))


def undated_count(motes: list[Mote], *, exclude_ids: set[str] | None = None) -> int:
    """Motes carrying no date-shaped property at all -- the status bar's
    "without a date property" figure. Asked of every key rather than only
    the enabled bindings: a mote hidden because you toggled `due` off is
    still a dated mote, and counting it as undated would make the number
    move for the wrong reason."""
    excluded = exclude_ids or set()
    return sum(
        1 for mote in motes
        if mote.id not in excluded and not any(
            key not in END_KEYS and parse_date_value(value) is not None
            for key, value in mote.properties.items()
        )
    )


def is_calendar_property(key: str) -> bool:
    """Whether a property key is one the calendar itself writes or reads
    structurally, rather than user data the detail panel should list
    plainly. Used only for presentation."""
    return key in RESERVED_KEYS or key in END_KEYS

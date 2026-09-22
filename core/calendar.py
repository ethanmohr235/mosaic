# core/calendar.py
"""Motes placed on a calendar.

This reads *any* property whose value says when something happens,
rather than one property named `date`. A garden where meetings say
`when`, tasks say `due` and events say `starts` is a garden a one-key
reader shows almost nothing of.

So a **binding** is any property key whose value parses as an interval
(core/interval.py) -- discovered per garden rather than declared, and
toggled per key in the UI. Nothing is stored to make a key a binding,
exactly as nothing is stored to make a mote a species (core/kinds.py):
it is answered by looking.

There are no reserved names left in here at all. A start, a span and a
repetition used to be three properties -- `date`, `duration`/`ends`, and
`recurrence` -- and only the first had a shape you could recognize,
which is why the other two had to be recognized by name. ISO 8601
interval notation folds all three into one self-describing value, so the
whole of "when" is now discovered the same way. Gardens written the old
way are folded into the new one on open (core/db.py::_migrate_intervals).

Reading has always been general like this; *writing* used to be the
exception, pinned to `date` because Google Calendar export needed one
authoritative key. That export is gone (nothing in MonOSS reaches the
network any more), and with it the last reason to treat one key as more
real than another.

Pure by construction -- motes in, occurrences out, no connection. That
is what core/scenarios.py::apply() and core/incidence.py already do, and
it is what lets the whole calendar be tested without a window or a
database.
"""
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from dateutil.rrule import rrulestr

from core import interval as interval_repo
from core.entities import Mote

# What a mote with a start but no declared span lasts. core/interval.py
# owns these; re-exported because callers reach for them here.
DEFAULT_TIMED_MINUTES = interval_repo.DEFAULT_TIMED_MINUTES
DEFAULT_ALL_DAY_DAYS = interval_repo.DEFAULT_ALL_DAY_DAYS

# The key a *new* mote's start is written to. Reading is general; a new
# date still has one right home, and having two would be how a garden
# ends up with the same event under three keys. This is a default, not a
# reserved name -- nothing reads it specially.
DEFAULT_BINDING = "date"

# The day earliest_free_slot() schedules into when its caller names no
# other. A default rather than a constant: whose working day this is is
# the caller's answer, and a garden can say otherwise.
DEFAULT_WORK_START = time(9, 0)
DEFAULT_WORK_END = time(17, 0)

# A rule with no UNTIL/COUNT is infinite, so an expansion is always
# clipped to the window being drawn. This bounds the pathological case --
# a per-minute rule across a month view -- rather than the normal one.
MAX_OCCURRENCES_PER_MOTE = 750

_WEEKDAY_CODES = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")

# A dtstart for parse-only checks. Any value works -- rrulestr() needs
# one to build a rule it will never expand -- and a constant keeps
# is_recurring() from depending on what time it is.
_PARSE_ANCHOR = datetime(2000, 1, 1)


@dataclass(frozen=True)
class Occurrence:
    """One placement of one mote on the calendar.

    A recurring mote produces many of these, all carrying the same
    `mote`: clicking any occurrence opens the mote itself, the same "one
    mote -> one event -> the whole series" model the whole module works
    in.
    """
    mote: Mote
    binding: str
    start: datetime
    end: datetime
    all_day: bool
    # Whether this placement came out of a rule rather than standing
    # alone. Answered here, where the rule was parsed anyway, so
    # Monastery can ask "this occurrence or the whole series?" without
    # parsing anything in JavaScript.
    recurring: bool = False


def parse_date_value(value) -> tuple[datetime, bool] | None:
    """(when, all_day) for a value that says when something starts, else
    None. The shape test a binding is recognized by, kept as a named
    function because several callers want only the start."""
    found = interval_repo.parse(value)
    return None if found is None else (found.start, found.all_day)


def binding_counts(motes: list[Mote]) -> dict[str, int]:
    """Every property key holding an interval somewhere in the garden,
    with how many motes carry it that way.

    Counts drive the sidebar's ordering -- the key a garden actually
    organizes itself by should sort to the top without anyone
    configuring that.
    """
    counts: dict[str, int] = {}
    for mote in motes:
        for key, value in mote.properties.items():
            if interval_repo.parse(value) is not None:
                counts[key] = counts.get(key, 0) + 1
    return counts


def default_bindings(motes: list[Mote]) -> list[str]:
    """Which bindings a window with no saved preference starts on: all of
    them, most-used first. Showing everything and letting the sidebar
    subtract is the right default for a view whose purpose is finding
    what you forgot was dated."""
    counts = binding_counts(motes)
    return sorted(counts, key=lambda k: (-counts[k], k))


def _rule_text(found: interval_repo.Interval) -> str | None:
    """An interval's rule lines as one block ready for rrulestr(), or
    None when it does not repeat.

    A bare `FREQ=...` gets `RRULE:` *added* rather than stripped: once a
    value can hold exception lines too, an untagged first line is
    ambiguous, and rrulestr() reads a tagged one just as happily either
    way.

    `UNTIL` also gets its trailing `Z` stripped when present. A rule
    written elsewhere commonly puts `UNTIL` in UTC, but a mote's start is
    always naive, and a UTC-anchored UNTIL paired with a naive dtstart is
    exactly the mix dateutil refuses to compare -- rrulestr() raises and
    _starts() falls back to a single occurrence, silently dropping every
    repetition.
    """
    if not found.rule_lines:
        return None
    lines = []
    for line in found.rule_lines:
        text = line.strip()
        if not text.upper().startswith(("RRULE:", *interval_repo.RULE_PREFIXES[1:])):
            text = f"RRULE:{text}"
        lines.append(re.sub(r"(UNTIL=\d{8}T\d{6})Z\b", r"\1", text, flags=re.IGNORECASE))
    return "\n".join(lines)


def _starts(
    found: interval_repo.Interval,
    window_start: datetime,
    window_end: datetime,
) -> list[datetime]:
    """Every start this interval has that could reach the window -- one,
    or many if it repeats. Overlap with the window is decided by the
    caller, which is why these are not clipped here.

    An unparseable rule yields the single underlying start rather than
    nothing: the same permissiveness core/modoc_bind.py applies to a
    broken note, and for the same reason. A mote whose rule you typo'd
    should appear on its own date looking wrong, not vanish.
    """
    rule = _rule_text(found)
    if rule is None:
        return [found.start]
    try:
        expansion = rrulestr(rule, dtstart=found.start, forceset=True)
    except (ValueError, TypeError):
        return [found.start]
    # Widened by the span before clipping: an occurrence that *began*
    # before the window can still run into it, and a query starting at
    # window_start would drop the three-day event that started the day
    # before this week.
    return list(expansion.between(
        window_start - found.span, window_end, inc=True
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
    bindings (a task both starting and due), or as several repetitions of
    one rule. Neither is deduplicated: the calendar is showing what the
    properties say, and a task whose `starts` and `due` are different
    days genuinely occupies both.

    `exclude_ids` keeps templates and scenario overrides off the grid --
    they hold replacement values rather than real data, so a dated one
    must not draw as something that happened.
    """
    excluded = exclude_ids or set()
    found: list[Occurrence] = []

    for mote in motes:
        if mote.id in excluded:
            continue
        for binding in bindings:
            placed = interval_repo.parse(mote.properties.get(binding))
            if placed is None:
                continue
            span = placed.span
            for occurrence_start in _starts(placed, window_start, window_end):
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
                    all_day=placed.all_day,
                    recurring=placed.recurring,
                ))

    return sorted(found, key=lambda o: (o.start, not o.all_day, o.mote.id))


def undated_count(motes: list[Mote], *, exclude_ids: set[str] | None = None) -> int:
    """Motes carrying no interval-shaped property at all -- the status
    bar's "without a date property" figure. Asked of every key rather
    than only the enabled bindings: a mote hidden because you toggled
    `due` off is still a dated mote, and counting it as undated would
    make the number move for the wrong reason."""
    excluded = exclude_ids or set()
    return sum(
        1 for mote in motes
        if mote.id not in excluded and not any(
            interval_repo.parse(value) is not None
            for value in mote.properties.values()
        )
    )


def earliest_free_slot(
    motes: list[Mote],
    *,
    day: date,
    duration: timedelta,
    bindings: list[str],
    work_start: time = DEFAULT_WORK_START,
    work_end: time = DEFAULT_WORK_END,
    exclude_ids: set[str] | None = None,
) -> datetime | None:
    """The first moment on `day` with `duration` free after it, or None if
    the day cannot fit it.

    This is what Momentum means by dropping a task on a day: the person
    chooses the day, and the earliest gap that is actually free chooses
    the time. It reads the same occurrences the calendar draws, through
    the same bindings, so "busy" means exactly what Monastery shows as
    busy -- there is no second notion of a booked hour.

    All-day placements are not busy. A task `due` on Thursday marks the
    day rather than occupying it, and treating one as a booking would
    make every day carrying a deadline unschedulable.

    Pure like everything above it: motes in, a datetime out. The work
    window is a parameter rather than a constant read from somewhere
    because whose day this is belongs to the caller
    (momentum/api.py reads it off the garden's settings mote).
    """
    window_start = datetime.combine(day, work_start)
    window_end = datetime.combine(day, work_end)
    if duration <= timedelta(0) or window_start + duration > window_end:
        return None

    busy = sorted(
        (o.start, o.end)
        for o in occurrences(
            motes,
            bindings=bindings,
            window_start=window_start,
            window_end=window_end,
            exclude_ids=exclude_ids,
        )
        if not o.all_day
    )

    # Walk the day, keeping a cursor at the earliest still-free moment.
    # Overlapping placements merge for free: the cursor only ever moves
    # forward, so two meetings sharing an hour close one gap, not two.
    cursor = window_start
    for start, end in busy:
        if start - cursor >= duration:
            return cursor
        cursor = max(cursor, end)
        if cursor + duration > window_end:
            return None
    return cursor if cursor + duration <= window_end else None


def conflicts(
    motes: list[Mote],
    *,
    bindings: list[str],
    window_start: datetime,
    window_end: datetime,
    exclude_ids: set[str] | None = None,
) -> list[tuple[Occurrence, Occurrence]]:
    """Every pair of timed placements in the window that overlap.

    Pure, like everything above it: occurrences in, pairs out, so a
    caller can both count collisions for a toast and know exactly who
    is in each one. All-day placements are excluded for the same reason
    `earliest_free_slot` never treats them as busy -- a task `due`
    Thursday marks the day rather than occupying an hour of it, so it
    cannot conflict with anything.

    A sweep over occurrences sorted by start, the same walk
    `earliest_free_slot` does across one day's busy list, but here
    every open occurrence is compared against whichever earlier ones
    have not yet ended -- one pair per actual overlap, not one entry
    per participant.
    """
    timed = sorted(
        (o for o in occurrences(
            motes, bindings=bindings,
            window_start=window_start, window_end=window_end,
            exclude_ids=exclude_ids,
        ) if not o.all_day),
        key=lambda o: (o.start, o.end),
    )

    found: list[tuple[Occurrence, Occurrence]] = []
    open_occurrences: list[Occurrence] = []
    for occurrence in timed:
        open_occurrences = [o for o in open_occurrences if o.end > occurrence.start]
        for other in open_occurrences:
            if other.mote.id != occurrence.mote.id:
                found.append((other, occurrence))
        open_occurrences.append(occurrence)
    return found


def is_calendar_value(value) -> bool:
    """Whether a property value is one the calendar reads structurally,
    rather than user data the detail panel should list plainly. Used only
    for presentation.

    Asked about the **value**, not the key -- which is the whole of what
    changed here. There is no list of calendar property names any more to
    ask about, so the badge a panel shows moves from "this key is
    reserved" to "this value is a recognized shape."
    """
    return interval_repo.parse(value) is not None


# ---- writing -------------------------------------------------------
#
# Monastery lets you drag a rectangle; everything below says what that
# means in properties. Still pure -- a mote and a datetime or two in, a
# dict of properties out -- so what a drag *means* is tested in
# tests/test_calendar.py without a window, and monastery/api.py only has
# to write down what it is handed.
#
# Which key a drag rewrites is the binding that placed the rectangle.
# Reading is deliberately general (a task placed by `due` is a task on
# the grid), so an edit of what you can see has to be an edit of the key
# you can see it by -- writing somewhere else would leave `due` where it
# was and place the mote twice. And because a value now carries its own
# span and rule, every one of these is a single write to a single key.


def is_recurring(mote: Mote, binding: str) -> bool:
    """Whether this placement is a series rather than a single event,
    which is what decides whether Monastery asks "this occurrence, or the
    whole series?" before writing.

    A rule that does not parse is deliberately *not* recurring: it
    already draws as one placement on its own date (_starts()), and
    offering to except an occurrence out of it would be offering to edit
    something the calendar never drew.
    """
    found = interval_repo.parse(mote.properties.get(binding))
    if found is None:
        return False
    rule = _rule_text(found)
    if rule is None:
        return False
    try:
        rrulestr(rule, dtstart=_PARSE_ANCHOR, forceset=True)
    except (ValueError, TypeError):
        return False
    return True


def _exception_line(when: datetime, all_day: bool) -> str:
    """One occurrence, said the way RFC 5545 says it. An all-day start
    gets the `VALUE=DATE` form because that is what it actually is -- a
    date, not midnight -- and an exclusion only excludes a start it
    compares equal to."""
    if all_day:
        return f"EXDATE;VALUE=DATE:{when.strftime('%Y%m%d')}"
    return f"EXDATE:{when.strftime('%Y%m%dT%H%M%S')}"


def with_exception(mote: Mote, binding: str, occurrence_start: datetime) -> dict:
    """The binding's value with one more occurrence taken out of it, or
    `{}` when there is no rule to take it out of -- a mote that does not
    recur has nothing to except, and the caller deletes the mote itself
    instead (monastery/api.py::delete_occurrence).

    Excluding the *anchor* occurrence is allowed and works: the series
    goes on expanding from a start that no longer places anything, which
    is exactly what RFC 5545 means by an EXDATE on DTSTART. Excluding one
    twice is a no-op rather than a second identical line.
    """
    found = interval_repo.parse(mote.properties.get(binding))
    if found is None or not found.rule_lines:
        return {}
    line = _exception_line(occurrence_start, found.all_day)
    if line in found.rule_lines:
        return {}
    updated = interval_repo.with_rule_lines(found, [*found.rule_lines, line])
    return {binding: interval_repo.format_like(mote.properties[binding], updated)}


def _shift_byday(rule: str, from_weekday: int, to_weekday: int) -> str:
    """A rule's `BYDAY` moved off one weekday and onto another.

    Dragging Monday's standup to Tuesday has to rewrite `BYDAY=MO`, or
    the shifted dtstart is overruled by the rule and dateutil goes on
    producing Mondays -- the drag would appear to do nothing. Only the
    tokens naming the day you moved are touched, so `BYDAY=MO,WE,FR`
    dragged off Monday becomes `TU,WE,FR` rather than collapsing to one
    day, and an ordinal token ("-1FR") keeps its ordinal.
    """
    old, new = _WEEKDAY_CODES[from_weekday], _WEEKDAY_CODES[to_weekday]

    def replace(match: re.Match) -> str:
        moved: list[str] = []
        for token in (t.strip() for t in match.group(1).split(",")):
            if not token:
                continue
            day = token[:-2] + new if token[-2:].upper() == old else token
            if day not in moved:
                moved.append(day)
        return f"BYDAY={','.join(moved)}"

    return re.sub(r"BYDAY=([A-Za-z0-9,+\-]+)", replace, rule, flags=re.IGNORECASE)


def span_of(mote: Mote, binding: str) -> timedelta | None:
    """How long each of this mote's placements under `binding` lasts.

    The same answer occurrences() hands every start, asked without a
    window, so a move can carry forward a span it is not changing.
    """
    found = interval_repo.parse(mote.properties.get(binding))
    return None if found is None else found.span


def moved_properties(
    mote: Mote, binding: str, occurrence_start: datetime, new_start: datetime
) -> dict:
    """The series shifted so that the occurrence at `occurrence_start`
    lands on `new_start`.

    A shift, not a reassignment: every other occurrence moves by the same
    delta, which is what makes dragging any occurrence of a series mean
    the same thing as dragging its first. The span travels with it -- a
    move does not change a length -- and a rule naming its weekdays has
    those moved too, or the shifted anchor would be silently overruled.
    """
    found = interval_repo.parse(mote.properties.get(binding))
    if found is None:
        return {}
    delta = new_start - occurrence_start
    if not delta:
        return {}
    moved = interval_repo.moved_to(found, found.start + delta)

    if found.start.weekday() != moved.start.weekday():
        shifted = [
            _shift_byday(line, found.start.weekday(), moved.start.weekday())
            for line in moved.rule_lines
        ]
        if shifted != list(moved.rule_lines):
            moved = interval_repo.with_rule_lines(moved, shifted)

    return {binding: interval_repo.format_like(mote.properties[binding], moved)}


def resized_properties(
    mote: Mote, binding: str, occurrence_start: datetime, new_end: datetime
) -> dict:
    """The mote's span changed so that the occurrence beginning at
    `occurrence_start` now ends at `new_end`.

    One span, every occurrence: core reads a single length off the value
    and hands it to each start, so lengthening one Tuesday of a weekly
    meeting lengthens all of them. That is the same "one mote is the
    whole series" the rest of this module means, and the reason Monastery
    asks first when a mote recurs.
    """
    found = interval_repo.parse(mote.properties.get(binding))
    if found is None:
        return {}
    span = new_end - occurrence_start
    if span <= timedelta(0):
        return {}
    resized = interval_repo.with_span(found, span)
    # force_span: a resize is an explicit statement about length, so it
    # is written down even when it lands on the default -- otherwise
    # dragging an event back to an hour would erase the span rather than
    # set it.
    return {binding: interval_repo.format_like(
        mote.properties[binding], resized, force_span=True
    )}


def detached_properties(
    mote: Mote, binding: str, new_start: datetime, new_end: datetime
) -> dict:
    """The properties that make one occurrence stand on its own -- what
    monastery/api.py copies the mote with when you move or resize a
    single occurrence instead of the series.

    The copy is a plain mote at a plain time: the rule lines are dropped
    here rather than by the caller, since a detached occurrence that kept
    them would go on recurring. Everything else the original said is
    kept, because a Tuesday moved out of a weekly standup is still that
    standup.
    """
    found = interval_repo.parse(mote.properties.get(binding))
    if found is None:
        return {}
    span = new_end - new_start
    detached = interval_repo.with_rule_lines(
        interval_repo.moved_to(found, new_start), ()
    )
    if span > timedelta(0):
        detached = interval_repo.with_span(detached, span)
    return {binding: interval_repo.format_like(
        mote.properties[binding], detached, force_span=True
    )}

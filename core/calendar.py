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
import re
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

# Lines a `recurrence` property may carry besides the rule itself.
# EXDATE is the one Monastery writes: taking a single occurrence out of a
# series is what RFC 5545 already calls an exception, so "delete this
# one" needed no new reserved property -- `recurrence` could always say
# it, and dateutil could always read it (rrulestr(..., forceset=True)).
EXCEPTION_PREFIXES = ("EXDATE", "RDATE", "EXRULE")

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
    # Whether this placement came out of a rule rather than standing
    # alone. Answered here, where the rule was parsed anyway, so
    # Monastery can ask "this occurrence or the whole series?" without
    # parsing anything in JavaScript.
    recurring: bool = False


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


def _recurrence_lines(raw) -> list[str]:
    """A `recurrence` property as the RFC 5545 lines it stands for, each
    one carrying its own tag.

    A bare `FREQ=...` gets `RRULE:` *added* rather than stripped -- the
    way event_payload() adds it back when it pushes. Once the property
    can hold exception lines too (see EXCEPTION_PREFIXES), an untagged
    first line is ambiguous, and rrulestr() reads a tagged one just as
    happily either way.

    `UNTIL` also gets its trailing `Z` stripped when present. Google
    Calendar always writes `UNTIL` in UTC, but parse_date_value() never
    attaches tzinfo to a mote's `date`, so a UTC-anchored UNTIL paired
    with our always-naive dtstart is exactly the mix dateutil refuses to
    compare -- rrulestr() raises and _starts() falls back to a single
    occurrence, silently dropping every recurrence a Google sync wrote.
    """
    if not isinstance(raw, str) or not raw.strip():
        return []
    lines = []
    for line in raw.splitlines():
        text = line.strip()
        if not text:
            continue
        upper = text.upper()
        if not upper.startswith("RRULE:") and not upper.startswith(EXCEPTION_PREFIXES):
            text = f"RRULE:{text}"
        lines.append(re.sub(r"(UNTIL=\d{8}T\d{6})Z\b", r"\1", text, flags=re.IGNORECASE))
    return lines


def _recurrence_rule(properties: dict) -> str | None:
    """The mote's recurrence as one normalized block, ready for
    rrulestr(), or None if it has none."""
    lines = _recurrence_lines(properties.get("recurrence"))
    return "\n".join(lines) if lines else None


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
        expansion = rrulestr(rule, dtstart=start, forceset=True)
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
        recurring = is_recurring(mote)
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
                    recurring=recurring,
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


# ---- writing -------------------------------------------------------
#
# Monastery lets you drag a rectangle; everything below says what that
# means in properties. Still pure -- a mote and a datetime or two in, a
# dict of properties out -- so what a drag *means* is tested in
# tests/test_calendar.py without a window, and monastery/api.py only has
# to write down what it is handed.
#
# Which key a drag rewrites is the binding that placed the rectangle,
# never `date`. Reading is deliberately general (a task placed by `due`
# is a task on the grid), so an edit of what you can see has to be an
# edit of the key you can see it by -- writing `date` instead would
# leave `due` where it was and place the mote twice. Creating a mote is
# the other way round and stays that way: one right home for a new date
# (monastery/api.py::create_mote).

_WEEKDAY_CODES = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")

# A dtstart for parse-only checks. Any value works -- rrulestr() needs
# one to build a rule it will never expand -- and a constant keeps
# is_recurring() from depending on what time it is.
_PARSE_ANCHOR = datetime(2000, 1, 1)


def is_recurring(mote: Mote) -> bool:
    """Whether this mote is a series rather than a single placement,
    which is what decides whether Monastery asks "this occurrence, or the
    whole series?" before writing.

    A rule that does not parse is deliberately *not* recurring: it
    already draws as one placement on its own date (_starts()), and
    offering to except an occurrence out of it would be offering to edit
    something the calendar never drew.
    """
    rule = _recurrence_rule(mote.properties)
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


def with_exception(properties: dict, occurrence_start: datetime, all_day: bool) -> str | None:
    """The mote's `recurrence` with one more occurrence taken out of it,
    or None when there is no rule to take it out of -- a mote that does
    not recur has nothing to except, and the caller deletes the mote
    itself instead (monastery/api.py::delete_occurrence).

    Excluding the *anchor* occurrence is allowed and works: the series
    goes on expanding from a `date` that no longer places anything, which
    is exactly what RFC 5545 means by an EXDATE on DTSTART. Excluding one
    twice is a no-op rather than a second identical line.
    """
    lines = _recurrence_lines(properties.get("recurrence"))
    if not lines:
        return None
    line = _exception_line(occurrence_start, all_day)
    return "\n".join(lines if line in lines else [*lines, line])


def _format_like(original: str, when: datetime, all_day: bool) -> str:
    """`when`, written the way `original` was written.

    A garden holds dates people typed -- "2026-08-28", "2026-08-24 09:15",
    full ISO -- and a drag should move a value, not reformat it. A mote
    whose `due` grows a ":00" every time you nudge it is a mote whose
    history (core/oplog.py records one row per changed key) is noise.
    """
    if all_day:
        return when.strftime("%Y-%m-%d")
    separator = " " if " " in original and "T" not in original else "T"
    time_part = when.strftime("%H:%M:%S") if original.count(":") >= 2 else when.strftime("%H:%M")
    return f"{when.strftime('%Y-%m-%d')}{separator}{time_part}"


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


def _span_properties(properties: dict, start: datetime, all_day: bool, span: timedelta) -> dict:
    """How long something lasts, written in whichever of the two ways
    this mote already says it: an explicit `ends`/`end` if it has one,
    else `duration`.

    Rewriting the key the mote already uses is the same principle as
    writing back to the binding that placed it -- an edit should change
    what you were shown, not leave it standing and add a second answer
    beside it, which is exactly what _end_for() would then have to choose
    between.
    """
    for key in END_KEYS:
        if parse_date_value(properties.get(key)) is not None:
            end = start + span
            # `ends` is inclusive on an all-day mote as people write it
            # (_explicit_end() reads it back that way), so the exclusive
            # span this module works in is converted before it is written.
            return {key: _format_like(
                properties[key], end - timedelta(days=1) if all_day else end, all_day
            )}
    length = span.days if all_day else round(span.total_seconds() / 60)
    return {"duration": max(1, length)}


def span_of(mote: Mote, binding: str) -> timedelta | None:
    """How long each of this mote's placements under `binding` lasts --
    its `ends`/`end` if it declares one, else what `duration` implies.

    The same answer occurrences() hands every start (_end_for()), asked
    without a window, so a move can carry forward a span it is not
    changing.
    """
    parsed = parse_date_value(mote.properties.get(binding))
    if parsed is None:
        return None
    start, all_day = parsed
    return _end_for(mote, start, all_day) - start


def moved_properties(
    mote: Mote, binding: str, occurrence_start: datetime, new_start: datetime
) -> dict:
    """The series shifted so that the occurrence at `occurrence_start`
    lands on `new_start`.

    A shift, not a reassignment: every other occurrence moves by the same
    delta, which is what makes dragging any occurrence of a series mean
    the same thing as dragging its first. `ends`/`end` travels with it --
    a move does not change a span -- and a rule naming its weekdays has
    those moved too, or the shifted anchor would be silently overruled.
    """
    parsed = parse_date_value(mote.properties.get(binding))
    if parsed is None:
        return {}
    anchor, all_day = parsed
    delta = new_start - occurrence_start
    if not delta:
        return {}
    moved_anchor = anchor + delta
    moved = {binding: _format_like(mote.properties[binding], moved_anchor, all_day)}

    for key in END_KEYS:
        end = parse_date_value(mote.properties.get(key))
        if end is not None:
            moved[key] = _format_like(mote.properties[key], end[0] + delta, end[1])
            break

    rule = mote.properties.get("recurrence")
    if isinstance(rule, str) and rule.strip() and anchor.weekday() != moved_anchor.weekday():
        shifted = _shift_byday(rule, anchor.weekday(), moved_anchor.weekday())
        if shifted != rule:
            moved["recurrence"] = shifted
    return moved


def resized_properties(
    mote: Mote, binding: str, occurrence_start: datetime, new_end: datetime
) -> dict:
    """The mote's span changed so that the occurrence beginning at
    `occurrence_start` now ends at `new_end`.

    One span, every occurrence: core reads a single length off the mote
    and hands it to each start (_end_for()), so lengthening one Tuesday
    of a weekly meeting lengthens all of them. That is the same "one mote
    is the whole series" the export side has always meant, and the reason
    Monastery asks first when a mote recurs.
    """
    parsed = parse_date_value(mote.properties.get(binding))
    if parsed is None:
        return {}
    anchor, all_day = parsed
    span = new_end - occurrence_start
    if span <= timedelta(0):
        return {}
    return _span_properties(mote.properties, anchor, all_day, span)


def detached_properties(
    mote: Mote, binding: str, new_start: datetime, new_end: datetime
) -> dict:
    """The properties that make one occurrence stand on its own -- what
    monastery/api.py copies the mote with when you move or resize a
    single occurrence instead of the series.

    The copy is a plain mote at a plain time: the caller drops
    `recurrence` (it is one event now, not a series) and `gcal_event_id`
    (two motes must never claim one Google event). Everything else the
    original said is kept, because a Tuesday moved out of a weekly
    standup is still that standup.
    """
    parsed = parse_date_value(mote.properties.get(binding))
    if parsed is None:
        return {}
    _, all_day = parsed
    detached = {binding: _format_like(mote.properties[binding], new_start, all_day)}
    detached.update(_span_properties(mote.properties, new_start, all_day, new_end - new_start))
    return detached

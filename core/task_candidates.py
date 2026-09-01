# core/task_candidates.py
"""Which property key means "status", "priority" or "estimate" in a
garden -- answered by looking, not by reserving a name.

core/calendar.py established the shape: a *binding* is any key whose
value parses as a date, discovered per garden and toggled in the UI,
with nothing stored to make a key one. This asks the same question about
the two shapes a task board needs and a date parser cannot recognize.

The two shapes are genuinely different questions, which is why this is a
module rather than three more functions over there:

- an **enum key** holds a short label out of a small repeating set
  (`status: doing`, `priority: high`). Recognized by the *shape of the
  set*, not by any vocabulary -- a garden whose statuses are `icebox`
  and `shipped` is as recognizable as one saying `todo` and `done`.
- an **estimate key** holds how long something will take (`5h`, `90m`,
  `1d`, or a bare number of minutes).

Nothing here decides *the* answer for a garden. It ranks candidates and
names a default; the window offers them and the person confirms, exactly
as Monastery's sidebar offers bindings. A wrong guess must be one click
from being right, not a migration.

Pure by construction -- motes in, candidates out, no connection -- the
same contract core/calendar.py and core/scenarios.py keep, and what lets
all of this be tested without a window or a database.
"""
import re
from dataclasses import dataclass

from core.entities import Mote
from core.interval import parse as parse_interval
from core.links import URL_PROPERTY

# How long a value can be and still read as a label rather than prose.
# A status is a word or two; a sentence in a `notes` property is not a
# column heading, however few motes happen to share it.
MAX_LABEL_LENGTH = 24

# How many distinct values a key may hold and still be an enum. Above
# this it is free text that happens to repeat (a `name`, an `owner`),
# and a board built from it would have a column per mote.
MAX_DISTINCT_VALUES = 12

# What a bare estimate number means. Minutes, because that is the unit
# core/calendar.py's `duration` already uses for a timed mote, and two
# readings of `estimate: 90` in one app would be indefensible.
BARE_ESTIMATE_UNIT_MINUTES = 1

# A day of *work*, not 24 hours: `1d` on a task means a day spent on it.
# The mockup this was designed against reads it the same way.
HOURS_PER_ESTIMATE_DAY = 8

# Where a garden with no history starts. These are a *preference among
# discovered candidates*, never a requirement: a key named `status` wins
# a tie, but a garden that calls it `state` is fully supported and the
# board says `state` everywhere. See default_enum_key().
PREFERRED_STATUS_NAMES = ("status", "state", "stage", "column")
PREFERRED_PRIORITY_NAMES = ("priority", "importance", "urgency")
PREFERRED_ESTIMATE_NAMES = ("estimate", "estimated", "effort", "size")

# The columns a board falls back to when its status key holds no values
# yet -- a brand new garden has nothing to discover. Ordinary strings
# written into an ordinary property, not a vocabulary the engine knows.
DEFAULT_STATUSES = ("todo", "doing", "blocked", "done")

# The order statuses sort into when a garden *does* have its own. Any
# value not named here keeps its discovered position after these, so a
# garden with an `icebox` column does not have it silently dropped.
STATUS_ORDER_HINT = ("todo", "next", "doing", "in progress", "blocked", "review", "done")

_ESTIMATE_PATTERN = re.compile(r"^([\d.]+)\s*(h|m|d|hr|hrs|min|mins|hours|minutes|days)$", re.IGNORECASE)

_ESTIMATE_UNIT_MINUTES = {
    "m": 1, "min": 1, "mins": 1, "minutes": 1,
    "h": 60, "hr": 60, "hrs": 60, "hours": 60,
    "d": HOURS_PER_ESTIMATE_DAY * 60, "days": HOURS_PER_ESTIMATE_DAY * 60,
}


@dataclass(frozen=True)
class Candidate:
    """One key that could mean what was asked, and the evidence for it.

    `values` is empty for an estimate key -- the numbers are not a set to
    choose from, unlike an enum's labels, which *are* the board's
    columns and so travel with the candidate that would build them.
    """
    key: str
    count: int
    values: tuple[str, ...] = ()


def parse_estimate_value(value) -> float | None:
    """How many minutes an estimate says, or None if it says nothing.

    Accepts what people actually type -- `5h`, `90m`, `1.5h`, `1d`, `45`
    -- because an estimate is entered in a property field like every
    other value in MonOSS, and rejecting `5h` in favour of one true
    format would just move the problem into the person's head.

    Booleans are rejected before the number path sees them, the same
    guard parse_date_value() needs and for the same reason: `True` is an
    int in Python, and a mote with `done: true` has not estimated an
    hour of work.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) * BARE_ESTIMATE_UNIT_MINUTES if value > 0 else None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    match = _ESTIMATE_PATTERN.match(text)
    if match is None:
        try:
            number = float(text)
        except ValueError:
            return None
        return number * BARE_ESTIMATE_UNIT_MINUTES if number > 0 else None
    number = float(match.group(1))
    if number <= 0:
        return None
    return number * _ESTIMATE_UNIT_MINUTES[match.group(2).lower()]


def format_estimate(minutes: float | None) -> str:
    """Minutes as a board reads them back -- `90m`, `4h`, `1.5h`. The
    inverse of parse_estimate_value() for display only; nothing writes
    this back over what a person typed, the same way a drag never
    reformats a date (core/calendar.py::_format_like). When an estimate
    genuinely has to be written back -- work logged against it leaves
    less of it -- that is format_estimate_like(), which keeps the
    notation instead of imposing this one."""
    if not minutes or minutes <= 0:
        return ""
    if minutes < 60:
        return f"{round(minutes)}m"
    hours = minutes / 60
    return f"{hours:g}h"


def format_estimate_like(original, minutes: float):
    """`minutes`, written the way `original` was written.

    The same rule core/interval.py::format_like states for a date, and
    for the same reason: a garden holds what a person typed, and an edit
    should move a value rather than reformat it. So `5h` less an hour
    comes back `4h`, `90min` less half of it comes back `45min`, and a
    bare `300` comes back a bare number -- as a string if that is how it
    was held, since a garden that writes its estimates as text should
    not find one turning into a number under it.

    The unit only survives while it stays exact. `5h` less 25 minutes is
    275, which no whole number of hours says; rounding to `4.6h` to keep
    the suffix would trade the number for the notation, which is the
    wrong way round -- what the estimate *is* outranks how it was
    spelled, so those fall back to explicit minutes. Nothing is left of
    an estimate spent down to zero, so that goes back as plain 0 rather
    than an odd-reading `0h`.
    """
    minutes = round(minutes)
    if minutes <= 0:
        return 0
    if isinstance(original, str):
        match = _ESTIMATE_PATTERN.match(original.strip())
        if match is None:
            return str(minutes)
        unit = match.group(2)
        size = _ESTIMATE_UNIT_MINUTES[unit.lower()]
        return f"{minutes // size}{unit}" if minutes % size == 0 else f"{minutes}m"
    return minutes


def _is_label(value) -> bool:
    """Whether a value reads as a short label rather than data of some
    other kind. Dates are excluded explicitly: `due: 2026-08-28` is a
    short repeating string by every other test here, and belongs to
    core/calendar.py, not to a status column."""
    if isinstance(value, bool) or not isinstance(value, str):
        return False
    text = value.strip()
    if not text or len(text) > MAX_LABEL_LENGTH:
        return False
    return parse_interval(text) is None


def _skip(key: str) -> bool:
    """Keys that are structurally the app's rather than a task's.

    Only two names left to skip, and both for the same reason: a link and
    a mote's name are ordinary strings that would otherwise read as
    labels. When something happens needs no entry here at all any more --
    an interval has a shape, so _is_label() already rejects one.
    """
    return key == URL_PROPERTY or key == "name"


def enum_candidates(motes: list[Mote]) -> list[Candidate]:
    """Every key holding a small repeating set of labels, most-used
    first -- what a board could sort itself into columns by.

    Ranked but not chosen: several keys legitimately have this shape in
    one garden (`status` and `priority` both do, which is the point),
    and which one is which is a question only the person can settle.
    """
    seen: dict[str, list[str]] = {}
    for mote in motes:
        for key, value in mote.properties.items():
            if _skip(key) or not _is_label(value):
                continue
            seen.setdefault(key, []).append(value.strip())

    found = []
    for key, values in seen.items():
        distinct = _distinct(values)
        if len(distinct) > MAX_DISTINCT_VALUES:
            continue
        found.append(Candidate(key=key, count=len(values), values=tuple(distinct)))
    return sorted(found, key=lambda c: (-c.count, c.key))


def estimate_candidates(motes: list[Mote]) -> list[Candidate]:
    """Every key holding a parseable duration, most-used first."""
    counts: dict[str, int] = {}
    for mote in motes:
        for key, value in mote.properties.items():
            if _skip(key):
                continue
            if parse_estimate_value(value) is not None:
                counts[key] = counts.get(key, 0) + 1
    return sorted(
        (Candidate(key=key, count=count) for key, count in counts.items()),
        key=lambda c: (-c.count, c.key),
    )


def _distinct(values: list[str]) -> list[str]:
    """The distinct values in first-seen order, so a garden's own
    vocabulary keeps whatever order it appears in rather than being
    alphabetised into something nobody chose."""
    ordered: list[str] = []
    for value in values:
        if value not in ordered:
            ordered.append(value)
    return ordered


def default_enum_key(
    motes: list[Mote], preferred: tuple[str, ...], *, exclude: str | None = None
) -> str | None:
    """Which enum key a window with no saved preference starts on.

    A preferred *name* wins when the garden has one, because a key
    actually called `status` almost always is the status. Otherwise the
    most-used candidate wins, which is how a garden that names it
    something else still gets a working board on first open.

    `exclude` keeps priority from defaulting to the same key status
    already took -- two boards of the same column would be one board and
    a wasted sidebar.
    """
    candidates = [c for c in enum_candidates(motes) if c.key != exclude]
    if not candidates:
        return None
    by_key = {c.key: c for c in candidates}
    for name in preferred:
        if name in by_key:
            return name
    return candidates[0].key


def default_estimate_key(motes: list[Mote]) -> str | None:
    """The estimate key a window starts on -- a preferred name if the
    garden has one, else the most-used duration-shaped key."""
    candidates = estimate_candidates(motes)
    if not candidates:
        return None
    by_key = {c.key: c for c in candidates}
    for name in PREFERRED_ESTIMATE_NAMES:
        if name in by_key:
            return name
    return candidates[0].key


def statuses_for(motes: list[Mote], key: str | None) -> list[str]:
    """The board's columns: the values that key actually holds, in a
    sensible order, or the starting set when it holds none yet.

    Known statuses lead in STATUS_ORDER_HINT's order and anything else
    follows in the order the garden introduced it. A garden with an
    `icebox` column gets an icebox column -- ordering an unknown value
    last is the most this can honestly say about it, and dropping it
    would hide motes.
    """
    if not key:
        return list(DEFAULT_STATUSES)
    found = next((c for c in enum_candidates(motes) if c.key == key), None)
    if found is None or not found.values:
        return list(DEFAULT_STATUSES)
    hint = {name: i for i, name in enumerate(STATUS_ORDER_HINT)}
    return sorted(
        found.values,
        key=lambda v: (hint.get(v.casefold(), len(hint)), found.values.index(v)),
    )


def total_estimate_minutes(motes: list[Mote], key: str | None) -> float:
    """How much work a set of motes represents, for a column's or a
    day's load figure. Motes with no estimate contribute nothing rather
    than a guessed default -- an unestimated task is unknown work, and
    inventing an hour for it would make the day look full for a reason
    nobody wrote down."""
    if not key:
        return 0.0
    total = 0.0
    for mote in motes:
        minutes = parse_estimate_value(mote.properties.get(key))
        if minutes is not None:
            total += minutes
    return total

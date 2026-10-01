# core/agenda.py
"""The calendar panes' grammar and answers (docs/console.md -> Settled for
U7).

    agenda [from] [to] [--keys a,b] [--type ‹species›] [‹find conditions›]
    week   [date]      [--keys …] [--type …] [‹conditions›]
    month  [date]      [--keys …] [--type …] [‹conditions›]

A pane is its command, so the range it shows is in the line: `[` and `]`
rewrite the date (rewrite()), and a layout reopens on the same week.
Dates are ISO days or what `find` reads as one (`today`, `7d`, `-2w`,
core/table.py::resolve_date). An agenda runs from today for two weeks by
default; a week starts on Monday; a month is the six weeks around it.

**Which dates.** Every property whose value is an interval is a *key*
(core/calendar.py calls them bindings); all of them show unless `--keys`
narrows it. `--type` keeps instances of one species, and find conditions
keep the motes they match.

**Bars.** A value spanning several whole days draws as a bar rather than
a row, and so does any mote with both a start and a `due`: its `start`
or `starts` value, else the day its project was started (when its
`garden` property was written, core/projects.py). Bars run from the
start's day through the due day.

**Edits are commands.** What a move or a resize writes is a `set` line
(edit_line()): one value, the key that placed the occurrence, in the
shape it was written (core/calendar.py's writing section). Lifting one
occurrence out of a series is the one edit a `set` can't say -- an
exception on the series and a copy where it now is -- so it is its own
verb, `detach`, one undo step."""
import calendar as calendar_lib
import shlex
import sqlite3
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from typing import Sequence

from core import calendar as calendar_repo
from core import findtext
from core import interval as interval_repo
from core import modoc
from core import motes as motes_repo
from core import projects as projects_repo
from core import query
from core import scenarios as scenarios_repo
from core import species as species_repo
from core import table as table_repo
from core import undo
from core.command_bar import CommandError
from core.entities import Mote

AGENDA, WEEK, MONTH = "agenda", "week", "month"
VIEWS = (AGENDA, WEEK, MONTH)
DETACH_VERB = "detach"
KEYS_FLAG, TYPE_FLAG = "--keys", "--type"
FLAGS = (KEYS_FLAG, TYPE_FLAG)

AGENDA_DAYS = 14
MONTH_WEEKS = 6
START_KEYS = ("start", "starts")
DUE_KEY = "due"
SPAN, DUE = "span", "due"  # what made a bar


# ---- the grammar ----------------------------------------------------------

@dataclass(frozen=True)
class CalLine:
    view: str
    start: str | None = None   # as typed: an ISO day, `today`, `7d` …
    end: str | None = None     # agenda only
    keys: tuple[str, ...] | None = None
    type: str | None = None
    conditions: tuple[str, ...] = ()

    def text(self) -> str:
        words = [self.view]
        if self.start:
            words.append(self.start)
        if self.end:
            words.append(self.end)
        if self.keys is not None:
            words += [KEYS_FLAG, ",".join(self.keys)]
        if self.type:
            words += [TYPE_FLAG, self.type]
        return shlex.join(words + list(self.conditions))


def parse(view: str, args: Sequence[str]) -> CalLine:
    if view not in VIEWS:
        raise CommandError(f"{view!r} is not a calendar view")
    dates: list[str] = []
    fields: dict = {}
    conditions: list[str] = []
    i = 0
    while i < len(args):
        word = args[i]
        if word.startswith("--"):
            if word not in FLAGS:
                raise CommandError(f"{view} takes {', '.join(FLAGS)} -- not {word}")
            if i + 1 >= len(args):
                raise CommandError(f"{word} needs a value")
            value = args[i + 1]
            i += 2
            if word == KEYS_FLAG:
                fields["keys"] = tuple(k for k in (p.strip() for p in value.split(",")) if k)
            else:
                fields["type"] = value
            continue
        i += 1
        if _is_date(word):
            dates.append(word)
        else:
            findtext.parse_condition(word)  # raises FindSyntaxError naming the word
            conditions.append(word)
    most = 2 if view == AGENDA else 1
    if len(dates) > most:
        raise CommandError(f"{view} takes {'a start and an end' if most == 2 else 'one date'}")
    return CalLine(view, dates[0] if dates else None, dates[1] if len(dates) > 1 else None,
                   conditions=tuple(conditions), **fields)


def parse_text(command: str) -> CalLine:
    try:
        words = shlex.split(command or "")
    except ValueError as e:
        raise CommandError(f"calendar: {e}") from None
    if not words:
        raise CommandError("an empty calendar line")
    return parse(words[0], words[1:])


def _is_date(word: str) -> bool:
    return _day(word, date(2000, 1, 1)) is not None


def _day(text: str | None, today: date) -> date | None:
    if not text:
        return None
    resolved = table_repo.resolve_date(text, today.isoformat())
    try:
        return date.fromisoformat(resolved[:10]) if len(resolved) >= 10 else None
    except ValueError:
        return None


# ---- the range ------------------------------------------------------------

def window(line: CalLine, today: date | None = None) -> tuple[date, date]:
    """[first day, day after the last) the line shows."""
    today = today or date.today()
    anchor = _day(line.start, today) or today
    if line.view == AGENDA:
        end = _day(line.end, today)
        end = end + timedelta(days=1) if end else anchor + timedelta(days=AGENDA_DAYS)
        if end <= anchor:
            raise CommandError("an agenda's end comes after its start")
        return anchor, end
    if line.view == WEEK:
        monday = anchor - timedelta(days=anchor.weekday())
        return monday, monday + timedelta(days=7)
    first = anchor.replace(day=1)
    start = first - timedelta(days=first.weekday())
    return start, start + timedelta(days=7 * MONTH_WEEKS)


def rewrite(line: CalLine, change: dict, today: date | None = None) -> CalLine:
    """A pane gesture as a line: `page` (+1/-1) moves a whole view's
    worth, `today` comes back to now, `key` turns one key off or back on
    (`all_keys` is every key the garden has, so the first toggle can say
    which remain)."""
    today = today or date.today()
    if "today" in change:
        return replace(line, start=None, end=None)
    if "page" in change:
        step = int(change["page"])
        start, end = window(line, today)
        if line.view == AGENDA:
            length = end - start
            new = start + length * step
            return replace(line, start=new.isoformat(),
                           end=(new + length - timedelta(days=1)).isoformat() if line.end else None)
        if line.view == WEEK:
            return replace(line, start=(start + timedelta(days=7 * step)).isoformat())
        anchor = (_day(line.start, today) or today).replace(day=1)
        month = anchor.month - 1 + step
        moved = anchor.replace(year=anchor.year + month // 12, month=month % 12 + 1)
        return replace(line, start=moved.isoformat())
    if "key" in change:
        key = change["key"]
        every = list(change.get("all_keys") or [])
        shown = list(line.keys) if line.keys is not None else every
        shown = [k for k in shown if k != key] if key in shown else [*shown, key]
        return replace(line, keys=None if set(shown) == set(every) else tuple(shown))
    raise CommandError(f"no such change: {sorted(change)}")


# ---- the answer -----------------------------------------------------------

def run(conn: sqlite3.Connection, line: CalLine, today: date | None = None) -> dict:
    today = today or date.today()
    first, last = window(line, today)
    motes, relationships, index = table_repo.read(conn)
    excluded = _unplaced(conn, relationships)
    counts = calendar_repo.binding_counts([m for m in motes if m.id not in excluded])
    every = calendar_repo.default_bindings([m for m in motes if m.id not in excluded])
    problems: list[str] = []
    keys = list(line.keys) if line.keys is not None else every
    for key in keys:
        if key not in counts:
            problems.append(f"no mote has a date under {key!r}")
    kept = _kept(conn, line, motes, relationships, index, excluded, problems)

    start_dt = datetime.combine(first, time.min)
    end_dt = datetime.combine(last, time.min)
    found = calendar_repo.occurrences(kept, bindings=keys, window_start=start_dt,
                                      window_end=end_dt, exclude_ids=excluded)
    owned = {m.id: projects_repo.garden_named_by(m.properties) for m in kept}
    rows, bars = [], []
    for o in found:
        described = table_repo.describe(index, o.mote.id)
        entry = {
            "key": f"{o.mote.id}:{o.binding}:{o.start.isoformat()}",
            "moteId": o.mote.id, "name": described["name"], "speciesId": described["speciesId"],
            "binding": o.binding, "start": o.start.isoformat(), "end": o.end.isoformat(),
            "allDay": o.all_day, "recurring": o.recurring,
            "note": modoc.has_modoc(o.mote.properties), "garden": owned.get(o.mote.id),
        }
        if o.all_day and o.end - o.start > timedelta(days=1):
            bars.append({**entry, "source": SPAN, "end": o.end.date().isoformat(),
                         "start": o.start.date().isoformat()})
            continue
        entry["day"] = max(o.start.date(), first).isoformat()
        rows.append(entry)
    bars += _due_bars(conn, kept, keys, index, first, last)
    return {
        "command": line.text(),
        "view": line.view,
        "start": first.isoformat(),
        "end": last.isoformat(),
        "today": today.isoformat(),
        "title": _title(line.view, first, last, today),
        "keys": [{"key": k, "count": counts[k], "active": k in keys} for k in every],
        "rows": rows,
        "bars": sorted(bars, key=lambda b: (b["start"], b["name"].casefold())),
        "problems": problems,
        "speciesIds": sorted(m.id for m in species_repo.list_species(conn)),
    }


def _unplaced(conn, relationships) -> set[str]:
    """Templates and scenario overrides hold replacement values, not
    things that happen, so a dated one is never placed."""
    return species_repo.template_mote_ids(conn) | scenarios_repo.override_mote_ids(relationships)


def _kept(conn, line, motes, relationships, index, excluded, problems) -> list[Mote]:
    kept = [m for m in motes if m.id not in excluded]
    if line.type:
        wanted = {m.id for m in motes if str(m.properties.get("name", "")).casefold() == line.type.casefold()}
        if not wanted:
            problems.append(f"no species named {line.type!r}")
        kept = [m for m in kept if set(index.species_ids(m.id)) & wanted]
    if line.conditions:
        spec = findtext.parse_find(shlex.join(line.conditions))
        schema = table_repo.Schema.of(index, motes, relationships)
        resolved, found = table_repo.resolve_query(spec.query, schema)
        problems.extend(found)
        matched = {m.id for m in query.evaluate(index, kept, resolved)}
        kept = [m for m in kept if m.id in matched]
    return kept


def _due_bars(conn, motes, keys, index, first: date, last: date) -> list[dict]:
    """A bar for every mote with both a start and a `due`, when `due` is
    a key being shown."""
    if DUE_KEY not in keys:
        return []
    bars = []
    for mote in motes:
        due = interval_repo.parse(mote.properties.get(DUE_KEY))
        if due is None:
            continue
        begun = _begun(conn, mote)
        if begun is None or begun > due.start.date():
            continue
        end = due.start.date() + timedelta(days=1)
        if begun >= last or end <= first:
            continue
        described = table_repo.describe(index, mote.id)
        bars.append({
            "key": f"{mote.id}:due-bar", "moteId": mote.id, "name": described["name"],
            "speciesId": described["speciesId"], "binding": DUE_KEY, "source": DUE,
            "start": begun.isoformat(), "end": end.isoformat(), "allDay": True,
            "recurring": False, "note": modoc.has_modoc(mote.properties),
            "garden": projects_repo.garden_named_by(mote.properties),
        })
    return bars


def _begun(conn, mote: Mote) -> date | None:
    for key in START_KEYS:
        found = interval_repo.parse(mote.properties.get(key))
        if found is not None:
            return found.start.date()
    if projects_repo.garden_named_by(mote.properties) is None:
        return None
    row = conn.execute(
        "SELECT updated_at FROM mote_properties WHERE mote_id = ? AND key = ?",
        (mote.id, projects_repo.GARDEN_KEY),
    ).fetchone()
    try:
        return datetime.fromisoformat(str(row[0])[:19]).date() if row and row[0] else None
    except ValueError:
        return None


def _title(view: str, first: date, last: date, today: date) -> str:
    final = last - timedelta(days=1)
    if view == MONTH:
        middle = first + timedelta(days=14)
        return f"{calendar_lib.month_name[middle.month]} {middle.year}"
    fmt = "%b %-d"
    year = f" {final.year}" if final.year != today.year or first.year != final.year else ""
    return f"{first.strftime(fmt)} – {final.strftime(fmt)}{year}"


# ---- edits ------------------------------------------------------------------

def _placed(conn, mote_id: str, binding: str) -> Mote:
    mote = motes_repo.get_mote(conn, mote_id)
    if mote is None:
        raise CommandError("that mote no longer exists")
    if interval_repo.parse(mote.properties.get(binding)) is None:
        raise CommandError(f"{binding!r} no longer holds a date")
    return mote


def edit_value(conn, mote_id: str, binding: str, occurrence_start: str,
               new_start: str, new_end: str) -> str | None:
    """The value `binding` takes when the occurrence at `occurrence_start`
    comes to rest at [new_start, new_end) -- the whole series moved and
    resized by the same amount. None when nothing changes."""
    mote = _placed(conn, mote_id, binding)
    start, target, target_end = (datetime.fromisoformat(t) for t in (occurrence_start, new_start, new_end))
    if target_end <= target:
        raise CommandError("an event ends after it starts")
    moved = calendar_repo.moved_properties(mote, binding, start, target) if target != start else {}
    after = Mote(properties={**mote.properties, **moved}, id=mote.id)
    resized = (calendar_repo.resized_properties(after, binding, target, target_end)
               if target_end - target != calendar_repo.span_of(mote, binding) else {})
    changed = {**moved, **resized}
    return changed.get(binding)


def edit_line(conn, mote_id: str, binding: str, occurrence_start: str,
              new_start: str, new_end: str) -> str | None:
    """edit_value() as the `set` line a pane runs."""
    value = edit_value(conn, mote_id, binding, occurrence_start, new_start, new_end)
    return None if value is None else _set(mote_id, binding, value)


def skip_line(conn, mote_id: str, binding: str, occurrence_start: str) -> str | None:
    """The `set` line that takes one occurrence out of a series (an
    EXDATE). None when the mote doesn't recur -- deleting it is `delete`."""
    mote = _placed(conn, mote_id, binding)
    excepted = calendar_repo.with_exception(mote, binding, datetime.fromisoformat(occurrence_start))
    return _set(mote_id, binding, excepted[binding]) if excepted else None


def _set(mote_id: str, key: str, value: str) -> str:
    return f"set #{mote_id} {shlex.quote(key)} {shlex.quote(value)}"


def slot_value(start: str, end: str, all_day: bool) -> str:
    """How a new event at [start, end) is written: `2026-10-05T09:00/PT1H`,
    or a bare day for an all-day slot."""
    s, e = datetime.fromisoformat(start), datetime.fromisoformat(end)
    if all_day and e - s == timedelta(days=1):
        return interval_repo.format_datetime(s, all_day=True)
    return interval_repo.format(interval_repo.Interval(start=s, end=e, all_day=all_day))


def detach(conn: sqlite3.Connection, args: Sequence[str]) -> tuple[str, "undo.Command"]:
    """`detach #id ‹key› ‹occurrence start› ‹new start› ‹new end›`: one
    occurrence lifted out of its series -- an exception where it was, a
    copy (species kept) where it now is. One undo step."""
    if len(args) != 5 or not args[0].startswith("#"):
        raise CommandError("usage: detach #id ‹key› ‹occurrence start› ‹new start› ‹new end›")
    mote_id, binding = args[0][1:], args[1]
    mote = _placed(conn, mote_id, binding)
    if not calendar_repo.is_recurring(mote, binding):
        raise CommandError("that event doesn't repeat -- move it with set")
    occurrence, start, end = (datetime.fromisoformat(a) for a in args[2:])
    if end <= start:
        raise CommandError("an event ends after it starts")
    steps = []
    excepted = calendar_repo.with_exception(mote, binding, occurrence)
    if excepted:
        before = {k: mote.properties[k] for k in excepted if k in mote.properties}
        motes_repo.update_mote(conn, mote.id, excepted)
        steps.append(undo.for_update_mote(conn, mote.id, before, excepted, label="detach"))
    detached = calendar_repo.detached_properties(mote, binding, start, end)
    copy_id = motes_repo.copy_mote(conn, conn, mote.id, detached)
    for species in species_repo.species_of(conn, mote.id):
        species_repo.set_species(conn, copy_id, species.id)
    steps.append(undo.for_create_mote(conn, copy_id, motes_repo.get_mote(conn, copy_id).properties,
                                      label="detach"))
    name = mote.properties.get("name") or f"#{mote.id[:8]}"
    return f"moved one {name} out of its series", undo.combine("detach an occurrence", steps)


def choices(conn, view: str, rest: str) -> list[tuple[str, str]]:
    words = rest.split()
    partial = "" if not rest or rest[-1].isspace() else words[-1]
    before = words[-2] if partial and len(words) > 1 else (words[-1] if words and not partial else None)
    if before == KEYS_FLAG:
        motes = motes_repo.list_motes(conn)
        return [(k, "date key") for k in calendar_repo.default_bindings(motes)]
    if before == TYPE_FLAG:
        return [(shlex.quote(str(s.properties.get("name"))), "species")
                for s in species_repo.list_species(conn) if s.properties.get("name")]
    if partial.startswith("-"):
        return [(KEYS_FLAG, "which date keys"), (TYPE_FLAG, "one species")]
    return [("today", "this day"), ("7d", "a week from today")]

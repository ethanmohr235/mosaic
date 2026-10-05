# core/planner.py
"""Plan my week, and planned vs. actual time (roadmap phase 5; decided
2026-10-05).

    plan [--days N] [--hours 9-17] [--block 2h] [--from ‹date›] [--apply]
    worked ‹mote› ‹duration› [‹date›]
    budget [‹mote›]

**What is planned.** Open work: a mote with an `estimate` (hours -- `3`,
`2h`, `1h30m`, `PT90M`) and a `due`, whose `status` is not done or
submitted. Nothing is guessed: a mote with a `due` and no estimate is
reported as skipped, one past due as overdue.

**How.** Earliest deadline first. Each mote's remaining hours (its
estimate, less what is logged done, less what is already planned ahead)
are cut into blocks of at most `--block` and each goes in the earliest
free slot inside the working hours (`core/calendar.py::earliest_free_slot`,
so "busy" is exactly what Monastery draws as busy) on or before the due
day, never past a timed due. What does not fit is reported with the hours
short. `plan` alone only previews; `plan --apply` writes.

**What is written.** Ordinary motes: an instance of the species `Work
block`, `when` the slot (the same interval shape every calendar value
has), `kind: planned`, `status: planned`, and a `for` relationship to the
mote the time is for. Doing the work is marking the block `status: done`
(`set`); `worked HW4 90m` logs an unplanned stretch as `kind: actual`.
Planned vs. actual is then a sum, and `find` can ask it anything.

One undo step per write.
"""
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Sequence

from core import calendar as calendar_repo
from core import interval as interval_repo
from core import motes as motes_repo
from core import relationships as rel_repo
from core import species as species_repo
from core import undo
from core.agenda import slot_value
from core.command_bar import CommandError
from core.entities import Mote

PLAN_VERB, WORKED_VERB, BUDGET_VERB = "plan", "worked", "budget"
VERBS = (PLAN_VERB, WORKED_VERB, BUDGET_VERB)

WHEN_KEY, KIND_KEY, STATUS_KEY = "when", "kind", "status"
PLANNED, ACTUAL = "planned", "actual"
FOR = "for"
BLOCK_SPECIES = "Work block"
DONE_STATUSES = {"done", "submitted", "complete", "completed", "closed"}

DEFAULT_DAYS = 7
DEFAULT_BLOCK = timedelta(hours=2)
MIN_BLOCK = timedelta(minutes=30)
STEP = timedelta(minutes=30)

_HOURS = re.compile(r"^(?:(?P<h>\d+(?:\.\d+)?)\s*h)?\s*(?:(?P<m>\d+)\s*m(?:in)?)?$", re.IGNORECASE)
_HOURS_RANGE = re.compile(r"^(\d{1,2})(?::(\d{2}))?-(\d{1,2})(?::(\d{2}))?$")


def parse_duration(value) -> timedelta | None:
    """`3`, `2.5`, `2h`, `90m`, `1h30m`, `PT90M` as a span; a bare number
    is hours (what a person writes in an `estimate`)."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return timedelta(hours=float(value)) if value > 0 else None
    text = str(value).strip()
    if not text:
        return None
    try:
        return timedelta(hours=float(text)) if float(text) > 0 else None
    except ValueError:
        pass
    iso = interval_repo.parse_duration(text)
    if iso is not None:
        return iso if iso > timedelta(0) else None
    match = _HOURS.match(text)
    if match and (match.group("h") or match.group("m")):
        span = timedelta(hours=float(match.group("h") or 0), minutes=int(match.group("m") or 0))
        return span if span > timedelta(0) else None
    return None


def hours_text(span: timedelta) -> str:
    hours = span.total_seconds() / 3600
    return f"{hours:g}h" if hours == int(hours) or round(hours, 2) == hours else f"{hours:.1f}h"


# ---- the line -----------------------------------------------------------

@dataclass(frozen=True)
class PlanLine:
    days: int = DEFAULT_DAYS
    start: time = calendar_repo.DEFAULT_WORK_START
    end: time = calendar_repo.DEFAULT_WORK_END
    block: timedelta = DEFAULT_BLOCK
    first: str | None = None
    apply: bool = False


def usage() -> str:
    from core.reserved import RESERVED_VERBS
    return f"usage: {RESERVED_VERBS[PLAN_VERB]['usage']}"


def parse_plan(args: Sequence[str]) -> PlanLine:
    fields: dict = {}
    items = list(args)
    i = 0
    while i < len(items):
        flag = items[i]
        if flag == "--apply":
            fields["apply"] = True
        elif flag in ("--days", "--hours", "--block", "--from"):
            i += 1
            if i >= len(items):
                raise CommandError(f"{flag} needs a value -- {usage()}")
            value = items[i]
            if flag == "--days":
                if not value.isdigit() or not 1 <= int(value) <= 60:
                    raise CommandError("--days is a whole number from 1 to 60")
                fields["days"] = int(value)
            elif flag == "--hours":
                match = _HOURS_RANGE.match(value)
                if not match:
                    raise CommandError("--hours is a range like 9-17 or 8:30-16")
                start = time(int(match.group(1)), int(match.group(2) or 0))
                end = time(int(match.group(3)), int(match.group(4) or 0))
                if start >= end:
                    raise CommandError("--hours must end after it starts")
                fields["start"], fields["end"] = start, end
            elif flag == "--block":
                block = parse_duration(value)
                if block is None or block < MIN_BLOCK:
                    raise CommandError("--block is a length of at least 30m, like 2h or 90m")
                fields["block"] = block
            else:
                fields["first"] = value
        else:
            raise CommandError(f"unknown argument {flag!r} -- {usage()}")
        i += 1
    return PlanLine(**fields)


# ---- open work ----------------------------------------------------------

@dataclass
class Work:
    mote: Mote
    name: str
    due: datetime
    due_all_day: bool
    estimate: timedelta
    done: timedelta = timedelta(0)
    planned: timedelta = timedelta(0)

    @property
    def remaining(self) -> timedelta:
        return max(self.estimate - self.done - self.planned, timedelta(0))


@dataclass(frozen=True)
class Block:
    work: Work
    start: datetime
    end: datetime


@dataclass(frozen=True)
class Plan:
    blocks: tuple[Block, ...] = ()
    short: tuple[tuple[str, timedelta, str], ...] = ()    # name, hours short, due
    skipped: tuple[tuple[str, str], ...] = ()            # name, why

    @property
    def hours(self) -> timedelta:
        return sum((b.end - b.start for b in self.blocks), timedelta(0))

    def summary(self, applied: bool = False) -> str:
        verb = "placed" if applied else "would place"
        days = len({b.start.date() for b in self.blocks})
        motes = len({b.work.mote.id for b in self.blocks})
        parts = [f"{verb} {len(self.blocks)} block{'' if len(self.blocks) == 1 else 's'} "
                 f"({hours_text(self.hours)}) over {days} day{'' if days == 1 else 's'} "
                 f"for {motes} mote{'' if motes == 1 else 's'}"
                 if self.blocks else "nothing to place"]
        for b in self.blocks[:6]:
            parts.append(f"{b.start:%a %d %H:%M}-{b.end:%H:%M} {b.work.name}")
        if len(self.blocks) > 6:
            parts.append(f"... {len(self.blocks) - 6} more")
        for name, short, due in self.short:
            parts.append(f"short {hours_text(short)}: {name} (due {due})")
        if self.skipped:
            shown = "; ".join(f"{n} ({why})" for n, why in self.skipped[:3])
            more = f" +{len(self.skipped) - 3} more" if len(self.skipped) > 3 else ""
            parts.append(f"skipped: {shown}{more}")
        if self.blocks and not applied:
            parts.append("`plan --apply` writes them")
        return " | ".join(parts)


def _links(conn) -> dict[str, list[str]]:
    """block id -> the mote it is for."""
    found: dict[str, list[str]] = {}
    for rel in rel_repo.list_relationships(conn, type_=FOR):
        found.setdefault(rel.to_id, []).append(rel.from_id)
    return found


def _name(mote: Mote) -> str:
    name = mote.properties.get("name")
    return name if isinstance(name, str) and name else mote.id[:8]


def is_block(mote: Mote) -> bool:
    return mote.properties.get(KIND_KEY) in (PLANNED, ACTUAL)


def _status(mote: Mote) -> str:
    value = mote.properties.get(STATUS_KEY)
    return value.strip().casefold() if isinstance(value, str) else ""


def _span(mote: Mote) -> tuple[datetime, datetime] | None:
    found = interval_repo.parse(mote.properties.get(WHEN_KEY))
    return (found.start, found.end) if found else None


def open_work(conn: sqlite3.Connection, now: datetime) -> tuple[list[Work], list[tuple[str, str]]]:
    """The motes to plan for, earliest due first, and those skipped (and why)."""
    motes = motes_repo.list_motes(conn)
    unplaced = species_repo.template_mote_ids(conn)
    species_ids = {m.id for m in species_repo.list_species(conn)}
    links = _links(conn)
    by_id = {m.id: m for m in motes}
    work: list[Work] = []
    skipped: list[tuple[str, str]] = []
    for mote in motes:
        if mote.id in unplaced or mote.id in species_ids or is_block(mote):
            continue
        due = interval_repo.parse(mote.properties.get("due"))
        if due is None or _status(mote) in DONE_STATUSES:
            continue
        estimate = parse_duration(mote.properties.get("estimate"))
        if estimate is None:
            skipped.append((_name(mote), "no estimate"))
            continue
        deadline = due.start + (timedelta(days=1) if due.all_day else timedelta(0))
        if deadline <= now:
            skipped.append((_name(mote), "overdue"))
            continue
        item = Work(mote, _name(mote), due.start, due.all_day, estimate)
        for block_id in links.get(mote.id, []):
            block = by_id.get(block_id)
            span = _span(block) if block else None
            if block is None or span is None:
                continue
            length = span[1] - span[0]
            if block.properties.get(KIND_KEY) == ACTUAL or _status(block) in DONE_STATUSES:
                item.done += length
            elif span[1] > now:
                item.planned += length
        work.append(item)
    work.sort(key=lambda w: (w.due + (timedelta(days=1) if w.due_all_day else timedelta(0)), w.name.casefold()))
    skipped.sort()
    return work, skipped


# ---- planning -----------------------------------------------------------

def plan(conn: sqlite3.Connection, line: PlanLine, now: datetime | None = None) -> Plan:
    """The blocks `line` would place. Writes nothing."""
    now = now or datetime.now().replace(second=0, microsecond=0)
    first = _first_day(line, now)
    work, skipped = open_work(conn, now)
    motes = [m for m in motes_repo.list_motes(conn) if m.id not in species_repo.template_mote_ids(conn)]
    bindings = sorted(set(calendar_repo.default_bindings(motes)) | {WHEN_KEY})
    placed: list[Block] = []
    short: list[tuple[str, timedelta, str]] = []
    virtual: list[Mote] = []   # blocks placed so far, busy for the next ones

    for item in work:
        left = item.remaining
        deadline = item.due + (timedelta(days=1) if item.due_all_day else timedelta(0))
        for offset in range(line.days):
            if left <= timedelta(0):
                break
            day = first + timedelta(days=offset)
            if datetime.combine(day, time.min) >= deadline:
                break
            while left > timedelta(0):
                slot = _fit(motes + virtual, day, min(left, line.block), line, bindings, now, deadline)
                if slot is None:
                    break
                start, length = slot
                placed.append(Block(item, start, start + length))
                virtual.append(Mote({WHEN_KEY: slot_value(start.isoformat(), (start + length).isoformat(), False)}))
                left -= length
        if left > timedelta(0):
            short.append((item.name, left, item.due.date().isoformat()))
    placed.sort(key=lambda b: (b.start, b.work.name.casefold()))
    return Plan(tuple(placed), tuple(short), tuple(skipped))


def _first_day(line: PlanLine, now: datetime) -> date:
    if line.first:
        found = interval_repo.parse_datetime(line.first)
        if found is None:
            raise CommandError(f"--from takes a date like 2026-10-05, not {line.first!r}")
        return found[0].date()
    return now.date()


def _fit(motes, day: date, want: timedelta, line: PlanLine, bindings, now: datetime,
         deadline: datetime) -> tuple[datetime, timedelta] | None:
    """The earliest free slot on `day` for `want`, shrinking by half-hours
    to the shortest block that still fits, never starting in the past nor
    ending after `deadline`."""
    held = motes
    if day == now.date() and now.time() > line.start:
        # What is left of today starts at the next half hour.
        rounded = now + (STEP - timedelta(minutes=now.minute % 30, seconds=now.second)) \
            if now.minute % 30 or now.second else now
        held = motes + [Mote({WHEN_KEY: slot_value(datetime.combine(day, line.start).isoformat(),
                                                   rounded.isoformat(), False)})]
    length = want
    while length >= MIN_BLOCK:
        start = calendar_repo.earliest_free_slot(
            held, day=day, duration=length, bindings=bindings,
            work_start=line.start, work_end=line.end)
        if start is not None and start + length <= deadline:
            return start, length
        length -= STEP
    return None


# ---- writing ------------------------------------------------------------

def _species(conn) -> tuple[str, list[undo.Command]]:
    """The `Work block` species, made (as one undo step) if absent."""
    for mote in species_repo.list_species(conn):
        if mote.properties.get("name") == BLOCK_SPECIES:
            return mote.id, []
    for mote in motes_repo.list_motes(conn):
        if mote.properties.get("name") == BLOCK_SPECIES:
            return mote.id, []
    species_id = motes_repo.create_mote(conn, {"name": BLOCK_SPECIES})
    return species_id, [undo.for_create_mote(conn, species_id, {"name": BLOCK_SPECIES}, "work block species")]


def _write_block(conn, species_id: str, work_mote: Mote, start: datetime, end: datetime,
                 kind: str, status: str, label: str) -> list[undo.Command]:
    properties = {"name": f"Work: {_name(work_mote)}", WHEN_KEY: slot_value(
        start.isoformat(), end.isoformat(), False), KIND_KEY: kind, STATUS_KEY: status}
    block_id = motes_repo.create_mote(conn, properties)
    commands = [undo.for_create_mote(conn, block_id, properties, label)]
    rel = species_repo.set_species(conn, block_id, species_id)
    commands.append(undo.for_create_relationship(conn, rel, block_id, species_id, species_repo.INSTANCE_OF, label))
    link = rel_repo.create_relationship(conn, block_id, work_mote.id, FOR)
    commands.append(undo.for_create_relationship(conn, link, block_id, work_mote.id, FOR, label))
    return commands


def apply(conn: sqlite3.Connection, found: Plan) -> tuple[str, "undo.Command | None"]:
    if not found.blocks:
        return found.summary(applied=True), None
    species_id, commands = _species(conn)
    for block in found.blocks:
        commands += _write_block(conn, species_id, block.work.mote, block.start, block.end,
                                 PLANNED, PLANNED, "plan")
    return found.summary(applied=True), undo.combine("plan my week", commands)


# ---- worked & time ------------------------------------------------------

def find_mote(conn: sqlite3.Connection, ref: str) -> Mote:
    motes = motes_repo.list_motes(conn)
    if ref.startswith("#"):
        found = [m for m in motes if m.id == ref[1:] or m.id.startswith(ref[1:])]
    else:
        found = [m for m in motes if _name(m) == ref] or \
                [m for m in motes if _name(m).casefold() == ref.casefold()]
    if not found:
        raise CommandError(f"no mote named {ref!r}")
    if len(found) > 1:
        raise CommandError(f"{len(found)} motes match {ref!r} -- use #id")
    return found[0]


def worked(conn: sqlite3.Connection, args: Sequence[str],
           now: datetime | None = None) -> tuple[str, "undo.Command"]:
    """`worked ‹mote› ‹duration› [‹date›]`: time actually spent, logged as
    a `kind: actual` block -- ending now, or starting at the work day's
    opening on `date`. One undo step."""
    now = now or datetime.now().replace(second=0, microsecond=0)
    if len(args) not in (2, 3):
        raise CommandError("usage: worked ‹mote› ‹duration› [‹date›]")
    mote = find_mote(conn, args[0])
    length = parse_duration(args[1])
    if length is None:
        raise CommandError(f"{args[1]!r} is not a length of time -- try 90m or 1.5h")
    if len(args) == 3:
        found = interval_repo.parse_datetime(args[2])
        if found is None:
            raise CommandError(f"{args[2]!r} is not a date")
        start = datetime.combine(found[0].date(), calendar_repo.DEFAULT_WORK_START)
    else:
        start = now - length
    species_id, commands = _species(conn)
    commands += _write_block(conn, species_id, mote, start, start + length, ACTUAL, "done", "worked")
    return (f"logged {hours_text(length)} on {_name(mote)} ({start:%a %d %H:%M})",
            undo.combine("worked", commands))


@dataclass(frozen=True)
class TimeRow:
    name: str
    mote_id: str
    estimate: timedelta | None
    done: timedelta
    planned: timedelta

    @property
    def left(self) -> timedelta | None:
        return None if self.estimate is None else max(self.estimate - self.done - self.planned, timedelta(0))

    def text(self) -> str:
        parts = [f"{self.name}:"]
        parts.append(f"est {hours_text(self.estimate)}," if self.estimate else "no estimate,")
        parts.append(f"done {hours_text(self.done)}, planned {hours_text(self.planned)}")
        if self.left is not None:
            parts.append(f"left {hours_text(self.left)}")
        return " ".join(parts)


def report(conn: sqlite3.Connection, ref: str | None = None,
           now: datetime | None = None) -> list[TimeRow]:
    """Planned vs. actual, per mote that has an estimate or any block.
    `done` is what was logged (`worked`) plus planned blocks marked done;
    `planned` is the rest still ahead."""
    now = now or datetime.now().replace(second=0, microsecond=0)
    motes = motes_repo.list_motes(conn)
    by_id = {m.id: m for m in motes}
    links = _links(conn)
    wanted = find_mote(conn, ref).id if ref else None
    rows = []
    for mote in motes:
        if is_block(mote) or (wanted and mote.id != wanted):
            continue
        estimate = parse_duration(mote.properties.get("estimate"))
        done = planned = timedelta(0)
        has_blocks = False
        for block_id in links.get(mote.id, []):
            block, span = by_id.get(block_id), None
            span = _span(block) if block else None
            if block is None or span is None:
                continue
            has_blocks = True
            length = span[1] - span[0]
            if block.properties.get(KIND_KEY) == ACTUAL or _status(block) in DONE_STATUSES:
                done += length
            elif span[1] > now:
                planned += length
        if estimate is None and not has_blocks:
            continue
        rows.append(TimeRow(_name(mote), mote.id, estimate, done, planned))
    return sorted(rows, key=lambda r: r.name.casefold())


def run_verb(conn: sqlite3.Connection, verb: str, args: Sequence[str],
             now: datetime | None = None) -> tuple[str, int, "undo.Command | None"]:
    """The console's `plan`, `worked` and `budget`: the log line, how many
    motes were written, and the undo step."""
    if verb == PLAN_VERB:
        line = parse_plan(args)
        found = plan(conn, line, now)
        if not line.apply:
            return found.summary(), 0, None
        message, command = apply(conn, found)
        return message, len(found.blocks) * 2, command
    if verb == WORKED_VERB:
        message, command = worked(conn, args, now)
        return message, 1, command
    if len(args) > 1:
        raise CommandError("usage: budget [‹mote›]")
    rows = report(conn, args[0] if args else None, now)
    return ("; ".join(r.text() for r in rows) or "nothing estimated or logged yet"), 0, None

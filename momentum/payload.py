# momentum/payload.py
"""A garden's tasks, shaped for the JS bridge.

Momentum paints from what the *backend* decided -- which key means
status (core/task_candidates.py), where a task sits in time
(core/calendar.py) -- rather than re-deciding either in JavaScript. That
is the same line monastery/payload.py draws, and for the same reason:
one reader per question, so two windows can never come to disagree
about what a garden says.

Datetimes cross the bridge as ISO strings, naive and local, exactly as
monastery/payload.py sends them.
"""
from dataclasses import dataclass
from datetime import datetime

from core import kinds as kinds_repo
from core import species as species_repo
from core import task_candidates as candidates_repo
from core.calendar import Occurrence, is_calendar_value, parse_date_value
from core.interval import parse as parse_interval
from core.entities import Mote, Relationship

NAME_KEY = "name"

# Momentum's own structural keys, recognized only by this window. Like
# core/calendar.py's `ends`/`end`, these are ordinary property names --
# nothing in the schema or docs/architecture.md reserves them, and a
# garden that never opens Momentum never grows one.
SUBTASKS_KEY = "subtasks"

# Status values that mean the work is over. Not a vocabulary the engine
# enforces -- a garden may use any words it likes and the board will
# show them -- only the ones this window recognizes well enough to stop
# counting toward a day's remaining load. Kept in step with
# momentum/frontend/app.js::DONE_WORDS.
DONE_VALUES = ("done", "complete", "completed", "shipped", "harvested")

# Relationship types Momentum reads and writes. Ordinary types, created
# through core/relationships.py like every other edge -- which is what
# makes them visible in Monarch and durable across a rename, unlike a
# name matched out of a text property.
ASSIGNED_TO = "assigned_to"
DEPENDS_ON = "depends_on"
BLOCKED_BY = "blocked_by"
TASK_RELATIONSHIPS = (ASSIGNED_TO, DEPENDS_ON, BLOCKED_BY)


@dataclass(frozen=True)
class Directory:
    """Everything the payloads need to say about a mote beyond its id:
    its name, its species (which is what colours it, in all six
    windows) and its kinds. Built once per request from one read of the
    garden. monastery/payload.py's, deliberately duplicated rather than
    imported -- no adapter imports another adapter, and each window
    answers this about different payloads."""
    motes: dict[str, Mote]
    relationships: list[Relationship]
    kinds: dict[str, list[str]]

    @staticmethod
    def of(motes: list[Mote], relationships: list[Relationship]) -> "Directory":
        return Directory(
            {m.id: m for m in motes}, relationships, kinds_repo.kinds_in(motes, relationships)
        )

    def describe(self, mote_id: str | None) -> dict:
        mote = self.motes.get(mote_id) if mote_id else None
        if mote is None:
            return {"id": mote_id, "name": mote_id or "", "speciesId": None,
                    "speciesName": None, "kinds": []}
        species_id = self._species_id(mote.id)
        species = self.motes.get(species_id) if species_id else None
        return {
            "id": mote.id,
            "name": name_of(mote),
            "speciesId": species_id,
            "speciesName": name_of(species) if species else None,
            "kinds": self.kinds.get(mote.id, []),
        }

    def _species_id(self, mote_id: str) -> str | None:
        """The first species a mote is an instance of -- the one the
        graph canvas colours it by, so a task reads the same colour on
        the board as it does in Monarch."""
        for relationship in self.relationships:
            if relationship.from_id == mote_id and relationship.type == species_repo.INSTANCE_OF:
                return relationship.to_id
        return None

    def targets(self, mote_id: str, type_: str) -> list[str]:
        return [r.to_id for r in self.relationships
                if r.from_id == mote_id and r.type == type_]


@dataclass(frozen=True)
class Keys:
    """Which property key means what, for one request.

    Carried together because every payload below needs all of them and
    because they arrive together: the window remembers the person's
    choice and sends it back on each call, the same way Monastery sends
    its active bindings (monastery/api.py::calendar).
    """
    status: str | None
    priority: str | None
    estimate: str | None
    bindings: tuple[str, ...]

    def as_payload(self) -> dict:
        return {
            "status": self.status,
            "priority": self.priority,
            "estimate": self.estimate,
            "bindings": list(self.bindings),
        }


def name_of(mote: Mote) -> str:
    name = mote.properties.get(NAME_KEY)
    return name if isinstance(name, str) and name else mote.id[:8]


def subtasks_of(mote: Mote) -> list[dict]:
    """A mote's checklist, defensively. A property a person edited by
    hand into something that is not a list of `{text, done}` yields an
    empty checklist rather than breaking the board -- the same
    permissiveness core/calendar.py gives a typo'd recurrence."""
    raw = mote.properties.get(SUBTASKS_KEY)
    if not isinstance(raw, list):
        return []
    found = []
    for entry in raw:
        if isinstance(entry, dict) and isinstance(entry.get("text"), str):
            found.append({"text": entry["text"], "done": bool(entry.get("done"))})
        elif isinstance(entry, str):
            # A bare list of strings is a checklist someone typed the
            # obvious way. Reading it as all-unchecked is friendlier
            # than showing nothing.
            found.append({"text": entry, "done": False})
    return found


def _placement(mote: Mote, keys: Keys) -> dict | None:
    """Where this mote sits in time, under the first binding it actually
    carries. One answer, not one per binding: the board shows a card
    once, and the day it shows it on is the day the person scheduled it.
    Monastery is where a mote dated two ways legitimately draws twice."""
    for binding in keys.bindings:
        parsed = parse_date_value(mote.properties.get(binding))
        if parsed is None:
            continue
        start, all_day = parsed
        return {
            "binding": binding,
            "start": start.isoformat(),
            "day": start.strftime("%Y-%m-%d"),
            "allDay": all_day,
        }
    return None


def card_payload(mote: Mote, directory: Directory, keys: Keys) -> dict:
    """One task as every view draws it. The board, the week and the
    focus queue all show the same card, so they all read it here --
    three shapes of the same mote would be three chances to disagree."""
    subtasks = subtasks_of(mote)
    estimate_raw = mote.properties.get(keys.estimate) if keys.estimate else None
    minutes = candidates_repo.parse_estimate_value(estimate_raw)
    owners = [directory.describe(t) for t in directory.targets(mote.id, ASSIGNED_TO)]
    return {
        **directory.describe(mote.id),
        "status": _label(mote.properties.get(keys.status)) if keys.status else None,
        "priority": _label(mote.properties.get(keys.priority)) if keys.priority else None,
        "estimate": estimate_raw if isinstance(estimate_raw, str) else (
            candidates_repo.format_estimate(minutes) if minutes else None
        ),
        "estimateMinutes": minutes,
        "scheduled": _placement(mote, keys),
        "subtasks": subtasks,
        "subtasksDone": sum(1 for s in subtasks if s["done"]),
        "owners": owners,
        "blocked": bool(directory.targets(mote.id, BLOCKED_BY)),
    }


def _label(value) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def board_payload(
    cards: list[dict],
    *,
    statuses: list[str],
    keys: Keys,
    candidates: dict,
    stats: dict,
) -> dict:
    """The board: one column per status the garden actually uses, plus
    a home for everything the status key says nothing about.

    That last column is not a design flourish. A garden discovers its
    status key from motes that already carry it, so there are almost
    always motes that do not -- and a board that silently dropped them
    would be hiding work.
    """
    by_status: dict[str, list[dict]] = {s: [] for s in statuses}
    unset: list[dict] = []
    for card in cards:
        status = card["status"]
        if status is None:
            unset.append(card)
        else:
            by_status.setdefault(status, []).append(card)

    columns = [
        {
            "key": status,
            "label": status,
            "cards": by_status.get(status, []),
            "minutes": sum(c["estimateMinutes"] or 0 for c in by_status.get(status, [])),
        }
        for status in by_status
    ]
    if unset:
        columns.append({
            "key": None,
            "label": "no status",
            "cards": unset,
            "minutes": sum(c["estimateMinutes"] or 0 for c in unset),
        })
    return {"columns": columns, "keys": keys.as_payload(), "candidates": candidates, "stats": stats}


def days_payload(
    days: list[dict],
    pool: list[dict],
    *,
    keys: Keys,
    candidates: dict,
    stats: dict,
    capacity_minutes: float,
    work_start: str,
    work_end: str,
) -> dict:
    """The week: seven days of scheduled cards and the undated pool they
    are dragged out of.

    `work_start`/`work_end` are the earliest and latest time of day a
    drop onto a day is allowed to land -- core/calendar.py's own
    `earliest_free_slot` arguments, sent along so the rail can show and
    edit the same window scheduling actually uses.
    """
    return {
        "days": days,
        "pool": pool,
        "keys": keys.as_payload(),
        "candidates": candidates,
        "stats": stats,
        "capacityMinutes": capacity_minutes,
        "workStart": work_start,
        "workEnd": work_end,
    }


def day_payload(day: str, cards: list[dict], *, capacity_minutes: float) -> dict:
    """One column of the week. `minutes` counts only what is still open:
    a day whose work is done is not a full day, and a capacity bar that
    stayed red after you finished everything would be measuring the
    wrong thing."""
    open_cards = [c for c in cards if not is_done(c)]
    minutes = sum(c["estimateMinutes"] or 0 for c in open_cards)
    return {
        "day": day,
        "cards": cards,
        "minutes": minutes,
        "over": bool(capacity_minutes and minutes > capacity_minutes),
    }


def is_done_value(value) -> bool:
    """Whether a raw status property value reads as finished. The same
    test is_done() applies to a card's already-shaped "status" field,
    lifted out so momentum/api.py can ask it of a mote's property
    straight off the graph -- deciding whether marking a task done is
    a *change* (and so owes a closing session) has to happen before a
    card exists to ask is_done() of."""
    return isinstance(value, str) and value.strip().casefold() in DONE_VALUES


def is_done(card: dict) -> bool:
    """Whether a card counts as finished, for the two questions that
    need it: a day's remaining load and the "open" figure.

    Read from the *value*, not from a reserved status: whichever key
    this garden uses for status, a value spelling one of these means the
    work is over. A garden whose done column is called something else
    entirely still boards correctly -- it only misses out on being
    discounted from the load, which is the safe direction to be wrong in.
    """
    return is_done_value(card.get("status"))


def session_payload(mote: Mote, directory: Directory, subject_id: str | None) -> dict:
    """One logged work session. `subject` is the task it was about --
    the mote on the other end of its `about` relationship -- so the log
    can name what the time went into without a second lookup.

    How long it ran comes from the interval's own span, the same reading
    Monastery gives it: a session is an ordinary dated mote and nothing
    about it is special-cased."""
    placed = parse_interval(mote.properties.get("date"))
    return {
        "id": mote.id,
        "start": placed.start.isoformat() if placed else None,
        "day": placed.start.strftime("%Y-%m-%d") if placed else None,
        "minutes": placed.span.total_seconds() / 60 if placed else 0.0,
        "subject": directory.describe(subject_id) if subject_id else None,
    }


def focus_payload(
    queue: list[dict],
    sessions: list[dict],
    *,
    keys: Keys,
    candidates: dict,
    stats: dict,
    streak: int,
    heatmap: list[dict],
) -> dict:
    return {
        "queue": queue,
        "sessions": sessions,
        "keys": keys.as_payload(),
        "candidates": candidates,
        "stats": stats,
        "streak": streak,
        "heatmap": heatmap,
    }


def candidates_payload(
    enum_found: list[candidates_repo.Candidate],
    estimate_found: list[candidates_repo.Candidate],
    binding_counts: dict[str, int],
    keys: Keys,
) -> dict:
    """What the sidebar offers: every key that *could* mean each thing,
    with the evidence for it, and which one is currently chosen.

    Deliberately the same shape as monastery/payload.py's `bindings`
    list -- key, count, active -- because it is the same idea, and a
    person who has used one sidebar should recognize the other.
    """
    return {
        "enum": [
            {"key": c.key, "count": c.count, "values": list(c.values)} for c in enum_found
        ],
        "estimate": [{"key": c.key, "count": c.count} for c in estimate_found],
        "bindings": [
            {"key": key, "count": count, "active": key in keys.bindings}
            for key, count in sorted(binding_counts.items(), key=lambda kv: (-kv[1], kv[0]))
        ],
    }


def panel_payload(mote: Mote, directory: Directory, keys: Keys) -> dict:
    """One mote as the detail panel shows it: its card, its raw
    properties, and every relationship touching it with the direction it
    points -- real edges, read from the graph.

    `calendarKeys` marks which properties are the calendar's own
    machinery rather than user data, exactly as monastery/payload.py
    does; `taskKeys` does the same for the keys this window is currently
    reading as status/priority/estimate, so the panel can show them as
    the controls above rather than repeating them as plain rows.
    """
    relationships = [
        r for r in directory.relationships if mote.id in (r.from_id, r.to_id)
    ]
    task_keys = [k for k in (keys.status, keys.priority, keys.estimate) if k]
    return {
        **card_payload(mote, directory, keys),
        "properties": mote.properties,
        "calendarKeys": [k for k, v in mote.properties.items() if is_calendar_value(v)],
        "taskKeys": [k for k in mote.properties if k in task_keys or k == SUBTASKS_KEY],
        "relationships": [_relationship(r, mote.id, directory) for r in relationships],
    }


def _relationship(relationship: Relationship, mote_id: str, directory: Directory) -> dict:
    outgoing = relationship.from_id == mote_id
    other = relationship.to_id if outgoing else relationship.from_id
    return {
        "id": relationship.id,
        "type": relationship.type,
        "outgoing": outgoing,
        "other": directory.describe(other),
    }


def occurrence_day(occurrence: Occurrence) -> str:
    return occurrence.start.strftime("%Y-%m-%d")


def iso_day(when: datetime) -> str:
    return when.strftime("%Y-%m-%d")

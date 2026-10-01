# monastery/payload.py
"""A placed garden, shaped for the JS bridge.

Monastery paints from occurrences the *backend* produced, rather than
scanning properties for dates a second time in JavaScript. That is what
keeps the calendar and any future `monoss calendar` command from coming
to disagree about which motes are dated: there is one reader
(core/calendar.py), and this, which only reshapes its answers into
JSON-able dicts.

Datetimes cross the bridge as ISO strings. JavaScript's Date parses them
directly and nothing here needs a timezone: every property MonOSS stores
is naive local time, so sending an offset would be inventing
information the garden does not have.
"""
from dataclasses import dataclass

from core import kinds as kinds_repo
from core import species as species_repo
from core.calendar import Occurrence, is_calendar_value
from core.entities import Mote, Relationship

NAME_KEY = "name"


@dataclass(frozen=True)
class Directory:
    """Everything the payloads need to say about a mote beyond its id:
    its name, its species (which is what colours it, in all three
    windows) and its kinds. Built once per request from one read of the
    garden. the old mora/payload.py's, deliberately duplicated rather than
    imported -- no adapter imports another adapter, and the two windows
    answer this question about different payloads."""
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
        """The first species a mote is an instance of -- the one the main
        app's canvas colours it by (monarch/frontend/graph.js::_speciesModeColor),
        so a mote reads the same colour on the calendar as in the graph."""
        for relationship in self.relationships:
            if relationship.from_id == mote_id and relationship.type == species_repo.INSTANCE_OF:
                return relationship.to_id
        return None


def name_of(mote: Mote) -> str:
    name = mote.properties.get(NAME_KEY)
    return name if isinstance(name, str) and name else mote.id[:8]


def occurrence_payload(occurrence: Occurrence, directory: Directory) -> dict:
    """One placement, with everything the grid needs to draw it without
    asking a second question. `key` is per-occurrence rather than per-mote
    because a recurring mote appears many times in one week and the DOM
    needs to tell those apart; `id` stays the mote's, because clicking
    any of them opens the same mote. `start` and `end` are also what a
    drag sends back to say which placement it grabbed -- an occurrence
    has no id of its own, and its start is what identifies it inside a
    series."""
    return {
        **directory.describe(occurrence.mote.id),
        "key": f"{occurrence.mote.id}:{occurrence.binding}:{occurrence.start.isoformat()}",
        "binding": occurrence.binding,
        "start": occurrence.start.isoformat(),
        "end": occurrence.end.isoformat(),
        "allDay": occurrence.all_day,
        # Whether dropping this rectangle somewhere else has to ask
        # "this occurrence, or the whole series?" first. core answered it
        # while expanding the rule (core/calendar.py::is_recurring), so
        # no JavaScript ever parses an RRULE.
        "recurring": occurrence.recurring,
    }


def calendar_payload(
    occurrences: list[Occurrence],
    directory: Directory,
    *,
    binding_counts: dict[str, int],
    active_bindings: list[str],
    undated: int,
) -> dict:
    """One view's worth of answer: what to draw, what could be drawn, and
    the one count the frontend cannot work out for itself.

    `undated` is here because it is a fact about the whole garden, which
    the frontend never sees; how many motes are *placed* is deliberately
    not, since the species filter is client-side and the only honest
    answer is the one computed against what is actually on screen."""
    return {
        "occurrences": [occurrence_payload(o, directory) for o in occurrences],
        "bindings": [
            {"key": key, "count": count, "active": key in active_bindings}
            for key, count in sorted(binding_counts.items(), key=lambda kv: (-kv[1], kv[0]))
        ],
        "species": _species_payload(directory),
        "stats": {"undated": undated},
    }


def _species_payload(directory: Directory) -> list[dict]:
    """Every species in the garden, for the sidebar's filter list and the
    draft popover's type picker. Asked of core/kinds.py rather than of a
    hardcoded list of types: a garden's types are whatever motes are
    pointed at by instance_of, and that is the only place that is
    answered."""
    entries = [
        directory.describe(mote_id)
        for mote_id, kinds in directory.kinds.items()
        if "species" in kinds
    ]
    return sorted(entries, key=lambda e: e["name"].casefold())


def panel_payload(mote: Mote, directory: Directory) -> dict:
    """One mote as the detail panel shows it: its properties, and every
    relationship touching it with the direction it points.

    `calendarKeys` marks which properties the calendar reads, so the
    panel can show them apart without deciding for itself which those
    are -- that answer is core/calendar.py's.

    Asked about each **value**, not each key: there is no list of
    calendar property names any more. A key holding an interval is the
    calendar's under whatever name the garden gave it."""
    relationships = [
        r for r in directory.relationships if mote.id in (r.from_id, r.to_id)
    ]
    return {
        **directory.describe(mote.id),
        "properties": mote.properties,
        "calendarKeys": [k for k, v in mote.properties.items() if is_calendar_value(v)],
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

# core/scenarios.py
"""Scenarios: a named perturbation of the garden, so one graph can hold
more than one answer.

A garden holds exactly one state, but every question worth asking about a
model is comparative -- design against off-design, best against worst
case, what breaks if the feedwater drops 20 K. Without scenarios each of
those means editing the garden, running, writing the answer down, and
editing it back.

The shape mirrors templates, one arrow over:

    template_of : species  ::  overrides : mote

A **template** holds placeholder properties for a species and is excluded
from that species' instances. An **override** holds replacement
properties for one specific mote and is excluded from every run unless a
scenario selects it. A **scenario** gathers overrides:

    Winter Case --varies--> Winter Turbine --overrides--> HP Turbine

Two reserved relationship types, no new properties, no new tables. An
override's replacement values are ordinary properties, so the existing
property editor, the units mechanism and the whole MCP surface work on
them unchanged.

An override replaces every property it carries, `name` included -- there
is no special case, exactly as a template supplies every property it
carries to a new instance. Name an override for what its target becomes
under that scenario, or leave `name` off it entirely.

`apply()` is pure, like core/simulation.py's `_build_view` that it feeds:
it takes a properties dict and returns a new one. That purity is why
scenarios cost so little -- the run machinery was already driven from a
properties dict rather than from the database.
"""
import sqlite3
from dataclasses import dataclass

from core.entities import Mote
from core.motes import get_mote, get_motes
from core.relationships import list_relationships

# An override mote -> the mote whose properties it replaces.
OVERRIDES = "overrides"
# A scenario mote -> an override it includes.
VARIES = "varies"


@dataclass(frozen=True)
class Override:
    """One mote's replacement properties under some scenario."""
    mote_id: str
    target_id: str
    properties: dict


@dataclass(frozen=True)
class Scenario:
    """A named set of overrides, applied together or not at all."""
    mote_id: str
    name: str
    overrides: tuple[Override, ...]

    @property
    def target_ids(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(o.target_id for o in self.overrides))


def override_mote_ids(relationships: list) -> set[str]:
    """Every mote acting as an override -- the source of at least one
    `overrides` relationship.

    These hold replacement values, not real data, so a run must leave
    them out of the graph it solves: the same exclusion
    core/species.py's template_mote_ids() exists for. Otherwise an
    override declaring `freedoms` would become solvable in its own right,
    and its values would be read as if they were some mote's real state.
    """
    return {rel.from_id for rel in relationships if rel.type == OVERRIDES}


def list_scenarios(conn: sqlite3.Connection) -> list[Scenario]:
    """Every mote acting as a scenario -- the source of at least one
    `varies` relationship."""
    relationships = list_relationships(conn)
    scenario_ids = {rel.from_id for rel in relationships if rel.type == VARIES}
    scenarios = [_build(conn, mote, relationships) for mote in get_motes(conn, list(scenario_ids))]
    return sorted(scenarios, key=lambda s: s.name)


def get_scenario(conn: sqlite3.Connection, scenario_id: str) -> Scenario | None:
    """One scenario by mote id, or None if that mote varies nothing.

    A mote with no `varies` relationship is not an empty scenario, it is
    not a scenario -- running against one is almost certainly a mistyped
    id, and quietly running the unperturbed garden instead would hide it.
    """
    mote = get_mote(conn, scenario_id)
    if mote is None:
        return None
    relationships = list_relationships(conn)
    if not any(r.type == VARIES and r.from_id == scenario_id for r in relationships):
        return None
    return _build(conn, mote, relationships)


def _build(conn: sqlite3.Connection, mote: Mote, relationships: list) -> Scenario:
    override_ids = [r.to_id for r in relationships if r.type == VARIES and r.from_id == mote.id]
    targets = {
        r.from_id: r.to_id for r in relationships if r.type == OVERRIDES
    }

    overrides = []
    for override in get_motes(conn, override_ids):
        target_id = targets.get(override.id)
        if target_id is None:
            # An override mote pointing at nothing yet. A half-built
            # scenario shouldn't break the whole run, the same reading
            # core/rules.py gives a rule with no equation.
            continue
        overrides.append(Override(override.id, target_id, dict(override.properties)))

    name = mote.properties.get("name")
    label = str(name) if isinstance(name, str) and name.strip() else mote.id[:8]
    return Scenario(mote.id, label, tuple(sorted(overrides, key=lambda o: o.target_id)))


def apply(properties: dict[str, dict], scenario: Scenario | None) -> dict[str, dict]:
    """The garden's properties as this scenario would have them.

    Pure: a new dict, leaving the caller's untouched. Overrides are
    merged per property rather than replacing a mote wholesale, so an
    override naming one pressure changes that pressure and nothing else.
    """
    if scenario is None:
        return properties

    perturbed = dict(properties)
    for override in scenario.overrides:
        merged = dict(perturbed.get(override.target_id, {}))
        merged.update(override.properties)
        perturbed[override.target_id] = merged
    return perturbed

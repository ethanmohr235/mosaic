# core/inspect.py
"""What an inspector shows for one mote: its properties with an editor
kind for each, the keys its template says it usually has, its species,
and every relationship with the other end's label.

It is in `core/` because the console's props pane and its `p` float
both draw from it (docs/console.md, U3), and "editing a mote works
identically everywhere" starts with both seeing the same mote. Reads only; every write is a command
(core/commands.py).
"""
import re
import sqlite3

from core import motes as motes_repo
from core import simulation
from core import relationships as rel_repo
from core import species as species_repo
from core import units as units_repo
from core.reserved import RESERVED_PROPERTIES, RESERVED_RELATIONSHIP_TYPES

# Enum inference: a key is offered as a dropdown when the instances of a
# mote's species agree that it holds one of a small, repeated set of
# strings. Two instances sharing a value is coincidence; the thresholds
# below are what separates "status is one of four words" from "name is a
# different string every time".
_ENUM_MAX_OPTIONS = 12
_ENUM_MIN_REPEATS = 2

# Date detection is by value shape rather than by key name: a garden names
# its own keys, and an ISO date in the value is not a guess.
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def species_ids(conn: sqlite3.Connection) -> list[str]:
    """Every species in the garden, sorted -- the universe a colour palette
    claims its entries from; every pane's payload carries it, so a
    species is one colour everywhere (docs/architecture.md -> Species
    colour)."""
    return sorted(mote.id for mote in species_repo.list_species(conn))


def inspect(conn: sqlite3.Connection, mote_id: str) -> dict | None:
    """Everything an inspector draws for one mote, in one call, or None
    for a mote that isn't there.

    One call rather than the six it would otherwise take (mote, species,
    template, siblings, relationships, their labels), because it runs on
    every selection change in a view you can arrow-key through. The
    console's props pane and float (console/mote.py) draw from this, so
    what a mote looks like in an editor is decided once."""
    mote = motes_repo.get_mote(conn, mote_id)
    if mote is None:
        return None

    species = species_repo.species_of(conn, mote_id)
    species_id = species[0].id if species else None
    template = species_repo.template_for(conn, species_id) if species_id else None

    unsolved = unsolved_keys(mote.properties, simulation.garden_freedoms(conn).get(mote_id, set()))
    return {
        "id": mote.id,
        "label": label(mote),
        "properties": dict(mote.properties),
        "keys": visible_keys(mote.properties) + [k for k in unsolved if k not in mote.properties],
        "unsolved": unsolved,
        "reserved": sorted(k for k in mote.properties if k in RESERVED_PROPERTIES),
        "kinds": kinds_of(conn, mote, species_id),
        "hints": template_hints(template, mote.properties),
        "species": {
            "id": species_id,
            "label": label(species[0]) if species else None,
            "options": [
                {"id": s.id, "label": label(s)}
                for s in sorted(species_repo.list_species(conn), key=label)
            ],
            "isTemplate": template is not None and template.id == mote.id,
            "isSpecies": any(s.id == mote.id for s in species_repo.list_species(conn)),
            "hasTemplate": template is not None,
        },
        "relationships": relationships_of(conn, mote_id),
        "relTypes": relationship_types(conn),
        "speciesUniverse": species_ids(conn),
    }


def relationships_of(conn, mote_id: str) -> list[dict]:
    """Both directions in one list, each row carrying the other end's
    label and species so the frontend can draw and colour it without
    a second call per row.

    `instance_of` and `template_of` are listed like anything else.
    They were hidden at first, on the grounds that the species
    control said the same thing -- but that made the one structural
    fact the whole graph is built on the only one you could not see
    in the list of what a mote is wired to. A species *is* a
    relationship; the panel now says so, and the species dropdown
    that used to duplicate it is gone."""
    rows = []
    species = set(species_ids(conn))
    for rel in rel_repo.list_relationships(conn):
        if rel.from_id != mote_id and rel.to_id != mote_id:
            continue
        outgoing = rel.from_id == mote_id
        other_id = rel.to_id if outgoing else rel.from_id
        other = motes_repo.get_mote(conn, other_id)
        other_species = species_repo.species_of(conn, other_id) if other else []
        rows.append({
            "id": rel.id,
            "type": rel.type,
            "direction": "out" if outgoing else "in",
            "otherId": other_id,
            "otherLabel": label(other) if other else other_id,
            "otherSpeciesId": other_species[0].id if other_species else None,
            # A species at the other end (every `instance_of` row) draws in
            # its own lifted color rather than as an untyped mote.
            "otherIsSpecies": other_id in species,
            "reserved": rel.type in RESERVED_RELATIONSHIP_TYPES,
        })
    rows.sort(key=lambda r: (r["direction"] != "out", r["type"], r["otherLabel"].lower()))
    return rows


def kinds_of(conn, mote, species_id: str | None) -> dict:
    """Per-key editor choice: which of text / number / date / enum /
    ref the inspector should draw for each property this mote holds.

    Decided here rather than in the frontend because the enum case
    needs the mote's siblings, and shipping every sibling's
    properties to the frontend to work that out would make the
    payload the size of the garden."""
    # The template counts as a sibling here. It is not an instance --
    # core.species.instances_of excludes it deliberately -- but it is
    # the species' own statement of what a value for this key looks
    # like, and leaving it out is what made a four-instance garden
    # show `status` as free text until two motes happened to agree.
    siblings = species_repo.instances_of(conn, species_id) if species_id else []
    template = species_repo.template_for(conn, species_id) if species_id else None
    if template is not None and all(s.id != template.id for s in siblings):
        siblings = siblings + [template]
    vocabulary = enum_vocabulary(siblings)
    declared_units = units_repo.parse_units_property(mote.properties.get(units_repo.UNITS_KEY))

    kinds = {}
    for key, value in mote.properties.items():
        if isinstance(value, bool):
            kinds[key] = {"kind": "boolean"}
        elif isinstance(value, (int, float)):
            kinds[key] = {"kind": "number", **unit_info(declared_units.get(key), value)}
        elif isinstance(value, str) and motes_repo.get_mote(conn, value) is not None:
            # A property whose value is another mote's id is a
            # reference -- the inspector makes it a button that walks
            # there rather than a text box you can typo an id into.
            target = motes_repo.get_mote(conn, value)
            target_species = species_repo.species_of(conn, value)
            kinds[key] = {
                "kind": "ref",
                "refLabel": label(target),
                "refSpeciesId": target_species[0].id if target_species else None,
            }
        elif isinstance(value, str) and _ISO_DATE.match(value):
            kinds[key] = {"kind": "date"}
        elif isinstance(value, dict):
            # A mote's `units` map (and anything else shaped like one)
            # is a dict, not text -- rendering it with the generic text
            # editor would print Python's/JS's default object stringify
            # ("[object Object]") instead of the key: value pairs it
            # actually holds.
            kinds[key] = {"kind": "map"}
        elif key in vocabulary:
            kinds[key] = {"kind": "enum", "options": vocabulary[key]}
        else:
            kinds[key] = {"kind": "text"}
    return kinds


def label(mote) -> str:
    """A mote's display name: its `name`, or its id when it hasn't got
    one. The same answer six windows already compute separately, and the
    reason the inspector's payload carries labels for the other end of
    every relationship rather than making the frontend guess."""
    if mote is None:
        return "—"
    name = mote.properties.get("name")
    return str(name) if name not in (None, "") else mote.id


def visible_keys(properties: dict) -> list[str]:
    """Editable keys in draw order: `name` is the panel's title field, so
    it is not repeated in the property list, and reserved keys sort last
    -- a rule's `equation` belongs in the list but not above the ordinary
    properties somebody opened the panel to edit."""
    ordinary = sorted(k for k in properties
                      if k != "name" and k not in RESERVED_PROPERTIES)
    reserved = sorted(k for k in properties if k in RESERVED_PROPERTIES)
    return ordinary + reserved


def unsolved_keys(properties: dict, freedoms: set[str]) -> list[str]:
    """The freedoms that hold no number: values the solver may determine
    that nothing has written yet. Shown as *not solved* rather than left
    out or blank -- `sim` and `plot` write nothing, so a blank here reads
    as a missing value when the answer is "run solve". Only `solve` (or
    `solve --live on`) writes them; a document reads them live anyway."""
    return sorted(
        key for key in freedoms
        if isinstance(properties.get(key), bool)
        or not isinstance(properties.get(key), (int, float))
    )


def template_hints(template, properties: dict) -> list[dict]:
    """The design's "Person usually has role, email, team" chips, sourced
    from the species' template instead of a hardcoded table: the template
    is already the mote whose properties new instances start from, so the
    keys it has and this mote lacks are exactly the ones this mote is
    missing. Carries the template's value alongside each key so picking a
    hint reproduces what create_instance() would have seeded at creation,
    rather than leaving the property empty for the user to fill in by
    hand. A species without a template offers no hints, which is honest --
    nothing in the garden has said what this kind of mote usually carries."""
    if template is None:
        return []
    keys = sorted(k for k in template.properties
                  if k != "name" and k not in properties and k not in RESERVED_PROPERTIES)
    return [{"key": k, "value": template.properties[k]} for k in keys]


def enum_vocabulary(siblings: list) -> dict[str, list[str]]:
    """key -> the closed set of strings its instances actually use.

    A key qualifies when its values are strings, there are few enough
    distinct ones to be a set rather than free text, and at least one
    value is used by more than one sibling. The last condition is what
    keeps `name` and `email` out: every instance has one, and no two
    agree."""
    values: dict[str, list[str]] = {}
    for sibling in siblings:
        for key, value in sibling.properties.items():
            if isinstance(value, str) and not isinstance(value, bool):
                values.setdefault(key, []).append(value)

    vocabulary = {}
    for key, seen in values.items():
        if key == "name" or key in RESERVED_PROPERTIES:
            continue
        distinct = sorted(set(seen))
        if not 0 < len(distinct) <= _ENUM_MAX_OPTIONS:
            continue
        if len(seen) - len(distinct) < _ENUM_MIN_REPEATS - 1:
            continue
        vocabulary[key] = distinct
    return vocabulary


def unit_info(unit_name: str | None, value) -> dict:
    """Extra fields the inspector shows next to a numeric value carrying a
    declared unit: the unit itself, and its base-SI equivalent for a
    tooltip (`8 ft` -> `2.4384 m`) so a conversion that's already
    happening invisibly during simulation is visible somewhere too.

    A garden that declares no units, or a bad unit, gets nothing extra
    back -- this is display only and must never turn a typo in a `units`
    map into an inspector error."""
    if not unit_name:
        return {}
    display = units_repo.format_unit(unit_name)
    try:
        base_value = units_repo.to_base(value, unit_name)
        base_symbol = units_repo.describe(units_repo.dimension_of(unit_name))
    except units_repo.UnitError:
        return {"unit": display}
    return {"unit": display, "unitBase": f"{base_value:g} {base_symbol}"}


def relationship_types(conn: sqlite3.Connection) -> list[str]:
    """Types already in use in this garden, which is what the picker
    offers. The design hardcodes a list ("works_on", "blocks", ...); a
    garden's vocabulary is its own, and the picker accepts a typed-in
    type anyway, so the list is a shortcut rather than a constraint.
    Reserved types are excluded -- `instance_of` is the species control's
    job, and hand-drawing one here would make a mote whose species the
    inspector then refuses to show."""
    types = {rel.type for rel in rel_repo.list_relationships(conn)}
    # `instance_of` is offered even in a garden that has none yet: it is
    # how a mote is given a species, and since the species dropdown was
    # retired this picker is the way to do it.
    types.add(species_repo.INSTANCE_OF)
    return sorted(types - (RESERVED_RELATIONSHIP_TYPES - {species_repo.INSTANCE_OF}))

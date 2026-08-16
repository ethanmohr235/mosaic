# core/species.py
import sqlite3
from collections.abc import Iterable

from core.entities import Mote, Relationship
from core.motes import create_mote, get_mote, get_motes
from core.relationships import create_relationship, list_relationships

# A species is just a mote -- nothing marks it as special structurally. A
# mote "is a species" purely by being the target of one or more of these
# relationships. Flat for v1: no species-of-species chains are interpreted
# anywhere in this module.
INSTANCE_OF = "instance_of"

# A template is likewise just a mote, pointing at the species it supplies
# starting properties for. Deliberately a different relationship type from
# INSTANCE_OF: a template is not an instance, so instances_of() excludes it
# for free and the simulation engine never tries to solve one.
TEMPLATE_OF = "template_of"


def set_species(conn: sqlite3.Connection, mote_id: str, species_id: str) -> str:
    """Declare mote_id an instance of species_id. A mote may be an instance
    of more than one species -- this doesn't replace any existing ones."""
    return create_relationship(conn, mote_id, species_id, INSTANCE_OF)


def species_of(conn: sqlite3.Connection, mote_id: str) -> list[Mote]:
    """Species mote_id is declared an instance of."""
    rels = list_relationships(conn, from_id=mote_id, type_=INSTANCE_OF)
    return get_motes(conn, [r.to_id for r in rels])


def instances_of(conn: sqlite3.Connection, species_id: str) -> list[Mote]:
    """Motes declared as instances of species_id."""
    rels = [r for r in list_relationships(conn, type_=INSTANCE_OF) if r.to_id == species_id]
    return get_motes(conn, [r.from_id for r in rels])


def species_ids_in(relationships: Iterable[Relationship]) -> set[str]:
    """Which motes a set of relationships makes into species. The pure
    counterpart of list_species(), for a caller that already holds the
    relationships and shouldn't re-query: ui/api.py classifies the exact
    subgraph focus() is about to return, and does it for the library
    garden too. One definition of "is a species", whether the question
    arrives as a query or as an already-fetched list."""
    return {r.to_id for r in relationships if r.type in (INSTANCE_OF, TEMPLATE_OF)}


def list_species(conn: sqlite3.Connection) -> list[Mote]:
    """Every mote currently acting as a species -- the target of at least
    one instance_of *or* template_of relationship. Templates count so that
    a species you've set up but not yet instantiated still appears."""
    return get_motes(conn, list(species_ids_in(list_relationships(conn))))


def template_mote_ids(conn: sqlite3.Connection) -> set[str]:
    """Every mote currently acting as a template -- the source of at
    least one template_of relationship. These hold starting properties
    for a species, not real data, so a caller that only wants "real"
    motes (e.g. calendar export) should exclude them."""
    return {r.from_id for r in list_relationships(conn, type_=TEMPLATE_OF)}


def set_template(conn: sqlite3.Connection, mote_id: str, species_id: str) -> str:
    """Make mote_id the template for species_id -- the mote whose properties
    new instances start from."""
    return create_relationship(conn, mote_id, species_id, TEMPLATE_OF)


def template_for(conn: sqlite3.Connection, species_id: str) -> Mote | None:
    """The species' template, if it has one. Only the first is used: a
    species with several templates is a modelling mistake, not a feature,
    and silently merging them would hide it."""
    rels = [r for r in list_relationships(conn, type_=TEMPLATE_OF) if r.to_id == species_id]
    for rel in rels:
        mote = get_mote(conn, rel.from_id)
        if mote is not None:
            return mote
    return None


def create_instance(
    conn: sqlite3.Connection, species_id: str, overrides: dict | None = None
) -> str:
    """Create a new instance of species_id, seeded from its template's
    properties if it has one. Without a template this is just a bare mote
    with an instance_of relationship, so it always does something sensible."""
    template = template_for(conn, species_id)
    properties = dict(template.properties) if template is not None else {}
    properties.update(overrides or {})

    mote_id = create_mote(conn, properties)
    set_species(conn, mote_id, species_id)
    return mote_id

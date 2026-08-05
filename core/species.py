# core/species.py
import sqlite3

from core.models import Mote
from core.motes import get_mote
from core.relationships import create_relationship, list_relationships

# A species is just a mote -- nothing marks it as special structurally. A
# mote "is a species" purely by being the target of one or more of these
# relationships. Flat for v1: no species-of-species chains are interpreted
# anywhere in this module.
INSTANCE_OF = "instance_of"


def set_species(conn: sqlite3.Connection, mote_id: str, species_id: str) -> str:
    """Declare mote_id an instance of species_id. A mote may be an instance
    of more than one species -- this doesn't replace any existing ones."""
    return create_relationship(conn, mote_id, species_id, INSTANCE_OF)


def species_of(conn: sqlite3.Connection, mote_id: str) -> list[Mote]:
    """Species mote_id is declared an instance of."""
    rels = list_relationships(conn, from_id=mote_id, type_=INSTANCE_OF)
    return [m for m in (get_mote(conn, r.to_id) for r in rels) if m is not None]


def instances_of(conn: sqlite3.Connection, species_id: str) -> list[Mote]:
    """Motes declared as instances of species_id."""
    rels = [r for r in list_relationships(conn, type_=INSTANCE_OF) if r.to_id == species_id]
    return [m for m in (get_mote(conn, r.from_id) for r in rels) if m is not None]


def list_species(conn: sqlite3.Connection) -> list[Mote]:
    """Every mote currently acting as a species -- i.e. the target of at
    least one instance_of relationship."""
    species_ids = {r.to_id for r in list_relationships(conn, type_=INSTANCE_OF)}
    return [m for m in (get_mote(conn, sid) for sid in species_ids) if m is not None]

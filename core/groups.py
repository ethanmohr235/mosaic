# core/groups.py
"""Folders -- purely organizational annotation, unrelated to species or
simulation. A folder is just a mote; nothing marks it as special
structurally. Membership is a relationship: a mote is grouped into a
folder by having a `groups` relationship pointing at it -- the same
shape as core/species.py's instance_of/species, deliberately mirrored so
folders reuse that pattern instead of inventing a new one. Flat, like
species: a folder being grouped into another folder isn't interpreted as
nesting anywhere in this module.

A folder is never picked up by core/simulation.py unless it's separately
declared an instance_of a species -- nothing here needs to guard against
that, since the solver only ever iterates a species' declared instances.
"""
import sqlite3

from core.models import Mote
from core.motes import get_motes
from core.relationships import create_relationship, list_relationships

GROUPS = "groups"


def set_group(conn: sqlite3.Connection, mote_id: str, folder_id: str) -> str:
    """Declare mote_id grouped into folder_id. A mote may belong to more
    than one folder -- this doesn't replace any existing ones."""
    return create_relationship(conn, mote_id, folder_id, GROUPS)


def group_of(conn: sqlite3.Connection, mote_id: str) -> list[Mote]:
    """Folders mote_id is grouped into."""
    rels = list_relationships(conn, from_id=mote_id, type_=GROUPS)
    return get_motes(conn, [r.to_id for r in rels])


def members_of(conn: sqlite3.Connection, folder_id: str) -> list[Mote]:
    """Motes grouped into folder_id."""
    rels = [r for r in list_relationships(conn, type_=GROUPS) if r.to_id == folder_id]
    return get_motes(conn, [r.from_id for r in rels])


def list_folders(conn: sqlite3.Connection) -> list[Mote]:
    """Every mote currently acting as a folder -- the target of at least
    one groups relationship."""
    folder_ids = {r.to_id for r in list_relationships(conn, type_=GROUPS)}
    return get_motes(conn, list(folder_ids))

# core/folders.py
"""Folders -- purely organizational annotation, unrelated to species or
simulation. A folder is just a mote; nothing marks it as special
structurally. Membership is a relationship: a mote is grouped into a
folder by having an `in_folder` relationship pointing at it -- the same
shape as core/species.py's instance_of/species, deliberately mirrored so
folders reuse that pattern instead of inventing a new one. Flat, like
species: a folder being placed in another folder isn't interpreted as
nesting anywhere in this module.

The relationship type reads as a sentence in the direction it points --
`member --in_folder--> folder`, like `instance --instance_of-->
species`, `template --template_of--> species`, `rule --applies_to-->
species`. It was originally `groups`, which read backwards (a member
does not "group" its folder); renaming it while no garden had used one
yet cost nothing, and leaving it would have meant one reserved type
disagreeing with every other for as long as the type existed.

Naming here mirrors core/species.py throughout: set_folder/folders_of/
folder_members against set_species/species_of/instances_of, so the two
halves of the same idea can't be told apart by their vocabulary.

A folder is never picked up by core/simulation.py unless it's separately
declared an instance_of a species -- nothing here needs to guard against
that, since the solver only ever iterates a species' declared instances.
"""
import sqlite3
from collections.abc import Iterable

from core.entities import Mote, Relationship
from core.motes import get_motes
from core.relationships import create_relationship, list_relationships

IN_FOLDER = "in_folder"


def set_folder(conn: sqlite3.Connection, mote_id: str, folder_id: str) -> str:
    """Declare mote_id grouped into folder_id. A mote may belong to more
    than one folder -- this doesn't replace any existing ones."""
    return create_relationship(conn, mote_id, folder_id, IN_FOLDER)


def folders_of(conn: sqlite3.Connection, mote_id: str) -> list[Mote]:
    """Folders mote_id is grouped into."""
    rels = list_relationships(conn, from_id=mote_id, type_=IN_FOLDER)
    return get_motes(conn, [r.to_id for r in rels])


def folder_members(conn: sqlite3.Connection, folder_id: str) -> list[Mote]:
    """Motes grouped into folder_id."""
    rels = [r for r in list_relationships(conn, type_=IN_FOLDER) if r.to_id == folder_id]
    return get_motes(conn, [r.from_id for r in rels])


def folder_ids_in(relationships: Iterable[Relationship]) -> set[str]:
    """Which motes a set of relationships makes into folders -- the pure
    counterpart of list_folders(), mirroring
    core/species.py::species_ids_in() for the same reason."""
    return {r.to_id for r in relationships if r.type == IN_FOLDER}


def list_folders(conn: sqlite3.Connection) -> list[Mote]:
    """Every mote currently acting as a folder -- the target of at least
    one in_folder relationship."""
    return get_motes(conn, list(folder_ids_in(list_relationships(conn))))

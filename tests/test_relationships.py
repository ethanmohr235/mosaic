import sqlite3

import pytest

from core.motes import create_mote
from core.relationships import (
    create_relationship,
    get_relationship,
    list_relationships,
    update_relationship,
    delete_relationship,
)


def _make_two_motes(conn):
    a = create_mote(conn, {"name": "Alice"})
    b = create_mote(conn, {"name": "Bob"})
    return a, b


def test_create_relationship_returns_str_id(conn):
    a, b = _make_two_motes(conn)
    rel_id = create_relationship(conn, a, b, "knows")
    assert isinstance(rel_id, str)


def test_create_relationship_with_bad_ids_raises_integrity_error(conn):
    with pytest.raises(sqlite3.IntegrityError):
        create_relationship(conn, "missing-a", "missing-b", "knows")


def test_get_relationship_found_and_not_found(conn):
    a, b = _make_two_motes(conn)
    rel_id = create_relationship(conn, a, b, "knows")

    rel = get_relationship(conn, rel_id)
    assert rel.from_id == a
    assert rel.to_id == b
    assert rel.type == "knows"

    assert get_relationship(conn, "missing") is None


def test_list_relationships_filters_by_from_id_and_type(conn):
    a, b = _make_two_motes(conn)
    c = create_mote(conn, {"name": "Carol"})
    create_relationship(conn, a, b, "knows")
    create_relationship(conn, a, c, "manages")
    create_relationship(conn, b, c, "knows")

    assert len(list_relationships(conn)) == 3
    assert len(list_relationships(conn, from_id=a)) == 2
    assert len(list_relationships(conn, type_="knows")) == 2
    assert len(list_relationships(conn, from_id=a, type_="knows")) == 1


def test_update_relationship_updates_only_given_fields(conn):
    a, b = _make_two_motes(conn)
    c = create_mote(conn, {"name": "Carol"})
    rel_id = create_relationship(conn, a, b, "knows")

    assert update_relationship(conn, rel_id, to_id=c) is True

    rel = get_relationship(conn, rel_id)
    assert rel.from_id == a
    assert rel.to_id == c
    assert rel.type == "knows"


def test_update_relationship_requires_a_field(conn):
    a, b = _make_two_motes(conn)
    rel_id = create_relationship(conn, a, b, "knows")
    with pytest.raises(ValueError):
        update_relationship(conn, rel_id)


def test_update_relationship_unknown_id_returns_false(conn):
    assert update_relationship(conn, "missing", type_="knows") is False


def test_delete_relationship_returns_true_when_found(conn):
    a, b = _make_two_motes(conn)
    rel_id = create_relationship(conn, a, b, "knows")
    assert delete_relationship(conn, rel_id) is True
    assert get_relationship(conn, rel_id) is None


def test_delete_relationship_returns_false_when_not_found(conn):
    assert delete_relationship(conn, "missing") is False

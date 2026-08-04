import pytest

from core.motes import create_mote, get_mote, list_motes, update_mote, delete_mote


@pytest.mark.parametrize("value", [
    "hello", 21, 3.14, True, False, None, [1, 2, 3], {"a": 1},
])
def test_property_round_trips_through_json(conn, value):
    mote_id = create_mote(conn, {"field": value})
    mote = get_mote(conn, mote_id)
    assert mote.properties["field"] == value


def test_create_mote_returns_str_id(conn):
    mote_id = create_mote(conn, {"name": "Alice"})
    assert isinstance(mote_id, str)


def test_create_mote_rejects_non_serializable_value(conn):
    with pytest.raises(ValueError):
        create_mote(conn, {"bad": {1, 2, 3}})


def test_update_mote_rejects_non_serializable_value(conn):
    mote_id = create_mote(conn, {"name": "Alice"})
    with pytest.raises(ValueError):
        update_mote(conn, mote_id, {"bad": {1, 2, 3}})


def test_get_mote_returns_none_for_unknown_id(conn):
    assert get_mote(conn, "does-not-exist") is None


def test_update_mote_on_unknown_id_raises_integrity_error(conn):
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        update_mote(conn, "does-not-exist", {"name": "Alice"})


def test_update_mote_merges_properties(conn):
    mote_id = create_mote(conn, {"name": "Alice", "age": 30})
    update_mote(conn, mote_id, {"age": 31})
    mote = get_mote(conn, mote_id)
    assert mote.properties == {"name": "Alice", "age": 31}


def test_list_motes_returns_all(conn):
    create_mote(conn, {"name": "Alice"})
    create_mote(conn, {"name": "Bob"})
    motes = list_motes(conn)
    assert len(motes) == 2
    assert {m.properties["name"] for m in motes} == {"Alice", "Bob"}


def test_delete_mote_returns_true_when_found(conn):
    mote_id = create_mote(conn, {"name": "Alice"})
    assert delete_mote(conn, mote_id) is True
    assert get_mote(conn, mote_id) is None


def test_delete_mote_returns_false_when_not_found(conn):
    assert delete_mote(conn, "does-not-exist") is False


def test_delete_mote_cascades_properties(conn):
    mote_id = create_mote(conn, {"name": "Alice"})
    delete_mote(conn, mote_id)
    cur = conn.cursor()
    cur.execute("SELECT * FROM mote_properties WHERE mote_id=?", (mote_id,))
    assert cur.fetchall() == []


def test_delete_mote_cascades_relationships(conn):
    from core.relationships import create_relationship, list_relationships

    a = create_mote(conn, {"name": "Alice"})
    b = create_mote(conn, {"name": "Bob"})
    create_relationship(conn, a, b, "knows")

    delete_mote(conn, a)

    assert list_relationships(conn) == []

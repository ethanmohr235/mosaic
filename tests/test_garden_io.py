import pytest

from core.db import get_connection
from core.gardens import create_garden
from core.garden_io import EXPORT_FORMAT, export_garden, import_garden
from core.motes import create_mote, get_mote, list_motes
from core.relationships import create_relationship, list_relationships


def test_export_shape(conn):
    a = create_mote(conn, {"name": "Alice"})
    b = create_mote(conn, {"name": "Bob"})
    create_relationship(conn, a, b, "knows")

    data = export_garden(conn)

    assert data["format"] == EXPORT_FORMAT
    assert {m["id"] for m in data["motes"]} == {a, b}
    assert len(data["relationships"]) == 1
    assert data["relationships"][0]["from_id"] == a
    assert data["relationships"][0]["to_id"] == b
    assert data["relationships"][0]["type"] == "knows"


def test_export_empty_garden(conn):
    data = export_garden(conn)
    assert data == {"format": EXPORT_FORMAT, "motes": [], "relationships": []}


def test_round_trip_preserves_ids_and_data(conn):
    a = create_mote(conn, {"name": "Alice", "age": 30})
    b = create_mote(conn, {"name": "Bob"})
    rel_id = create_relationship(conn, a, b, "knows")

    data = export_garden(conn)

    create_garden("target")
    target_conn = get_connection("target")
    mote_count, rel_count = import_garden(target_conn, data)

    assert (mote_count, rel_count) == (2, 1)

    imported_alice = get_mote(target_conn, a)
    assert imported_alice is not None
    assert imported_alice.properties == {"name": "Alice", "age": 30}

    rels = list_relationships(target_conn)
    assert len(rels) == 1
    assert rels[0].id == rel_id
    assert rels[0].from_id == a
    assert rels[0].to_id == b
    target_conn.close()


def test_import_rejects_unknown_format(conn):
    with pytest.raises(ValueError):
        import_garden(conn, {"format": "something-else", "motes": [], "relationships": []})


def test_import_rejects_id_collisions_without_partial_writes(conn):
    a = create_mote(conn, {"name": "Alice"})
    data = export_garden(conn)

    create_garden("target")
    target_conn = get_connection("target")
    # Pre-seed the target with a mote sharing the same id as the export.
    create_mote(target_conn, {"name": "Existing"}, id=a)

    with pytest.raises(ValueError):
        import_garden(target_conn, data)

    # No partial write: the target still only has the one pre-seeded mote.
    assert len(list_motes(target_conn)) == 1
    target_conn.close()

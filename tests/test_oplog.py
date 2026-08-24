import json

from core import oplog
from core.device import get_device_id
from core.motes import create_mote, delete_mote, delete_property, update_mote
from core.relationships import create_relationship, delete_relationship, update_relationship


def _ops(conn):
    return conn.execute("SELECT * FROM oplog ORDER BY seq").fetchall()


def test_create_mote_logs_one_op(conn):
    mote_id = create_mote(conn, {"name": "Alice"})
    rows = _ops(conn)
    assert len(rows) == 1
    assert rows[0]["action"] == "create_mote"
    assert rows[0]["entity_type"] == "mote"
    assert rows[0]["entity_id"] == mote_id
    assert rows[0]["device_id"] == get_device_id()
    assert json.loads(rows[0]["payload"]) == {"id": mote_id, "properties": {"name": "Alice"}}


def test_update_mote_logs_one_op_per_key(conn):
    mote_id = create_mote(conn, {})
    update_mote(conn, mote_id, {"a": 1, "b": 2})
    rows = [r for r in _ops(conn) if r["action"] == "set_property"]
    assert {json.loads(r["payload"])["key"] for r in rows} == {"a", "b"}


def test_delete_property_logs_only_when_found(conn):
    mote_id = create_mote(conn, {"a": 1})
    assert delete_property(conn, mote_id, "a") is True
    assert delete_property(conn, mote_id, "missing") is False
    rows = [r for r in _ops(conn) if r["action"] == "delete_property"]
    assert len(rows) == 1


def test_delete_mote_logs_only_when_found(conn):
    mote_id = create_mote(conn, {})
    assert delete_mote(conn, "nonexistent") is False
    assert delete_mote(conn, mote_id) is True
    rows = [r for r in _ops(conn) if r["action"] == "delete_mote"]
    assert len(rows) == 1


def test_relationship_primitives_log_ops_in_order(conn):
    a = create_mote(conn, {})
    b = create_mote(conn, {})
    rel_id = create_relationship(conn, a, b, "knows")
    update_relationship(conn, rel_id, type_="likes")
    delete_relationship(conn, rel_id)

    actions = [r["action"] for r in _ops(conn) if r["entity_type"] == "relationship"]
    assert actions == ["create_relationship", "update_relationship", "delete_relationship"]


def test_suppressed_prevents_logging(conn):
    with oplog.suppressed():
        create_mote(conn, {"name": "ghost"})
    assert _ops(conn) == []

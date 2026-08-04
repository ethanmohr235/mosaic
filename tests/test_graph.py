from core.graph import most_recent_mote_id, neighborhood, relationships_touching
from core.motes import create_mote, delete_property, update_mote
from core.relationships import create_relationship


def test_most_recent_mote_id_none_when_empty(conn):
    assert most_recent_mote_id(conn) is None


def test_most_recent_mote_id_returns_last_inserted(conn):
    create_mote(conn, {"name": "Alice"})
    create_mote(conn, {"name": "Bob"})
    last = create_mote(conn, {"name": "Carol"})
    assert most_recent_mote_id(conn) == last


def test_relationships_touching_matches_either_endpoint(conn):
    a = create_mote(conn, {"name": "Alice"})
    b = create_mote(conn, {"name": "Bob"})
    c = create_mote(conn, {"name": "Carol"})
    r1 = create_relationship(conn, a, b, "knows")
    r2 = create_relationship(conn, c, a, "manages")
    create_relationship(conn, b, c, "knows")

    touching_a = {r.id for r in relationships_touching(conn, a)}
    assert touching_a == {r1, r2}


def test_neighborhood_zero_hops_returns_only_start(conn):
    a = create_mote(conn, {"name": "Alice"})
    b = create_mote(conn, {"name": "Bob"})
    create_relationship(conn, a, b, "knows")

    motes, rels = neighborhood(conn, a, hops=0)
    assert [m.id for m in motes] == [a]
    assert rels == []


def test_neighborhood_one_hop_direct_neighbors_only(conn):
    a = create_mote(conn, {"name": "Alice"})
    b = create_mote(conn, {"name": "Bob"})
    c = create_mote(conn, {"name": "Carol"})
    create_relationship(conn, a, b, "knows")
    create_relationship(conn, b, c, "knows")

    motes, rels = neighborhood(conn, a, hops=1)
    assert {m.id for m in motes} == {a, b}
    assert len(rels) == 1


def test_neighborhood_two_hops_transitive(conn):
    a = create_mote(conn, {"name": "Alice"})
    b = create_mote(conn, {"name": "Bob"})
    c = create_mote(conn, {"name": "Carol"})
    create_relationship(conn, a, b, "knows")
    create_relationship(conn, b, c, "knows")

    motes, rels = neighborhood(conn, a, hops=2)
    assert {m.id for m in motes} == {a, b, c}
    assert len(rels) == 2


def test_neighborhood_reaches_source_of_incoming_edge(conn):
    # Alice --works_on--> Project: focusing on Project should still surface
    # Alice one hop out, even though the edge points into Project.
    alice = create_mote(conn, {"name": "Alice"})
    project = create_mote(conn, {"name": "Project"})
    create_relationship(conn, alice, project, "works_on")

    motes, rels = neighborhood(conn, project, hops=1)
    assert {m.id for m in motes} == {alice, project}
    assert len(rels) == 1
    assert rels[0].from_id == alice
    assert rels[0].to_id == project


def test_neighborhood_excludes_disconnected_components(conn):
    a = create_mote(conn, {"name": "Alice"})
    b = create_mote(conn, {"name": "Bob"})
    create_mote(conn, {"name": "Unrelated"})
    create_relationship(conn, a, b, "knows")

    motes, _ = neighborhood(conn, a, hops=5)
    assert {m.id for m in motes} == {a, b}


def test_neighborhood_handles_cycles_without_looping_forever(conn):
    a = create_mote(conn, {"name": "Alice"})
    b = create_mote(conn, {"name": "Bob"})
    create_relationship(conn, a, b, "knows")
    create_relationship(conn, b, a, "knows")

    motes, rels = neighborhood(conn, a, hops=10)
    assert {m.id for m in motes} == {a, b}
    assert len(rels) == 2


def test_neighborhood_nonexistent_start_returns_empty(conn):
    motes, rels = neighborhood(conn, "does-not-exist", hops=2)
    assert motes == []
    assert rels == []


def test_delete_property_removes_key_leaves_others(conn):
    mote_id = create_mote(conn, {"name": "Alice", "age": 30})
    assert delete_property(conn, mote_id, "age") is True

    from core.motes import get_mote
    mote = get_mote(conn, mote_id)
    assert mote.properties == {"name": "Alice"}


def test_delete_property_returns_false_for_unknown_key(conn):
    mote_id = create_mote(conn, {"name": "Alice"})
    assert delete_property(conn, mote_id, "nope") is False


def test_delete_property_returns_false_for_unknown_mote(conn):
    assert delete_property(conn, "does-not-exist", "name") is False

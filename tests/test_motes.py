import pytest

from core.motes import copy_mote, create_mote, get_mote, get_motes, list_motes, search_motes, update_mote, delete_mote


def _count_queries(conn, fn):
    """Runs fn() with SQL tracing on, returns (result, query_count)."""
    count = 0

    def trace(_sql):
        nonlocal count
        count += 1

    conn.set_trace_callback(trace)
    try:
        result = fn()
    finally:
        conn.set_trace_callback(None)
    return result, count


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


def test_list_motes_issues_a_constant_number_of_queries(conn):
    """Regression guard: list_motes() used to issue one properties query
    per mote (N+1). Seeding a batch and counting executed statements
    catches that coming back even if a future change reintroduces it."""
    for i in range(50):
        create_mote(conn, {"name": f"mote-{i}"})

    _, query_count = _count_queries(conn, lambda: list_motes(conn))
    assert query_count <= 3, f"list_motes() should not scale with mote count, issued {query_count} queries"


def test_get_motes_returns_in_input_order_and_skips_missing(conn):
    a = create_mote(conn, {"name": "Alice"})
    b = create_mote(conn, {"name": "Bob"})
    motes = get_motes(conn, [b, "does-not-exist", a])
    assert [m.id for m in motes] == [b, a]


def test_get_motes_matches_get_mote_per_item(conn):
    a = create_mote(conn, {"name": "Alice", "age": 30})
    b = create_mote(conn, {"name": "Bob"})
    [ma, mb] = get_motes(conn, [a, b])
    assert ma == get_mote(conn, a)
    assert mb == get_mote(conn, b)


def test_get_motes_empty_list_returns_empty(conn):
    assert get_motes(conn, []) == []


def test_get_motes_issues_a_constant_number_of_queries(conn):
    ids = [create_mote(conn, {"name": f"mote-{i}"}) for i in range(50)]
    _, query_count = _count_queries(conn, lambda: get_motes(conn, ids))
    assert query_count <= 3, f"get_motes() should not scale with id count, issued {query_count} queries"


def test_search_motes_empty_query_returns_everything(conn):
    create_mote(conn, {"name": "Alice"})
    create_mote(conn, {"name": "Bob"})
    assert len(search_motes(conn, "")) == 2
    assert len(search_motes(conn, "   ")) == 2


def test_search_motes_matches_substring_in_property_value(conn):
    a = create_mote(conn, {"name": "Alice Turbine"})
    create_mote(conn, {"name": "Bob"})
    assert [m.id for m in search_motes(conn, "turb")] == [a]


def test_search_motes_is_case_insensitive(conn):
    a = create_mote(conn, {"name": "Alice"})
    assert [m.id for m in search_motes(conn, "ALICE")] == [a]


def test_search_motes_matches_property_key(conn):
    a = create_mote(conn, {"resistance": 100})
    create_mote(conn, {"name": "Bob"})
    assert [m.id for m in search_motes(conn, "resist")] == [a]


def test_search_motes_matches_non_string_value_via_json(conn):
    a = create_mote(conn, {"active": True})
    create_mote(conn, {"active": False})
    assert [m.id for m in search_motes(conn, "true")] == [a]


def test_search_motes_matches_mote_id(conn):
    a = create_mote(conn, {"name": "Alice"}, id="turbine-1")
    create_mote(conn, {"name": "Bob"}, id="pump-1")
    assert [m.id for m in search_motes(conn, "turbine")] == [a]


def test_search_motes_key_value_syntax_requires_exact_match(conn):
    a = create_mote(conn, {"status": "open"})
    create_mote(conn, {"status": "opening"})
    assert [m.id for m in search_motes(conn, "status = open")] == [a]


def test_search_motes_key_value_syntax_with_no_value_matches_any_value(conn):
    a = create_mote(conn, {"status": "open"})
    b = create_mote(conn, {"status": "closed"})
    create_mote(conn, {"name": "no status property"})
    assert {m.id for m in search_motes(conn, "status =")} == {a, b}


def test_search_motes_no_match_returns_empty(conn):
    create_mote(conn, {"name": "Alice"})
    assert search_motes(conn, "nonexistent") == []


def test_search_motes_respects_limit(conn):
    for i in range(5):
        create_mote(conn, {"name": f"item-{i}"})
    assert len(search_motes(conn, "item", limit=2)) == 2


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


# ---- copy_mote: one primitive for seeding a new mote from an old one ----

def test_copy_mote_within_one_garden(conn):
    original = create_mote(conn, {"name": "Standup", "duration": 15})

    copy_id = copy_mote(conn, conn, original)

    assert copy_id != original
    assert get_mote(conn, copy_id).properties == {"name": "Standup", "duration": 15}
    # Independent: editing the copy leaves the original alone.
    update_mote(conn, copy_id, {"name": "Retro"})
    assert get_mote(conn, original).properties["name"] == "Standup"


def test_copy_mote_applies_overrides(conn):
    original = create_mote(conn, {"name": "Standup", "duration": 15})
    copy_id = copy_mote(conn, conn, original, {"name": "Monday standup"})
    assert get_mote(conn, copy_id).properties == {"name": "Monday standup", "duration": 15}


def test_copy_mote_across_gardens(conn):
    """The reason this takes two connections: the library copies in and
    out of a different garden entirely."""
    from core.db import get_connection, initialize_database
    from core.gardens import create_garden

    original = create_mote(conn, {"name": "Reactor"})
    create_garden("target")
    target = get_connection("target")
    try:
        initialize_database(target)
        copy_id = copy_mote(conn, target, original)
        assert get_mote(target, copy_id).properties == {"name": "Reactor"}
        # ...and it is genuinely over there, not here.
        assert get_mote(conn, copy_id) is None
    finally:
        target.close()


def test_copy_mote_does_not_copy_relationships(conn):
    """Properties only -- there is no general answer to what a
    relationship pointing outside the copied set should become."""
    from core.relationships import create_relationship, list_relationships

    a = create_mote(conn, {"name": "A"})
    b = create_mote(conn, {"name": "B"})
    create_relationship(conn, a, b, "mentions")

    copy_mote(conn, conn, a)

    assert len(list_relationships(conn)) == 1


def test_copy_mote_raises_for_a_missing_source(conn):
    with pytest.raises(LookupError):
        copy_mote(conn, conn, "nonexistent")

from core.groups import group_of, list_folders, members_of, set_group
from core.motes import create_mote


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


def test_set_group_creates_groups_relationship(conn):
    folder = create_mote(conn, {"name": "Thermodynamics"})
    m1 = create_mote(conn, {"name": "Entropy"})
    set_group(conn, m1, folder)

    assert [m.id for m in group_of(conn, m1)] == [folder]
    assert [m.id for m in members_of(conn, folder)] == [m1]


def test_mote_with_no_folder_returns_empty(conn):
    lonely = create_mote(conn, {"name": "Lonely"})
    assert group_of(conn, lonely) == []


def test_folder_with_no_members_returns_empty(conn):
    folder = create_mote(conn, {"name": "Empty"})
    assert members_of(conn, folder) == []


def test_mote_can_belong_to_multiple_folders(conn):
    thermo = create_mote(conn, {"name": "Thermodynamics"})
    favorites = create_mote(conn, {"name": "Favorites"})
    entropy = create_mote(conn, {"name": "Entropy"})
    set_group(conn, entropy, thermo)
    set_group(conn, entropy, favorites)

    assert {m.id for m in group_of(conn, entropy)} == {thermo, favorites}


def test_list_folders_returns_every_mote_with_members(conn):
    thermo = create_mote(conn, {"name": "Thermodynamics"})
    unused = create_mote(conn, {"name": "Unused folder"})
    entropy = create_mote(conn, {"name": "Entropy"})
    create_mote(conn, {"name": "Unrelated"})
    set_group(conn, entropy, thermo)

    assert {m.id for m in list_folders(conn)} == {thermo}
    assert unused not in {m.id for m in list_folders(conn)}


def test_list_folders_empty_when_no_groups_relationships(conn):
    create_mote(conn, {"name": "Alice"})
    assert list_folders(conn) == []


def test_group_of_and_members_of_issue_a_constant_number_of_queries(conn):
    """Regression guard: group_of()/members_of() used to issue one
    properties query per related mote (N+1) -- mirrors the same check
    for species_of()/instances_of() in tests/test_species.py."""
    thermo = create_mote(conn, {"name": "Thermodynamics"})
    multi_folder_mote = create_mote(conn, {"name": "belongs to many"})
    for i in range(50):
        member = create_mote(conn, {"name": f"m{i}"})
        set_group(conn, member, thermo)
        folder = create_mote(conn, {"name": f"f{i}"})
        set_group(conn, multi_folder_mote, folder)

    _, group_of_count = _count_queries(conn, lambda: group_of(conn, multi_folder_mote))
    assert group_of_count <= 3, f"group_of() should not scale with folder count, issued {group_of_count} queries"

    _, members_of_count = _count_queries(conn, lambda: members_of(conn, thermo))
    assert members_of_count <= 3, f"members_of() should not scale with member count, issued {members_of_count} queries"

from core.folders import folder_ids_in, folder_members, folders_of, list_folders, set_folder
from core.entities import Relationship
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


def test_set_folder_creates_an_in_folder_relationship(conn):
    folder = create_mote(conn, {"name": "Thermodynamics"})
    m1 = create_mote(conn, {"name": "Entropy"})
    set_folder(conn, m1, folder)

    assert [m.id for m in folders_of(conn, m1)] == [folder]
    assert [m.id for m in folder_members(conn, folder)] == [m1]


def test_mote_with_no_folder_returns_empty(conn):
    lonely = create_mote(conn, {"name": "Lonely"})
    assert folders_of(conn, lonely) == []


def test_folder_with_no_members_returns_empty(conn):
    folder = create_mote(conn, {"name": "Empty"})
    assert folder_members(conn, folder) == []


def test_mote_can_belong_to_multiple_folders(conn):
    thermo = create_mote(conn, {"name": "Thermodynamics"})
    favorites = create_mote(conn, {"name": "Favorites"})
    entropy = create_mote(conn, {"name": "Entropy"})
    set_folder(conn, entropy, thermo)
    set_folder(conn, entropy, favorites)

    assert {m.id for m in folders_of(conn, entropy)} == {thermo, favorites}


def test_list_folders_returns_every_mote_with_members(conn):
    thermo = create_mote(conn, {"name": "Thermodynamics"})
    unused = create_mote(conn, {"name": "Unused folder"})
    entropy = create_mote(conn, {"name": "Entropy"})
    create_mote(conn, {"name": "Unrelated"})
    set_folder(conn, entropy, thermo)

    assert {m.id for m in list_folders(conn)} == {thermo}
    assert unused not in {m.id for m in list_folders(conn)}


def test_list_folders_empty_when_no_in_folder_relationships(conn):
    create_mote(conn, {"name": "Alice"})
    assert list_folders(conn) == []


def test_folders_of_and_folder_members_issue_a_constant_number_of_queries(conn):
    """Regression guard: folders_of()/folder_members() used to issue one
    properties query per related mote (N+1) -- mirrors the same check
    for species_of()/instances_of() in tests/test_species.py."""
    thermo = create_mote(conn, {"name": "Thermodynamics"})
    multi_folder_mote = create_mote(conn, {"name": "belongs to many"})
    for i in range(50):
        member = create_mote(conn, {"name": f"m{i}"})
        set_folder(conn, member, thermo)
        folder = create_mote(conn, {"name": f"f{i}"})
        set_folder(conn, multi_folder_mote, folder)

    _, folders_of_count = _count_queries(conn, lambda: folders_of(conn, multi_folder_mote))
    assert folders_of_count <= 3, f"folders_of() should not scale with folder count, issued {folders_of_count} queries"

    _, folder_members_count = _count_queries(conn, lambda: folder_members(conn, thermo))
    assert folder_members_count <= 3, f"folder_members() should not scale with member count, issued {folder_members_count} queries"


# ---- folder_ids_in: the pure counterpart of list_folders ----

def test_folder_ids_in_counts_only_in_folder_relationships():
    """Mirrors core/species.py::species_ids_in -- one definition of "is a
    folder", used both by list_folders() and by monarch/api.py classifying an
    already-fetched subgraph."""
    rels = [
        Relationship(from_id="entropy", to_id="thermo", type="in_folder"),
        Relationship(from_id="r1", to_id="resistor", type="instance_of"),
    ]
    assert folder_ids_in(rels) == {"thermo"}


def test_folder_ids_in_agrees_with_list_folders(conn):
    folder = create_mote(conn, {"name": "Thermodynamics"})
    member = create_mote(conn, {"name": "Entropy"})
    set_folder(conn, member, folder)

    from core.relationships import list_relationships
    assert folder_ids_in(list_relationships(conn)) == {m.id for m in list_folders(conn)}

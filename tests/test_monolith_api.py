"""Monolith's JS bridge.

What earns a test here is behavior this layer adds -- which columns a
garden discovers, that a bulk edit writes every id it was given and
nothing else, that a delete can be undone under the same ids, and that a
saved view round-trips through a mote. The query language itself is
tests/test_query.py's; a method that only forwards to a repository is
already covered by that repository's tests. Same line mora/api.py's own
tests draw.
"""
import pytest

from core import query as q
from core import species as species_repo
from core.gardens import create_garden
from core.graph import whole_garden
from core.motes import create_mote, get_mote
from core.relationships import create_relationship
from monolith import payload as payload_repo
from momentum import app as momentum_port
from monarch import app as monarch_port
from monastery import app as monastery_port
from monolith.api import Api
from monolith.app import DEFAULT_HTTP_PORT
from mora import app as mora_port
from window_launcher import choose_port


@pytest.fixture
def api():
    create_garden("test")
    instance = Api("test")
    yield instance
    instance.shutdown()


@pytest.fixture
def garden(api) -> dict:
    """A species, three of its instances and a relationship between two
    of them -- enough for a property column, a rel column and a kind."""
    conn = api._conn
    species = create_mote(conn, {"name": "Task"})
    ids = {"species": species}
    for name, status in (("Schema", "doing"), ("Backfill", "doing"), ("Rehearsal", "todo")):
        ids[name.lower()] = create_mote(conn, {"name": name, "status": status, "estimate": 3})
        create_relationship(conn, ids[name.lower()], species, q.INSTANCE_OF)
    create_relationship(conn, ids["backfill"], ids["schema"], "depends_on")
    return ids


def columns_of(answer) -> list[str]:
    return [c["key"] for c in answer["schema"]["columns"]]


def rows_of(answer) -> list[str]:
    return [row["id"] for group in answer["groups"] for row in group["rows"]]


def query_with(**kwargs) -> dict:
    return q.Query(**kwargs).to_json()


# ---- what a garden's columns are ----

def test_columns_are_discovered_from_what_the_garden_holds(api, garden):
    keys = columns_of(api.table())
    assert "property:status" in keys
    assert "rel:out:depends_on" in keys
    assert "rel:in:depends_on" in keys
    assert "builtin:updated" in keys
    assert "graph:kind" in keys


def test_a_property_column_offers_its_distinct_values_as_options(api, garden):
    status = next(c for c in api.table()["schema"]["columns"] if c["key"] == "property:status")
    assert status["values"] == ["doing", "todo"]
    assert status["kind"] == q.TEXT
    assert status["editable"]


def test_a_numeric_column_is_recognized_and_offered_numeric_operators(api, garden):
    estimate = next(c for c in api.table()["schema"]["columns"] if c["key"] == "property:estimate")
    assert estimate["kind"] == q.NUMBER
    assert ">" in estimate["ops"]


def test_the_graphs_own_columns_are_never_editable(api, garden):
    columns = {c["key"]: c for c in api.table()["schema"]["columns"]}
    assert not columns["graph:kind"]["editable"]
    assert not columns["builtin:degree"]["editable"]
    assert columns["rel:out:depends_on"]["editable"]


def test_a_garden_nobody_configured_opens_on_a_usable_table(api, garden):
    answer = api.table()
    keys = [c["key"] for c in answer["columns"]]
    assert keys[0] == "builtin:name"
    assert "property:status" in keys
    assert len(rows_of(answer)) == 4


# ---- querying ----

def test_a_condition_narrows_the_rows_but_not_the_total(api, garden):
    conditions = (q.Condition(q.Field(q.PROPERTY, "status"), "is", "doing"),)
    answer = api.table(query_with(conditions=conditions))
    assert answer["matched"] == 2
    assert answer["total"] == 4
    assert set(rows_of(answer)) == {garden["schema"], garden["backfill"]}


def test_grouping_cuts_the_same_rows_into_labelled_groups(api, garden):
    answer = api.table(query_with(group=q.Field(q.PROPERTY, "status")))
    assert [(g["label"], g["count"]) for g in answer["groups"]] == [
        ("doing", 2), ("todo", 1), (q.UNSET, 1),
    ]


def test_a_rel_cell_reads_the_neighbours_name(api, garden):
    columns = (q.Field(q.REL, "depends_on", q.OUT),)
    answer = api.table(query_with(columns=columns))
    cells = {row["id"]: row["cells"][0] for group in answer["groups"] for row in group["rows"]}
    assert cells[garden["backfill"]]["text"] == "Schema"
    assert cells[garden["backfill"]]["ids"] == [garden["schema"]]
    assert cells[garden["rehearsal"]]["empty"]


def test_a_query_naming_a_vanished_property_still_opens(api, garden):
    """A saved view outlives the key it filters on."""
    columns = (q.Field(q.PROPERTY, "gone"),)
    answer = api.table(query_with(columns=columns))
    assert answer["columns"][0]["key"] == "property:gone"
    assert all(row["cells"][0]["empty"] for group in answer["groups"] for row in group["rows"])


# ---- editing ----

def test_set_property_writes_every_id_it_was_given_and_no_others(api, garden):
    ids = [garden["schema"], garden["backfill"]]
    assert api.set_property(ids, "owner", "Rina")["written"] == 2
    conn = api._conn
    assert get_mote(conn, garden["schema"]).properties["owner"] == "Rina"
    assert get_mote(conn, garden["backfill"]).properties["owner"] == "Rina"
    assert "owner" not in get_mote(conn, garden["rehearsal"]).properties


def test_a_typed_number_is_stored_as_a_number(api, garden):
    """api_common.coerce, so that `estimate > 3` keeps meaning what it
    says after somebody types 5 into the cell."""
    api.set_property([garden["schema"]], "estimate", "5")
    assert get_mote(api._conn, garden["schema"]).properties["estimate"] == 5


def test_a_stale_selection_writes_the_rows_that_are_still_there(api, garden):
    written = api.set_property([garden["schema"], "deleted-by-another-window"], "owner", "Rina")
    assert written["written"] == 1


def test_clearing_removes_the_key_rather_than_emptying_it(api, garden):
    api.clear_property([garden["schema"]], "status")
    assert "status" not in get_mote(api._conn, garden["schema"]).properties


def test_deleting_needs_no_confirmation_but_can_be_undone(api, garden):
    answer = api.delete_motes([garden["schema"]])
    assert answer["deleted"] == 1
    assert get_mote(api._conn, garden["schema"]) is None
    _motes, relationships = whole_garden(api._conn)
    assert garden["schema"] not in {r.from_id for r in relationships} | {r.to_id for r in relationships}

    restored = api.undo()
    assert restored["undone"] == "delete a mote"
    assert get_mote(api._conn, garden["schema"]).properties["name"] == "Schema"
    _motes, relationships = whole_garden(api._conn)
    pairs = {(r.from_id, r.to_id, r.type) for r in relationships}
    assert (garden["schema"], garden["species"], q.INSTANCE_OF) in pairs
    assert (garden["backfill"], garden["schema"], "depends_on") in pairs


def test_redoing_a_delete_removes_the_mote_again(api, garden):
    api.delete_motes([garden["schema"]])
    api.undo()

    redone = api.redo()
    assert redone["redone"] == "delete a mote"
    assert get_mote(api._conn, garden["schema"]) is None


def test_deleting_several_motes_is_undone_in_one_step(api, garden):
    answer = api.delete_motes([garden["schema"], garden["backfill"]])
    assert answer["deleted"] == 2

    restored = api.undo()
    assert restored["undone"] == "delete 2 motes"
    assert get_mote(api._conn, garden["schema"]) is not None
    assert get_mote(api._conn, garden["backfill"]) is not None
    assert api.undo()["undone"] is None  # the whole batch was one step


def test_setting_a_property_can_be_undone(api, garden):
    api.set_property([garden["schema"]], "owner", "Rina")
    assert api.undo()["undone"] == "set owner"
    assert "owner" not in get_mote(api._conn, garden["schema"]).properties


def test_creating_a_mote_can_be_undone(api, garden):
    created = api.create_mote({"name": "New mote"})
    assert api.undo()["undone"] == "create a mote"
    assert get_mote(api._conn, created["id"]) is None


# ---- editing relationships ----

def test_relate_writes_one_edge_per_pair_and_skips_the_ones_already_there(api, garden):
    pairs = [[garden["rehearsal"], garden["schema"]], [garden["backfill"], garden["schema"]]]
    assert api.relate(pairs, "depends_on")["written"] == 1
    _motes, relationships = whole_garden(api._conn)
    depends = [r for r in relationships if r.type == "depends_on"]
    assert len(depends) == 2


def test_relate_refuses_to_point_a_mote_at_itself(api, garden):
    assert api.relate([[garden["schema"], garden["schema"]]], "depends_on")["written"] == 0


def test_unrelate_erases_by_pair_and_type(api, garden):
    assert api.unrelate([[garden["backfill"], garden["schema"]]], "depends_on")["removed"] == 1
    _motes, relationships = whole_garden(api._conn)
    assert not [r for r in relationships if r.type == "depends_on"]
    # The instance_of edges between the same motes' species are untouched.
    assert len([r for r in relationships if r.type == q.INSTANCE_OF]) == 3


def test_the_matrix_covers_the_visible_rows_and_says_how_many(api, garden):
    matrix = api.table()["matrix"]
    positions = {m["id"]: i for i, m in enumerate(matrix["motes"])}
    assert matrix["shown"] == matrix["rows"] == 4
    depends = [e for e in matrix["edges"] if e["type"] == "depends_on"]
    assert len(depends) == 1
    assert (depends[0]["from"], depends[0]["to"]) == (
        positions[garden["backfill"]], positions[garden["schema"]])


# ---- saved views ----

def test_a_saved_view_is_an_ordinary_mote_carrying_a_query(api, garden):
    conditions = (q.Condition(q.Field(q.PROPERTY, "status"), "is", "doing"),)
    saved = api.save_view("In flight", query_with(conditions=conditions))
    mote = get_mote(api._conn, saved["id"])
    assert mote.properties["name"] == "In flight"
    assert q.has_query(mote.properties)
    assert q.Query.from_json(mote.properties[q.QUERY_KEY]).conditions == conditions


def test_a_saved_view_comes_back_in_the_view_list_with_its_summary(api, garden):
    conditions = (q.Condition(q.Field(q.PROPERTY, "status"), "is", "doing"),)
    saved = api.save_view("In flight", query_with(conditions=conditions))
    views = api.table()["views"]
    assert [v["id"] for v in views] == [saved["id"]]
    assert views[0]["summary"] == "status is doing"


def test_saving_over_a_view_updates_it_rather_than_adding_one(api, garden):
    first = api.save_view("In flight", query_with())
    again = api.save_view("In flight", query_with(join=q.OR), view_id=first["id"])
    assert again == {"id": first["id"], "created": False}
    assert len(api.table()["views"]) == 1


def test_opening_on_a_named_view_starts_from_its_query(api, garden):
    conditions = (q.Condition(q.Field(q.PROPERTY, "status"), "is", "todo"),)
    saved = api.save_view("Todo", query_with(conditions=conditions))
    opened = Api("test", saved["id"])
    try:
        assert rows_of(opened.table()) == [garden["rehearsal"]]
    finally:
        opened.shutdown()


def test_a_view_mote_is_not_a_kind(api, garden):
    """Carrying a query is data, like a url or a date -- the same line
    mora/api.py::list_notes draws for `modoc`. A view mote that read as a
    kind would displace its species in every other window's type label."""
    saved = api.save_view("In flight", query_with())
    row = next(r for g in api.table()["groups"] for r in g["rows"] if r["id"] == saved["id"])
    assert row["kinds"] == []


def test_saving_a_view_makes_it_an_instance_of_saved_view(api, garden):
    """Same as momentum/api.py::log_session declaring a logged session an
    instance of `Work Session`: every saved view is an instance of one
    `Saved View` species, found or created lazily the first time a view is
    saved -- so a garden's saved views show up as one kind of thing in
    Monarch and Monastery."""
    first = api.save_view("In flight", query_with())
    second = api.save_view("Todo", query_with())
    species = [m for m in species_repo.list_species(api._conn)
               if m.properties.get("name") == "Saved View"]
    assert len(species) == 1
    instance_ids = {m.id for m in species_repo.instances_of(api._conn, species[0].id)}
    assert instance_ids == {first["id"], second["id"]}


def test_deleting_a_view_removes_only_that_mote(api, garden):
    before = api.table()["total"]
    saved = api.save_view("In flight", query_with())
    assert api.delete_view(saved["id"])["deleted"]
    assert api.table()["views"] == []
    # The view instance is gone; the `Saved View` species it pointed at
    # (created lazily by save_view) is not -- a species persists past its
    # last instance, same as momentum/api.py's `Work Session`.
    assert api.table()["total"] == before + 1


# ---- the window ----

def test_monolith_asks_for_a_port_the_other_four_do_not(api):
    """mora/app.py's comment explains what a collision costs: the loser
    of the bind loads the winner's page against its own bridge. What is
    asserted is the *preference*, not that the port is free right now --
    a developer with Monolith already open would otherwise fail this."""
    assert DEFAULT_HTTP_PORT == 42005
    assert DEFAULT_HTTP_PORT not in {
        monarch_port.DEFAULT_HTTP_PORT, mora_port.DEFAULT_HTTP_PORT,
        monastery_port.DEFAULT_HTTP_PORT, momentum_port.DEFAULT_HTTP_PORT,
    }
    assert choose_port("Monolith", DEFAULT_HTTP_PORT) > 0


def test_the_matrix_row_cap_is_honest_about_itself(api):
    conn = api._conn
    for index in range(payload_repo.MATRIX_ROWS + 5):
        create_mote(conn, {"name": f"Mote {index:02d}"})
    matrix = api.table()["matrix"]
    assert matrix["shown"] == payload_repo.MATRIX_ROWS
    assert matrix["rows"] == payload_repo.MATRIX_ROWS + 5

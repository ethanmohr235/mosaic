"""core/views.py -- a query kept under a name.

The storage design is the whole thing worth testing: a view is an ordinary
mote carrying a `query`, it is an instance of a `Saved View` species, and
carrying one is deliberately *not* a kind. Ported from
tests/test_monolith_api.py.
"""
import pytest

from core import kinds as kinds_repo
from core import query as q
from core import species as species_repo
from core import views
from core.graph import whole_garden
from core.motes import create_mote, get_mote
from core.undo import UndoManager


@pytest.fixture
def undo_stack() -> UndoManager:
    return UndoManager()


def apply(undo_stack, result):
    value, command = result
    if command is not None:
        undo_stack.push(command)
    return value


DOING = q.Query(conditions=(q.Condition(q.Field(q.PROPERTY, "status"), "is", "doing"),))


def test_a_saved_view_is_an_ordinary_mote_carrying_a_query(conn):
    saved, _ = views.save_view(conn, "Doing", DOING)
    mote = get_mote(conn, saved["id"])
    assert saved["created"]
    assert mote.properties["name"] == "Doing"
    assert mote.properties[q.QUERY_KEY]["conditions"][0]["value"] == "doing"


def test_a_saved_view_comes_back_in_the_list(conn):
    views.save_view(conn, "Doing", DOING)
    listed = views.list_views(conn)
    assert [views.name_of(v) for v in listed] == ["Doing"]
    assert views.query_of(listed[0]).conditions == DOING.conditions


def test_views_are_listed_by_name(conn):
    for name in ("Zebra", "apple", "Mango"):
        views.save_view(conn, name, DOING)
    assert [views.name_of(v) for v in views.list_views(conn)] == ["apple", "Mango", "Zebra"]


def test_a_view_with_no_name_gets_a_placeholder(conn):
    saved, _ = views.save_view(conn, "", DOING)
    assert get_mote(conn, saved["id"]).properties["name"] == views.UNTITLED


def test_saving_over_a_view_updates_it_rather_than_adding_one(conn):
    first, _ = views.save_view(conn, "Doing", DOING)
    again, _ = views.save_view(conn, "Doing, sorted", q.Query(
        sort=q.Sort(q.Field(q.BUILTIN, q.BUILTIN_NAME))), view_id=first["id"])
    assert again == {"id": first["id"], "created": False}
    assert len(views.list_views(conn)) == 1
    assert views.name_of(get_mote(conn, first["id"])) == "Doing, sorted"


def test_saving_under_an_id_that_no_longer_exists_creates_a_new_view(conn):
    saved, _ = views.save_view(conn, "Doing", DOING, view_id="gone")
    assert saved["created"]
    assert get_mote(conn, saved["id"]) is not None


def test_a_query_is_reparsed_rather_than_stored_as_handed_in(conn):
    """What is written is exactly what can be read back, so a view saved
    from a surface that sent a stray key does not come back with one."""
    saved, _ = views.save_view(conn, "Doing", DOING)
    stored = get_mote(conn, saved["id"]).properties[q.QUERY_KEY]
    assert set(stored) == set(DOING.to_json())


def test_saving_a_view_makes_it_an_instance_of_saved_view(conn):
    saved, _ = views.save_view(conn, "Doing", DOING)
    species = species_repo.species_of(conn, saved["id"])
    assert [s.properties["name"] for s in species] == [views.VIEW_SPECIES_NAME]


def test_the_saved_view_species_is_made_once_and_reused(conn):
    views.save_view(conn, "One", DOING)
    views.save_view(conn, "Two", DOING)
    named = [
        m for m in species_repo.list_species(conn)
        if m.properties.get("name") == views.VIEW_SPECIES_NAME
    ]
    assert len(named) == 1


def test_a_garden_that_never_saved_a_view_never_grows_the_species(conn):
    create_mote(conn, {"name": "Schema"})
    assert not [
        m for m in species_repo.list_species(conn)
        if m.properties.get("name") == views.VIEW_SPECIES_NAME
    ]


def test_a_view_mote_is_not_a_kind(conn):
    """Carrying a `query` is data, like a url or a date. A view mote that
    read as a kind would displace its real species in every surface's type
    label."""
    saved, _ = views.save_view(conn, "Doing", DOING)
    motes, relationships = whole_garden(conn)
    assert saved["id"] not in kinds_repo.kinds_in(motes, relationships)


def test_find_view_by_name_ignores_case_and_surrounding_space(conn):
    saved, _ = views.save_view(conn, "This week", DOING)
    assert views.find_view_by_name(conn, "  this WEEK ").id == saved["id"]


def test_find_view_by_name_answers_none_for_a_name_nobody_saved(conn):
    views.save_view(conn, "This week", DOING)
    assert views.find_view_by_name(conn, "Next week") is None
    assert views.find_view_by_name(conn, "") is None


def test_a_hand_edited_query_comes_back_narrower_rather_than_raising(conn):
    """A view's property is editable from every surface, so nonsense in it
    must not be an exception in whatever opens it."""
    mote_id = create_mote(conn, {"name": "Broken", q.QUERY_KEY: "not a query at all"})
    assert views.query_of(get_mote(conn, mote_id)) == q.Query()
    assert [v.id for v in views.list_views(conn)] == [mote_id]


# ---- undo ----

def test_saving_a_view_can_be_undone(conn, undo_stack):
    saved = apply(undo_stack, views.save_view(conn, "Doing", DOING))

    assert undo_stack.undo() == "save view"
    assert get_mote(conn, saved["id"]) is None


def test_undoing_a_resave_restores_the_previous_query(conn, undo_stack):
    first, _ = views.save_view(conn, "Doing", DOING)
    apply(undo_stack, views.save_view(conn, "Everything", q.Query(), view_id=first["id"]))

    assert undo_stack.undo() == "save view"
    assert views.name_of(get_mote(conn, first["id"])) == "Doing"
    assert views.query_of(get_mote(conn, first["id"])).conditions == DOING.conditions

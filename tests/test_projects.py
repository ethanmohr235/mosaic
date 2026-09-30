import pytest

import core.db as db
from core import gardens as gardens_repo, motes as motes_repo, projects


def _garden(name: str, *motes: dict) -> list[str]:
    gardens_repo.create_garden(name)
    conn = db.get_connection(name)
    try:
        return [motes_repo.create_mote(conn, props) for props in motes]
    finally:
        conn.close()



# --- reading claims ---------------------------------------------------

def test_a_blank_or_non_string_garden_property_is_no_claim():
    assert projects.garden_named_by({"garden": "hw4"}) == "hw4"
    assert projects.garden_named_by({"garden": "  hw4 "}) == "hw4"
    assert projects.garden_named_by({"garden": ""}) is None
    assert projects.garden_named_by({"garden": 3}) is None
    assert projects.garden_named_by({}) is None


def test_owners_are_keyed_by_the_garden_they_claim():
    (hw,) = _garden("college", {"name": "HW 4", "garden": "hw4", "due": "2026-10-02"})
    _garden("hw4")
    claims = projects.owners()
    assert list(claims) == ["hw4"]
    (owner,) = claims["hw4"]
    assert (owner.garden, owner.mote_id, owner.name, owner.owns) == ("college", hw, "HW 4", "hw4")
    assert owner.properties["due"] == "2026-10-02"
    assert owner.exists
    assert projects.owner_of("hw4", claims) == owner
    assert projects.owner_of("college", claims) is None


def test_an_unnamed_cover_is_named_by_its_id_not_its_equation():
    (mid,) = _garden("a", {"garden": "b", "equation": "x == 1"})
    (owner,) = projects.owners()["b"]
    assert owner.name == mid[:8]


# --- the one-owner check ----------------------------------------------

def test_a_healthy_tree_has_no_problems():
    _garden("college", {"name": "HW 4", "garden": "hw4"})
    _garden("hw4")
    assert projects.check() == []


def test_two_claimants_are_flagged_and_neither_is_the_owner():
    _garden("college", {"name": "HW 4", "garden": "hw4"})
    _garden("job", {"name": "Report", "garden": "hw4"})
    _garden("hw4")
    (problem,) = projects.check()
    assert problem.kind == projects.DISPUTED
    assert problem.garden == "hw4"
    assert {o.name for o in problem.owners} == {"HW 4", "Report"}
    assert "HW 4 (in college)" in problem.message and "Report (in job)" in problem.message
    assert projects.owner_of("hw4") is None


def test_a_claim_on_a_garden_not_on_disk_is_missing():
    _garden("college", {"name": "HW 4", "garden": "hw4"})
    (problem,) = projects.check()
    assert problem.kind == projects.MISSING
    assert "does not exist" in problem.message


def test_a_mote_claiming_its_own_garden_is_a_loop():
    _garden("a", {"name": "Self", "garden": "a"})
    (problem,) = projects.check()
    assert problem.kind == projects.LOOP
    assert problem.garden == "a"


def test_a_loop_off_a_hubs_second_child_is_still_found():
    # a owns b and c; c owns a. Following only the first edge (a -> b)
    # would never see it.
    _garden("a", {"name": "B", "garden": "b"}, {"name": "C", "garden": "c"})
    _garden("b")
    _garden("c", {"name": "A", "garden": "a"})
    loops = [p for p in projects.check() if p.kind == projects.LOOP]
    assert len(loops) == 1
    assert loops[0].garden == "a"
    assert "a owns c owns a" in loops[0].message


# --- the tree ---------------------------------------------------------

def test_the_tree_nests_projects_under_their_owners_garden():
    _garden("college", {"name": "HW 4", "garden": "hw4"}, {"name": "Lab", "garden": "lab"})
    _garden("hw4", {"name": "Part B", "garden": "hw4-b"})
    _garden("hw4-b")
    _garden("lab")
    _garden("scratch")
    roots = projects.tree()
    assert [n.garden for n in roots] == ["college", "scratch"]
    assert all(n.loose for n in roots)
    college = roots[0]
    assert [c.name for c in college.children] == ["HW 4", "Lab"]
    hw4 = college.children[0]
    assert hw4.garden == "hw4" and not hw4.loose
    assert [c.garden for c in hw4.children] == ["hw4-b"]


def test_every_garden_is_in_the_tree_even_inside_a_loop():
    _garden("a", {"name": "B", "garden": "b"})
    _garden("b", {"name": "A", "garden": "a"})
    _garden("loose")
    drawn = set()

    def walk(node):
        drawn.add(node.garden)
        for child in node.children:
            walk(child)

    for node in projects.tree():
        walk(node)
    assert drawn == {"a", "b", "loose"}


def test_a_disputed_garden_hangs_under_every_claimant():
    _garden("x", {"name": "One", "garden": "p"})
    _garden("y", {"name": "Two", "garden": "p"})
    _garden("p")
    roots = {n.garden: n for n in projects.tree()}
    assert set(roots) == {"x", "y"}
    assert [c.garden for c in roots["x"].children] == ["p"]
    assert [c.garden for c in roots["y"].children] == ["p"]


def test_a_dangling_claim_draws_no_child():
    _garden("college", {"name": "HW 4", "garden": "gone"})
    (root,) = projects.tree()
    assert root.children == ()


def test_no_gardens_is_an_empty_tree():
    assert projects.tree() == []
    assert projects.check() == []


# --- Start project ----------------------------------------------------

def test_start_project_creates_and_links_a_garden_named_after_the_mote(conn):
    mid = motes_repo.create_mote(conn, {"name": "HW 4: Truss!"})
    garden, command = projects.start_project(conn, mid)
    assert garden == "hw-4-truss"
    assert gardens_repo.garden_exists(garden)
    assert motes_repo.get_mote(conn, mid).properties["garden"] == garden
    assert projects.owner_of(garden).mote_id == mid

    command.undo()
    assert "garden" not in motes_repo.get_mote(conn, mid).properties
    # Undo leaves the garden on disk, loose.
    assert gardens_repo.garden_exists(garden)
    command.redo()
    assert motes_repo.get_mote(conn, mid).properties["garden"] == garden


def test_a_derived_name_steps_aside_rather_than_adopting(conn):
    gardens_repo.create_garden("hw4")
    mid = motes_repo.create_mote(conn, {"name": "HW4"})
    garden, _ = projects.start_project(conn, mid)
    assert garden == "hw4-2"


def test_a_name_that_slugs_to_nothing_falls_back_to_the_id(conn):
    mid = motes_repo.create_mote(conn, {"name": "!!!"})
    garden, _ = projects.start_project(conn, mid)
    assert garden == mid[:8]


def test_a_named_garden_adopts_a_loose_one(conn):
    gardens_repo.create_garden("scratch")
    mid = motes_repo.create_mote(conn, {"name": "HW 4"})
    garden, _ = projects.start_project(conn, mid, "scratch")
    assert garden == "scratch"
    assert projects.owner_of("scratch").mote_id == mid


def test_a_named_garden_that_is_owned_is_refused(conn):
    _garden("other", {"name": "Owner", "garden": "taken"})
    gardens_repo.create_garden("taken")
    mid = motes_repo.create_mote(conn, {"name": "HW 4"})
    with pytest.raises(ValueError, match="already owned by Owner"):
        projects.start_project(conn, mid, "taken")
    assert "garden" not in motes_repo.get_mote(conn, mid).properties


def test_a_mote_already_owning_a_garden_is_refused(conn):
    mid = motes_repo.create_mote(conn, {"name": "HW 4", "garden": "hw4"})
    with pytest.raises(ValueError, match="already owns"):
        projects.start_project(conn, mid)


def test_start_project_rejects_a_bad_name_and_an_unknown_mote(conn):
    mid = motes_repo.create_mote(conn, {"name": "HW 4"})
    with pytest.raises(ValueError, match="Invalid garden name"):
        projects.start_project(conn, mid, "has space")
    with pytest.raises(LookupError):
        projects.start_project(conn, "nope")


def test_undo_restores_a_junk_garden_value(conn):
    mid = motes_repo.create_mote(conn, {"name": "HW 4", "garden": ""})
    garden, command = projects.start_project(conn, mid)
    assert motes_repo.get_mote(conn, mid).properties["garden"] == garden
    command.undo()
    assert motes_repo.get_mote(conn, mid).properties["garden"] == ""


def test_stop_owning_clears_the_claim_and_keeps_the_garden(conn):
    mid = motes_repo.create_mote(conn, {"name": "HW 4"})
    garden, _ = projects.start_project(conn, mid)
    claimed, command = projects.stop_owning(conn, mid)
    assert claimed == garden
    assert "garden" not in motes_repo.get_mote(conn, mid).properties
    assert gardens_repo.garden_exists(garden)
    command.undo()
    assert motes_repo.get_mote(conn, mid).properties["garden"] == garden


def test_stop_owning_a_mote_that_owns_nothing_is_a_no_op(conn):
    mid = motes_repo.create_mote(conn, {"name": "HW 4"})
    assert projects.stop_owning(conn, mid) == (None, None)


# --- the cover --------------------------------------------------------

def test_the_cover_reads_name_due_status_author_revision():
    _garden("college", {"name": "HW 4", "garden": "hw4", "due": "2026-10-02",
                        "status": "todo", "author": "E. M.", "revision": 3, "points": 10})
    (owner,) = projects.owners()["hw4"]
    assert owner.cover == projects.Cover(
        name="HW 4", due="2026-10-02", status="todo", author="E. M.", revision="3"
    )
    assert owner.cover.to_json()["revision"] == "3"


def test_a_bare_cover_is_just_a_name():
    (mid,) = _garden("a", {"garden": "b", "status": "  ", "due": True, "author": ["x"]})
    (owner,) = projects.owners()["b"]
    assert owner.cover == projects.Cover(name=mid[:8])


# --- archive ----------------------------------------------------------

def _tree_flags(nodes, out=None):
    out = {} if out is None else out
    for node in nodes:
        out[node.garden] = node.archived
        _tree_flags(node.children, out)
    return out


@pytest.fixture
def semester():
    _garden("college", {"name": "HW 4", "garden": "hw4"}, {"name": "Lab", "garden": "lab"})
    _garden("hw4", {"name": "Part B", "garden": "hw4-b"})
    _garden("hw4-b")
    _garden("lab")
    _garden("scratch")


def test_archiving_an_owned_garden_marks_its_owner_and_its_subtree(semester):
    message = projects.set_archived("hw4", True)
    assert "on its owner, HW 4 (in college)" in message
    (owner,) = projects.owners()["hw4"]
    assert owner.archived and owner.properties["archived"] is True
    assert _tree_flags(projects.tree()) == {
        "college": False, "hw4": True, "hw4-b": True, "lab": False, "scratch": False}
    assert projects.is_archived("hw4-b")
    assert "above 'hw4-b'" in projects.archived_by("hw4-b")


def test_a_loose_garden_is_archived_by_a_marker_inside_it(semester):
    assert "marker mote" in projects.set_archived("scratch", True)
    assert projects.is_archived("scratch")
    assert projects.set_archived("scratch", False).startswith("un-archived")
    assert not projects.is_archived("scratch")
    conn = db.get_connection("scratch")
    try:
        assert motes_repo.list_motes(conn) == []  # the marker we made is gone
    finally:
        conn.close()


def test_a_hub_marker_archives_every_project_beneath_it(semester):
    projects.set_archived("college", True)
    assert all(_tree_flags(projects.tree())[g] for g in ("college", "hw4", "hw4-b", "lab"))
    assert not projects.is_archived("scratch")


def test_ensure_writable_refuses_an_archived_garden_and_names_why(semester):
    projects.ensure_writable("hw4")
    projects.set_archived("hw4", True)
    with pytest.raises(projects.ArchivedGarden, match="HW 4 \\(in college\\) archives"):
        projects.ensure_writable("hw4-b")


def test_unarchiving_beneath_an_archived_ancestor_is_refused(semester):
    projects.set_archived("hw4", True)
    with pytest.raises(projects.ArchivedGarden, match="still archived from above"):
        projects.set_archived("hw4-b", False)
    projects.set_archived("hw4", False)
    assert not projects.is_archived("hw4-b")
    assert "archived" not in projects.owners()["hw4"][0].properties


def test_archiving_twice_and_unarchiving_what_is_not_archived_say_so(semester):
    projects.set_archived("lab", True)
    assert "already archived" in projects.set_archived("lab", True)
    assert "was not archived" in projects.set_archived("scratch", False)
    with pytest.raises(LookupError):
        projects.set_archived("nope", True)


def test_a_marked_owner_does_not_archive_its_own_garden(semester):
    # The owner lives in `college`; its flag archives `hw4`, not `college`.
    projects.set_archived("hw4", True)
    assert not projects.is_archived("college")


def test_a_hand_written_marker_keeps_its_mote_on_unarchive():
    (mid,) = _garden("notes", {"name": "Read me", "archived": "true", "body": "keep"})
    assert projects.is_archived("notes")
    projects.set_archived("notes", False)
    conn = db.get_connection("notes")
    try:
        props = motes_repo.get_mote(conn, mid).properties
    finally:
        conn.close()
    assert props == {"name": "Read me", "body": "keep"}

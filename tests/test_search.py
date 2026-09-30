import core.db as db
from core import gardens as gardens_repo, motes as motes_repo, projects, search


def _garden(name: str, *motes: dict) -> list[str]:
    gardens_repo.create_garden(name)
    conn = db.get_connection(name)
    try:
        return [motes_repo.create_mote(conn, props) for props in motes]
    finally:
        conn.close()


def test_search_finds_matches_in_every_garden_with_their_project():
    _garden("college", {"name": "HW 4", "garden": "hw4"}, {"name": "truss notes"})
    (bar,) = _garden("hw4", {"name": "truss bar", "E": 200})
    _garden("scratch", {"name": "unrelated"})
    results = search.search_all("truss")
    assert [(h.garden, h.name) for h in results.hits] == [
        ("college", "truss notes"), ("hw4", "truss bar")]
    hw4 = results.hits[1]
    assert hw4.mote_id == bar and hw4.project == "HW 4" and hw4.owner.garden == "college"
    assert results.hits[0].project == "college"  # a loose garden is its own project


def test_key_value_queries_work_across_gardens():
    _garden("a", {"name": "x", "status": "todo"})
    _garden("b", {"name": "y", "status": "done"})
    assert [h.garden for h in search.search_all("status = done").hits] == ["b"]


def test_archived_gardens_are_searched_last_and_say_so():
    _garden("a-old", {"name": "beam"})
    _garden("b-new", {"name": "beam"})
    projects.set_archived("a-old", True)
    hits = search.search_all("beam").hits
    assert [(h.garden, h.archived) for h in hits] == [("b-new", False), ("a-old", True)]
    assert [h.garden for h in search.search_all("beam", include_archived=False).hits] == ["b-new"]


def test_a_garden_with_more_hits_than_asked_is_named_as_truncated():
    _garden("big", *({"name": f"beam {n}"} for n in range(5)))
    results = search.search_all("beam", per_garden=3)
    assert len(results.hits) == 3 and results.truncated == ("big",)


def test_a_blank_query_is_no_search():
    _garden("a", {"name": "x"})
    assert search.search_all("  ") == search.Results()

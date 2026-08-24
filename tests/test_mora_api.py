"""Mora's JS bridge.

What earns a test here is behavior this layer adds -- which note it opens
when asked for none, that saving touches only the note, that a step is
clamped, and the payload shape the frontend is written against. A method
that only forwards to a repository is already covered by that
repository's tests; asserting it again here would only prove Python can
call a function. Same line ui/api.py's own tests draw.
"""
import pytest

from core.gardens import create_garden
from core.modoc import MODOC_KEY, read_source
from core.motes import create_mote, get_mote
from core.relationships import create_relationship
from mora.api import MAX_STEP, Api
from mora.app import DEFAULT_HTTP_PORT, choose_port


@pytest.fixture
def api():
    create_garden("test")
    instance = Api("test")
    yield instance
    instance.shutdown()


@pytest.fixture
def project(api):
    """A project of two tasks, one of which a rule advances, so both a
    plain and a projected render have something to say."""
    conn = api._conn
    species = create_mote(conn, {"name": "Task", "freedoms": "progress"})
    rule = create_mote(conn, {"name": "Grow", "equation": "progress == prev(progress) + 10"})
    create_relationship(conn, rule, species, "applies_to")

    project_id = create_mote(conn, {"name": "Atlas", "due": "2026-09-30", "status": "active"})
    for index in range(2):
        task = create_mote(conn, {"name": f"Task {index}", "progress": 0, "status": "todo"})
        create_relationship(conn, task, species, "instance_of")
        create_relationship(conn, project_id, task, "contains")
    return project_id


# ---- opening ----

def test_open_note_prefers_a_mote_that_already_has_one(api, project):
    api.save_note(project, "\\section{Atlas}")
    other = create_mote(api._conn, {"name": "Later"})
    assert api.open_note()["noteId"] == project
    assert other


def test_open_note_falls_back_to_the_most_recent_mote(api, project):
    latest = create_mote(api._conn, {"name": "Newest"})
    assert api.open_note()["noteId"] == latest


def test_open_note_on_a_mote_with_no_note_is_an_empty_page(api, project):
    opened = api.open_note(project)
    assert opened == {"noteId": project, "source": "", "name": "Atlas"}


def test_open_note_falls_back_when_the_requested_mote_is_gone(api, project):
    assert api.open_note("no-such-mote")["noteId"] is not None


def test_the_command_line_note_is_what_opens_first(api, project):
    api._initial_note = project
    assert api.open_note()["noteId"] == project


def test_open_note_in_an_empty_garden_returns_no_note(api):
    assert api.open_note()["noteId"] is None


def test_list_notes_is_only_the_motes_carrying_one(api, project):
    api.save_note(project, "\\section{Atlas}")
    assert [n["id"] for n in api.list_notes()] == [project]


# ---- saving ----

def test_save_note_writes_only_the_modoc_property(api, project):
    api.save_note(project, "\\section{Atlas}")
    assert get_mote(api._conn, project).properties == {
        "name": "Atlas", "due": "2026-09-30", "status": "active", MODOC_KEY: "\\section{Atlas}",
    }


def test_save_note_refuses_a_mote_that_does_not_exist(api):
    with pytest.raises(LookupError):
        api.save_note("nope", "text")


# ---- rendering ----

def test_render_resolves_the_live_source_not_the_saved_one(api, project):
    api.save_note(project, "saved text")
    payload = api.render(project, "Due \\val{due}.", 0)
    assert read_source(api._conn, project) == "saved text"
    assert payload["blocks"][0]["content"][1]["text"] == "2026-09-30"


def test_render_carries_the_counts_the_status_bar_shows(api, project):
    payload = api.render(project, "One \\val{due} and \\mote{Task 0}.\nSecond line.", 0)
    assert payload["stats"] == {"words": 6, "links": 1, "values": 1, "lines": 2}


def test_render_marks_a_stepped_result_as_projected(api, project):
    assert api.render(project, "x", 0)["projected"] is False
    assert api.render(project, "x", 2)["projected"] is True


def test_render_clamps_a_step_out_of_range(api, project):
    assert api.render(project, "x", -5)["step"] == 0
    assert api.render(project, "x", MAX_STEP + 100)["step"] == MAX_STEP


def test_render_colours_a_link_by_the_species_the_canvas_uses(api, project):
    link = api.render(project, "\\mote{Task 0}", 0)["blocks"][0]["content"][0]
    assert link["speciesId"] is not None
    assert link["kinds"] == []


def test_render_reports_problems_rather_than_failing(api, project):
    payload = api.render(project, "\\mote{Nowhere} \\bogus", 0)
    assert sorted(d["kind"] for d in payload["diagnostics"]) == [
        "unknown_command", "unresolved_link",
    ]


def test_render_counts_references_for_the_linked_in_text_list(api, project):
    payload = api.render(project, "\\mote{Task 0} and \\mote{Task 0} and \\mote{Task 1}", 0)
    assert [(e["name"], e["count"]) for e in payload["linked"]] == [("Task 0", 2), ("Task 1", 1)]


# ---- the data panel ----

def test_focus_returns_relationships_with_their_direction(api, project):
    panel = api.focus(project)
    assert panel["name"] == "Atlas"
    assert {r["other"]["name"] for r in panel["relationships"]} == {"Task 0", "Task 1"}
    assert all(r["outgoing"] for r in panel["relationships"])


def test_focus_names_the_species_so_the_panel_can_say_what_a_mote_is(api, project):
    """Carrying a note is not a kind (core/kinds.py), so a note mote has
    no kind to label it -- its species is what "what is this?" wants."""
    task = next(r["other"]["id"] for r in api.focus(project)["relationships"])
    panel = api.focus(task)
    assert panel["kinds"] == []
    assert panel["speciesName"] == "Task"


def test_focus_on_a_missing_mote_returns_nothing(api):
    assert api.focus("nope") is None


def test_search_backs_the_link_picker_with_ids_to_insert(api, project):
    found = api.search("Task 0")
    assert [f["name"] for f in found] == ["Task 0"]
    assert found[0]["id"]


# ---- applying ----

def test_apply_step_writes_the_projection_to_the_graph(api, project):
    api.apply_step(3)
    tasks = [m for m in [get_mote(api._conn, r["other"]["id"]) for r in api.focus(project)["relationships"]]]
    assert {t.properties["progress"] for t in tasks} == {30}


def test_apply_step_refuses_t_zero(api, project):
    with pytest.raises(ValueError):
        api.apply_step(0)


# ---- gardens ----

def test_switch_garden_moves_the_connection(api, project):
    create_garden("other")
    assert api.switch_garden("other") == {"garden": "other"}
    assert api.current_garden() == "other"
    assert api.list_notes() == []


# ---- the window's own port ----
#
# pywebview pins its server to 42001 whenever private_mode is off and no
# port is given, and the graph window takes that one. Two apps wanting it
# is not a near miss: the loser fails to bind but still points its window
# at that address, so it loads the winner's page against its own bridge.
# That shipped once, as a window titled Mora showing the MonOSS graph.

def test_mora_does_not_ask_for_the_graph_windows_port():
    assert DEFAULT_HTTP_PORT != 42001


def test_the_preferred_port_is_used_when_it_is_free():
    port = _a_free_port()
    assert choose_port(port) == port


def test_a_taken_port_falls_back_instead_of_colliding():
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as held:
        held.bind(("127.0.0.1", 0))
        held.listen(1)
        taken = held.getsockname()[1]
        chosen = choose_port(taken)
        assert chosen != taken
        assert 1024 <= chosen <= 65535


def _a_free_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]

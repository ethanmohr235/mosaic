"""Mora's JS bridge.

What earns a test here is behavior this layer adds -- which note it opens
when asked for none, that saving touches only the note, and the payload
shape the frontend is written against. A method
that only forwards to a repository is already covered by that
repository's tests; asserting it again here would only prove Python can
call a function. Same line monarch/api.py's own tests draw.
"""
import pytest

from core.db import get_connection, initialize_database
from core.gardens import create_garden, set_sync_dir
from core.modoc import MODOC_KEY, read_source
from core.motes import create_mote, get_mote, update_mote
from core.relationships import create_relationship
from core.species import set_species
from core.sweep import PLOT_KEY
from core.sync_remote import has_history as sync_has_history
from mora.api import Api
from mora.app import DEFAULT_HTTP_PORT
from window_launcher import choose_port


@pytest.fixture
def api():
    create_garden("test")
    instance = Api("test")
    yield instance
    instance.shutdown()


@pytest.fixture
def project(api):
    """A project of two tasks with a rule over them, so a render has
    something solved to say as well as something stored.

    The rule is written against `t` rather than `prev(progress)` so that
    it can be *swept*: a sweep has no previous tick to read, and one rule
    that cannot solve stops every solve in the garden -- including the
    saved views the figures below draw."""
    conn = api._conn
    species = create_mote(conn, {"name": "Task", "freedoms": "progress"})
    rule = create_mote(conn, {"name": "Grow", "equation": "progress == start + 10 * (t + 1)"})
    create_relationship(conn, rule, species, "applies_to")

    project_id = create_mote(conn, {"name": "Atlas", "due": "2026-09-30", "status": "active"})
    for index in range(2):
        task = create_mote(
            conn, {"name": f"Task {index}", "progress": 0, "start": 0, "status": "todo"}
        )
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


# ---- creating ----

def test_create_note_opens_on_an_empty_page(api):
    created = api.create_note()
    assert created["source"] == ""
    assert get_mote(api._conn, created["noteId"]).properties == {MODOC_KEY: ""}


def test_create_note_takes_the_given_name(api):
    created = api.create_note("Field notes")
    assert created["name"] == "Field notes"
    assert get_mote(api._conn, created["noteId"]).properties["name"] == "Field notes"


def test_create_note_without_a_name_falls_back_to_the_id_prefix(api):
    created = api.create_note()
    assert created["name"] == created["noteId"][:8]


def test_creating_a_note_can_be_undone(api):
    created = api.create_note("Field notes")
    assert api.undo()["undone"] is not None
    assert get_mote(api._conn, created["noteId"]) is None


def test_a_created_note_shows_up_in_the_switcher(api, project):
    api.save_note(project, "\\section{Atlas}")
    created = api.create_note("Field notes")
    assert {n["id"] for n in api.list_notes()} == {project, created["noteId"]}


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
    payload = api.render(project, "Due \\val{due}.")
    assert read_source(api._conn, project) == "saved text"
    assert payload["blocks"][0]["content"][1]["text"] == "2026-09-30"


def test_render_carries_the_counts_the_status_bar_shows(api, project):
    payload = api.render(project, "One \\val{due} and \\mote{Task 0}.\nSecond line.")
    assert payload["stats"] == {"words": 6, "links": 1, "values": 1, "lines": 2}


def test_render_has_one_reading_and_no_step_to_ask_for(api, project):
    """A note says what the garden is. Moving a garden through time is
    Motion's window, and Mora used to carry a `t+N` stepper and an Apply
    button that committed the projection -- a note editor holding a lever
    that belonged somewhere else."""
    assert "step" not in api.render(project, "x")
    assert "projected" not in api.render(project, "x")
    assert not hasattr(api, "apply_step")


def test_render_colours_a_link_by_the_species_the_canvas_uses(api, project):
    link = api.render(project, "\\mote{Task 0}")["blocks"][0]["content"][0]
    assert link["speciesId"] is not None
    assert link["kinds"] == []


def test_render_reports_problems_rather_than_failing(api, project):
    payload = api.render(project, "\\mote{Nowhere} \\bogus")
    assert sorted(d["kind"] for d in payload["diagnostics"]) == [
        "unknown_command", "unresolved_link",
    ]


def test_render_counts_references_for_the_linked_in_text_list(api, project):
    payload = api.render(project, "\\mote{Task 0} and \\mote{Task 0} and \\mote{Task 1}")
    assert [(e["name"], e["count"]) for e in payload["linked"]] == [("Task 0", 2), ("Task 1", 1)]


# ---- figures ----
#
# What a `\graph{...}` draws is tests/test_plotview.py's subject. What is
# this layer's is the memo: Mora re-resolves the whole note on every
# pause in typing, and a sweep is the one thing in a note expensive
# enough that doing it again for an unchanged answer would be felt.

@pytest.fixture
def view(api):
    """A crew whose capacity follows its size, and the Moxie view saved
    over it -- the same shape moxie/api.py::save_plot writes."""
    conn = api._conn
    species = create_mote(conn, {"name": "Crew", "freedoms": "capacity"})
    rule = create_mote(conn, {"name": "Throughput", "equation": "capacity == team * 12"})
    create_relationship(conn, rule, species, "applies_to")
    crew = create_mote(conn, {"name": "Platform crew", "team": 4})
    set_species(conn, crew, species)
    plot = create_mote(conn, {"name": "Capacity", PLOT_KEY: {
        "mode": "sweep",
        "x": {"mote": crew, "key": "team", "lo": 1.0, "hi": 8.0, "steps": 200},
        "series": [{"mote": crew, "key": "capacity", "label": "capacity"}],
    }})
    return {"plot": plot, "rule": rule, "crew": crew}


def _last_y(payload):
    figure = next(b for b in payload["blocks"] if b["type"] == "graph")
    assert figure["error"] is None, figure["error"]
    return [p for p in figure["series"][0]["points"] if p][-1][1]


def test_render_draws_the_saved_view_a_graph_names(api, project, view):
    assert _last_y(api.render(project, "\\graph{Capacity}")) == pytest.approx(8 * 12)


def test_a_figure_is_drawn_once_while_the_garden_holds_still(api, project, view):
    api.render(project, "\\graph{Capacity}")
    drawn = dict(api._figures._drawn)
    api.render(project, "\\graph{Capacity} and more prose")
    assert api._figures._drawn == drawn
    assert all(before is after for before, after in
               zip(drawn.values(), api._figures._drawn.values()))


def test_a_write_anywhere_in_the_garden_redraws_the_figure(api, project, view):
    """Invalidation is the garden's own last write, not a list of hooks:
    a rule edited in another window has to move the curve here too."""
    before = _last_y(api.render(project, "\\graph{Capacity}"))
    update_mote(api._conn, view["rule"], {"equation": "capacity == team * 24"})
    assert _last_y(api.render(project, "\\graph{Capacity}")) == pytest.approx(before * 2)


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


# ---- gardens ----

def test_switch_garden_moves_the_connection(api, project):
    create_garden("other")
    assert api.switch_garden("other") == {"garden": "other"}
    assert api.current_garden() == "other"
    assert api.list_notes() == []


# ---- app switcher (⌘J) ----
#
# The command-building itself is api_common.open_app's own test (see
# tests/test_api_common.py); what's worth checking here is only that
# this app's method actually delegates to it with this app's garden.

def test_open_app_delegates_to_api_common(api, monkeypatch):
    import api_common

    calls = []
    monkeypatch.setattr(
        api_common, "popen_detached", lambda args, **kwargs: calls.append(args)
    )
    assert api.open_app("monastery") == {"app": "monastery", "garden": "test", "focus": None}
    assert calls[0][2:] == ["--garden", "test"]


# ---- best-effort sync ----
#
# run_mora.py is its own launch path, not only ever opened from the graph
# window (monarch/app.py) -- so a garden pointed at a sync server must converge
# from a Mora-only session too, the same way monarch/api.py::Api does on open,
# switch and close.

def test_opening_mora_pushes_pending_local_changes(sync_dir):
    create_garden("synced")
    conn = get_connection("synced")
    initialize_database(conn)
    create_mote(conn, {"name": "written before Mora opened"})
    conn.close()

    set_sync_dir("synced", sync_dir)

    instance = Api("synced")
    instance.shutdown()

    assert sync_has_history(sync_dir, "synced")


def test_switching_garden_pushes_the_new_gardens_pending_changes(sync_dir):
    create_garden("plain")
    instance = Api("plain")

    create_garden("synced")
    conn = get_connection("synced")
    initialize_database(conn)
    create_mote(conn, {"name": "written before switching in"})
    conn.close()

    set_sync_dir("synced", sync_dir)

    instance.switch_garden("synced")
    instance.shutdown()

    assert sync_has_history(sync_dir, "synced")


def test_opening_mora_on_an_unsynced_garden_does_nothing(api):
    # No sync server configured for "test" (the `api` fixture's garden) --
    # must not raise or create one implicitly.
    assert api.current_garden() == "test"


# ---- manual refresh ----

def test_refresh_skips_sync_when_not_configured(api):
    assert api.refresh() == {"synced": False}


def test_refresh_syncs_when_configured(api, sync_dir):
    set_sync_dir("test", sync_dir)
    create_mote(api._conn, {"name": "Alice"})
    result = api.refresh()
    assert result["synced"] is True
    assert result["push"]["pushed"] >= 1


# ---- the window's own port ----
#
# pywebview pins its server to 42001 whenever private_mode is off and no
# port is given, and Monarch takes that one. Two apps wanting it
# is not a near miss: the loser fails to bind but still points its window
# at that address, so it loads the winner's page against its own bridge.
# That shipped once, as a window titled Mora showing the Monarch graph.

def test_mora_does_not_ask_for_the_graph_windows_port():
    assert DEFAULT_HTTP_PORT != 42001


def test_the_preferred_port_is_used_when_it_is_free():
    port = _a_free_port()
    assert choose_port("Mora", port) == port


def test_a_taken_port_falls_back_instead_of_colliding():
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as held:
        held.bind(("127.0.0.1", 0))
        held.listen(1)
        taken = held.getsockname()[1]
        chosen = choose_port("Mora", taken)
        assert chosen != taken
        assert 1024 <= chosen <= 65535


def _a_free_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]

import pytest

from cli.main import (
    _handle_folder,
    _handle_garden,
    _handle_library,
    _handle_model,
    _handle_modoc,
    _handle_mote,
    _handle_portal,
    _handle_simulate,
    _handle_species,
    build_parser,
)
from core.gardens import garden_exists
from core.motes import create_mote, list_motes
from core.relationships import create_relationship
from core.rules import APPLIES_TO
from core.species import set_species

PARSER = build_parser()


def parse(argv: list[str]):
    return PARSER.parse_args(argv)


def _wire_ideal_air(conn):
    """An 'Air' mote whose uses_model relationship resolves to an Ideal
    Gas property model. Returns (air_id, model_id) -- mirrors the fixture
    tests/test_simulation.py uses for the same property-model setup."""
    air = create_mote(conn, {"name": "Air"})
    model = create_mote(
        conn, {"name": "Ideal Air", "provider": "ideal_gas", "configuration": {"gas_constant": 287.05}}
    )
    create_relationship(conn, air, model, "uses_model")
    return air, model


# ---- model list/show/capabilities ----

def test_model_list_shows_registered_model_motes(conn, capsys):
    _, model = _wire_ideal_air(conn)
    _handle_model(conn, parse(["model", "list"]))
    out = capsys.readouterr().out
    assert model in out
    assert "Ideal Air" in out
    assert "provider=ideal_gas" in out


def test_model_list_empty_when_no_models(conn, capsys):
    _handle_model(conn, parse(["model", "list"]))
    assert capsys.readouterr().out == ""


def test_model_show_prints_provider_configuration_and_capabilities(conn, capsys):
    _, model = _wire_ideal_air(conn)
    _handle_model(conn, parse(["model", "show", "--id", model]))
    out = capsys.readouterr().out
    assert "Model: Ideal Air" in out
    assert "Provider: ideal_gas" in out
    assert "gas_constant" in out
    assert "Capabilities:" in out
    assert "pressure(density, temperature) -> pressure" in out


def test_model_capabilities_omits_the_header_block(capsys, conn):
    _, model = _wire_ideal_air(conn)
    _handle_model(conn, parse(["model", "capabilities", "--id", model]))
    out = capsys.readouterr().out
    assert "Model:" not in out
    assert "Provider:" not in out
    assert "pressure(density, temperature) -> pressure" in out


def test_model_show_reports_missing_provider_without_raising(conn, capsys):
    bad_model = create_mote(conn, {"name": "Bad Model"})
    _handle_model(conn, parse(["model", "show", "--id", bad_model]))
    out = capsys.readouterr().out
    assert "Error:" in out
    assert "provider" in out


def test_model_show_reports_no_mote_found(conn, capsys):
    _handle_model(conn, parse(["model", "show", "--id", "nonexistent"]))
    assert "No mote found." in capsys.readouterr().out


def test_mote_get_prints_models_line_for_a_mote_with_uses_model(conn, capsys):
    air, _ = _wire_ideal_air(conn)
    _handle_mote(conn, parse(["mote", "get", "--id", air]))
    out = capsys.readouterr().out
    assert "Models: Ideal Air" in out


def test_mote_get_omits_models_line_without_uses_model(conn, capsys):
    plain = create_mote(conn, {"name": "Plain"})
    _handle_mote(conn, parse(["mote", "get", "--id", plain]))
    out = capsys.readouterr().out
    assert "Models:" not in out


# ---- simulate ----

def _wire_solvable_container(conn):
    """A Container species with a single ideal-gas-law rule and one
    instance -- the smallest garden run_simulation() can solve. Returns
    the instance's mote id."""
    air, _ = _wire_ideal_air(conn)
    container = create_mote(conn, {"name": "Container", "unknowns": "pressure"})
    rule = create_mote(
        conn, {"name": "Ideal gas law", "equation": "pressure == property('gas', 'pressure', density, temperature)"}
    )
    create_relationship(conn, rule, container, APPLIES_TO)
    c1 = create_mote(conn, {"name": "C1", "density": 1.2, "temperature": 293.15})
    set_species(conn, c1, container)
    create_relationship(conn, c1, air, "gas")
    return c1


def test_simulate_bare_defaults_to_run_and_prints_changes(conn, capsys):
    c1 = _wire_solvable_container(conn)
    _handle_simulate(conn, parse(["simulate"]))
    out = capsys.readouterr().out
    assert "Solved 1 value" in out
    assert c1[:8] in out
    assert "pressure:" in out


def test_simulate_run_is_equivalent_to_bare_simulate(conn, capsys):
    _wire_solvable_container(conn)
    _handle_simulate(conn, parse(["simulate", "run"]))
    assert "Solved 1 value" in capsys.readouterr().out


def test_simulate_reports_failure_without_raising(conn, capsys):
    container = create_mote(conn, {"name": "Container", "unknowns": "pressure"})
    rule = create_mote(conn, {"name": "Bad rule", "equation": "pressure == missing_value"})
    create_relationship(conn, rule, container, APPLIES_TO)
    set_species(conn, create_mote(conn, {"name": "C1"}), container)

    _handle_simulate(conn, parse(["simulate"]))
    assert "Simulation failed:" in capsys.readouterr().out


def test_simulate_history_lists_ok_and_failed_runs(conn, capsys):
    _wire_solvable_container(conn)
    _handle_simulate(conn, parse(["simulate"]))  # ok run
    capsys.readouterr()

    container = create_mote(conn, {"name": "Bad Container", "unknowns": "pressure"})
    rule = create_mote(conn, {"name": "Bad rule", "equation": "pressure == missing_value"})
    create_relationship(conn, rule, container, APPLIES_TO)
    set_species(conn, create_mote(conn, {"name": "C2"}), container)
    _handle_simulate(conn, parse(["simulate"]))  # failed run
    capsys.readouterr()

    _handle_simulate(conn, parse(["simulate", "history"]))
    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if line.startswith("[")]
    assert len(lines) == 2
    # Most-recent-first: the failed run (run second) is listed before the ok one.
    assert "error" in lines[0] and "missing_value" in lines[0]
    assert "ok (applied)" in lines[1] and "1 change" in lines[1]


def test_simulate_history_respects_limit(conn, capsys):
    _wire_solvable_container(conn)
    _handle_simulate(conn, parse(["simulate"]))
    _handle_simulate(conn, parse(["simulate"]))
    capsys.readouterr()

    _handle_simulate(conn, parse(["simulate", "history", "--limit", "1"]))
    out = capsys.readouterr().out
    assert len(out.strip().splitlines()) == 1


def test_simulate_show_prints_run_detail_and_changes(conn, capsys):
    c1 = _wire_solvable_container(conn)
    _handle_simulate(conn, parse(["simulate"]))
    capsys.readouterr()

    _handle_simulate(conn, parse(["simulate", "history"]))
    history_line = capsys.readouterr().out.strip().splitlines()[0]
    run_id = history_line.split("]")[0].lstrip("[")

    _handle_simulate(conn, parse(["simulate", "show", "--id", run_id]))
    out = capsys.readouterr().out
    assert f"Run {run_id}" in out
    assert "status=ok" in out
    assert c1[:8] in out
    assert "pressure:" in out


def test_simulate_show_reports_no_run_found(conn, capsys):
    _handle_simulate(conn, parse(["simulate", "show", "--id", "nonexistent"]))
    assert "No run found." in capsys.readouterr().out


# ---- garden delete/restore ----

def test_garden_delete_and_restore_round_trip(capsys):
    _handle_garden(parse(["garden", "create", "throwaway"]))
    assert garden_exists("throwaway")

    _handle_garden(parse(["garden", "delete", "throwaway"]))
    assert not garden_exists("throwaway")
    assert "throwaway" in capsys.readouterr().out

    _handle_garden(parse(["garden", "restore", "throwaway"]))
    assert garden_exists("throwaway")


def test_garden_delete_missing_raises():
    with pytest.raises(LookupError):
        _handle_garden(parse(["garden", "delete", "no-such-garden"]))


def test_garden_restore_without_a_prior_delete_raises():
    with pytest.raises(LookupError):
        _handle_garden(parse(["garden", "restore", "never-deleted"]))


# ---- species / folders / portals / library / search ----
#
# The charter (docs/architecture.md, "What belongs on which surface")
# gives the CLI everything scriptable. These reached only the app or only
# MCP before.

def test_species_commands_cover_set_template_and_instantiate(conn, capsys):
    species = create_mote(conn, {"name": "Resistor"})
    template = create_mote(conn, {"name": "R", "resistance": 100})

    _handle_species(conn, parse(["species", "template", "--id", template, "--species", species]))
    _handle_species(conn, parse(["species", "new", "--species", species, "--properties", '{"name":"R1"}']))
    capsys.readouterr()

    _handle_species(conn, parse(["species", "instances", "--id", species]))
    out = capsys.readouterr().out
    assert "R1" in out
    assert "'resistance': 100" in out, "a new instance should inherit the template"


def test_species_list_and_of(conn, capsys):
    species = create_mote(conn, {"name": "Person"})
    alice = create_mote(conn, {"name": "Alice"})
    set_species(conn, alice, species)

    _handle_species(conn, parse(["species", "list"]))
    assert species in capsys.readouterr().out

    _handle_species(conn, parse(["species", "of", "--id", alice]))
    assert species in capsys.readouterr().out


def test_folder_commands_file_and_read_back(conn, capsys):
    folder = create_mote(conn, {"name": "Thermodynamics"})
    note = create_mote(conn, {"name": "Entropy"})

    _handle_folder(conn, parse(["folder", "set", "--id", note, "--folder", folder]))
    assert "filed under" in capsys.readouterr().out

    _handle_folder(conn, parse(["folder", "members", "--id", folder]))
    assert note in capsys.readouterr().out

    _handle_folder(conn, parse(["folder", "of", "--id", note]))
    assert folder in capsys.readouterr().out

    _handle_folder(conn, parse(["folder", "list"]))
    assert folder in capsys.readouterr().out


def test_mote_search_by_text_and_key_value(conn, capsys):
    create_mote(conn, {"name": "Alice", "role": "engineer"})
    create_mote(conn, {"name": "Bob", "role": "doctor"})

    _handle_mote(conn, parse(["mote", "search", "engineer"]))
    out = capsys.readouterr().out
    assert "Alice" in out and "Bob" not in out

    _handle_mote(conn, parse(["mote", "search", "role = doctor"]))
    out = capsys.readouterr().out
    assert "Bob" in out and "Alice" not in out


def test_library_save_list_and_use(conn, capsys):
    mote = create_mote(conn, {"name": "Standup", "duration": 15})

    _handle_library(conn, parse(["library", "save", "--id", mote]))
    entry_id = capsys.readouterr().out.split("id=")[-1].strip()

    _handle_library(conn, parse(["library", "list"]))
    assert "Standup" in capsys.readouterr().out

    _handle_library(conn, parse(["library", "use", "--id", entry_id, "--properties", '{"name":"Monday"}']))
    capsys.readouterr()
    copies = [m for m in list_motes(conn) if m.properties.get("name") == "Monday"]
    assert copies and copies[0].properties["duration"] == 15


def test_portal_create_and_resolve(conn, capsys):
    from core.db import get_connection, initialize_database
    from core.gardens import create_garden

    create_garden("elsewhere")
    other = get_connection("elsewhere")
    try:
        initialize_database(other)
        target = create_mote(other, {"name": "Reactor"})
    finally:
        other.close()

    _handle_portal(conn, parse(["portal", "create", "--to-garden", "elsewhere", "--to-mote", target]))
    portal_id = capsys.readouterr().out.split("id=")[-1].split()[0]

    _handle_portal(conn, parse(["portal", "resolve", "--id", portal_id]))
    out = capsys.readouterr().out
    assert "Garden: elsewhere" in out
    assert "Reactor" in out


# ---- modoc ----

@pytest.fixture
def note(conn):
    """A project of two tasks with a note on it, so every modoc command
    below has both a resolving `\\val` and a broken one to report."""
    project = create_mote(conn, {"name": "Atlas", "due": "2026-09-30"})
    for index in range(2):
        create_relationship(conn, project, create_mote(conn, {"name": f"T{index}"}), "contains")
    create_mote(conn, {"name": "unused"})
    from core.modoc import write_source

    write_source(conn, project, (
        "\\section{Atlas}\n\n"
        "Due \\val{due}, \\val{count(targets('contains'))} tasks, "
        "see \\mote{T0} and \\mote{Nowhere}. \\val{nonesuch}\n"
    ))
    return project


def test_modoc_render_resolves_values_and_links(conn, capsys, note):
    _handle_modoc(conn, parse(["modoc", "render", note]))
    out = capsys.readouterr().out
    assert "Atlas\n=====" in out
    assert "Due 2026-09-30, 2 tasks" in out
    assert "see T0 and Nowhere" in out


def test_modoc_check_lists_only_what_did_not_resolve(conn, capsys, note):
    _handle_modoc(conn, parse(["modoc", "check", note]))
    out = capsys.readouterr().out
    assert "Nowhere" in out and "nonesuch" in out
    assert "2026-09-30" not in out


def test_modoc_check_says_so_when_a_note_is_clean(conn, capsys):
    from core.modoc import write_source

    mote = create_mote(conn, {"name": "A", "due": "soon"})
    write_source(conn, mote, "Due \\val{due}.")
    _handle_modoc(conn, parse(["modoc", "check", mote]))
    assert capsys.readouterr().out.strip() == "No problems."


def test_modoc_values_prints_every_binding(conn, capsys, note):
    _handle_modoc(conn, parse(["modoc", "values", note]))
    out = capsys.readouterr().out
    assert "due = 2026-09-30" in out
    assert "count(targets('contains')) = 2" in out
    assert "nonesuch = ?nonesuch" in out


def test_modoc_export_and_import_round_trip(conn, capsys, note, tmp_path):
    from core.modoc import read_source

    path = tmp_path / "note.modoc"
    _handle_modoc(conn, parse(["modoc", "export", note, "--out", str(path)]))
    capsys.readouterr()
    assert path.read_text() == read_source(conn, note)

    _handle_modoc(conn, parse(["modoc", "import", str(path)]))
    new_id = capsys.readouterr().out.split("id=")[-1].strip()
    assert read_source(conn, new_id) == path.read_text()


def test_modoc_import_can_target_an_existing_mote(conn, capsys, note, tmp_path):
    from core.modoc import read_source

    path = tmp_path / "note.modoc"
    path.write_text("\\section{Replaced}")
    _handle_modoc(conn, parse(["modoc", "import", str(path), "--mote", note]))
    assert read_source(conn, note) == "\\section{Replaced}"


def test_modoc_render_at_a_step_never_writes(conn, capsys):
    from core.modoc import write_source
    from core.motes import get_mote

    species = create_mote(conn, {"name": "Task", "unknowns": "progress"})
    rule = create_mote(conn, {"name": "Grow", "equation": "progress == prev(progress) + 10"})
    create_relationship(conn, rule, species, APPLIES_TO)
    task = create_mote(conn, {"name": "T", "progress": 5})
    set_species(conn, task, species)
    write_source(conn, task, "\\val{progress}")

    _handle_modoc(conn, parse(["modoc", "render", task, "--step", "4"]))
    assert "45" in capsys.readouterr().out
    assert get_mote(conn, task).properties["progress"] == 5


def test_modoc_rejects_an_unknown_action(conn, capsys, note):
    args = parse(["modoc", "render", note])
    args.action = "frobnicate"
    _handle_modoc(conn, args)
    assert "Specify a modoc action" in capsys.readouterr().out

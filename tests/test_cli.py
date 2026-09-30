import io

import pytest

from cli.main import (
    _handle_garden,
    _handle_health,
    _handle_incidence,
    _handle_model,
    _handle_modoc,
    _handle_mote,
    _handle_roles,
    _handle_scenario,
    _handle_simulate,
    _handle_species,
    _handle_unit,
    _handle_why,
    _handle_worksheet,
    build_parser,
)
from core.gardens import garden_exists
from core.motes import create_mote, get_mote
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
    container = create_mote(conn, {"name": "Container", "freedoms": "pressure"})
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
    container = create_mote(conn, {"name": "Container", "freedoms": "pressure"})
    rule = create_mote(conn, {"name": "Bad rule", "equation": "pressure == missing_value"})
    create_relationship(conn, rule, container, APPLIES_TO)
    set_species(conn, create_mote(conn, {"name": "C1"}), container)

    _handle_simulate(conn, parse(["simulate"]))
    assert "Simulation failed:" in capsys.readouterr().out


def test_simulate_keeps_no_run_history(conn, capsys):
    """`simulate` only solves; there is no log of past runs to list."""
    with pytest.raises(SystemExit):
        parse(["simulate", "history"])
    capsys.readouterr()


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


# ---- species / search ----
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


def test_mote_search_by_text_and_key_value(conn, capsys):
    create_mote(conn, {"name": "Alice", "role": "engineer"})
    create_mote(conn, {"name": "Bob", "role": "doctor"})

    _handle_mote(conn, parse(["mote", "search", "engineer"]))
    out = capsys.readouterr().out
    assert "Alice" in out and "Bob" not in out

    _handle_mote(conn, parse(["mote", "search", "role = doctor"]))
    out = capsys.readouterr().out
    assert "Bob" in out and "Alice" not in out


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


def test_modoc_render_reads_the_garden_and_never_writes(conn, capsys):
    """A note has one reading: the garden as it stands. `--step` used to
    render it against a simulation preview N steps out; that is
    `preview_simulation`'s question and Motion's window, not a note's."""
    from core.modoc import write_source
    from core.motes import get_mote

    task = create_mote(conn, {"name": "T", "progress": 5})
    write_source(conn, task, "\\val{progress}")

    _handle_modoc(conn, parse(["modoc", "render", task]))
    assert "5" in capsys.readouterr().out
    assert get_mote(conn, task).properties["progress"] == 5

    with pytest.raises(SystemExit):
        parse(["modoc", "render", task, "--step", "4"])


def test_modoc_rejects_an_unknown_action(conn, capsys, note):
    args = parse(["modoc", "render", note])
    args.action = "frobnicate"
    _handle_modoc(conn, args)
    assert "Specify a modoc action" in capsys.readouterr().out

# ---- unit list/convert/check ----

def _resistor_with_units(conn, equation, units):
    """A one-instance species carrying declared units, for exercising the
    dimension check through the CLI."""
    species = create_mote(conn, {"name": "Resistor", "freedoms": "voltage"})
    rule = create_mote(conn, {"name": "Rule", "equation": equation})
    create_relationship(conn, rule, species, APPLIES_TO)
    instance = create_mote(conn, {
        "name": "R1", "current": 3, "resistance": 4.7, "units": units,
    })
    set_species(conn, instance, species)
    return instance


def test_unit_list_groups_units_by_dimension(conn, capsys):
    _handle_unit(conn, parse(["unit", "list"]))
    out = capsys.readouterr().out
    assert "kohm" in out
    assert "degC" in out


def test_unit_convert_prints_the_converted_value(conn, capsys):
    _handle_unit(conn, parse(["unit", "convert", "--value", "1", "--from", "kohm", "--to", "ohm"]))
    assert "1 kohm = 1000 ohm" in capsys.readouterr().out


def test_unit_convert_reports_incompatible_dimensions(conn, capsys):
    _handle_unit(conn, parse(["unit", "convert", "--value", "1", "--from", "ohm", "--to", "s"]))
    assert "different dimensions" in capsys.readouterr().out


def test_unit_convert_reports_an_unknown_unit(conn, capsys):
    _handle_unit(conn, parse(["unit", "convert", "--value", "1", "--from", "furlong", "--to", "m"]))
    assert "furlong" in capsys.readouterr().out


def test_unit_check_reports_a_consistent_garden(conn, capsys):
    _resistor_with_units(
        conn, "voltage == current * resistance",
        {"current": "mA", "resistance": "kohm", "voltage": "V"},
    )
    _handle_unit(conn, parse(["unit", "check"]))
    assert "OK" in capsys.readouterr().out


def test_unit_check_names_the_rule_and_instance_at_fault(conn, capsys):
    _resistor_with_units(
        conn, "voltage == current + resistance",
        {"current": "mA", "resistance": "kohm", "voltage": "V"},
    )
    _handle_unit(conn, parse(["unit", "check"]))
    out = capsys.readouterr().out
    assert "Rule on R1" in out
    assert "different dimensions" in out


def test_unit_check_with_no_rules_says_so(conn, capsys):
    _handle_unit(conn, parse(["unit", "check"]))
    assert "No rules to check." in capsys.readouterr().out


def test_simulate_run_prints_dimension_warnings(conn, capsys):
    _resistor_with_units(
        conn, "voltage == current + resistance",
        {"current": "mA", "resistance": "kohm", "voltage": "V"},
    )
    _handle_simulate(conn, parse(["simulate", "run"]))
    assert "warning:" in capsys.readouterr().out


def test_simulate_run_with_strict_units_fails(conn, capsys):
    _resistor_with_units(
        conn, "voltage == current + resistance",
        {"current": "mA", "resistance": "kohm", "voltage": "V"},
    )
    _handle_simulate(conn, parse(["simulate", "run", "--strict-units"]))
    assert "Simulation failed" in capsys.readouterr().out


### why


def _solved_chain(conn):
    """Two assignment rules in sequence, so every value has a
    substitution derivation to explain. Returns the instance's id."""
    species = create_mote(conn, {"name": "Link", "freedoms": "b,c"})
    for name, equation in (("B rule", "b == a * 2"), ("C rule", "c == b + 1")):
        create_relationship(
            conn, create_mote(conn, {"name": name, "equation": equation}),
            species, APPLIES_TO,
        )
    mote = create_mote(conn, {"name": "L1", "a": 5})
    set_species(conn, mote, species)
    return mote


def test_why_explains_a_solved_value_through_its_chain(conn, capsys):
    mote = _solved_chain(conn)
    _handle_simulate(conn, parse(["simulate", "run"]))
    capsys.readouterr()

    _handle_why(conn, parse(["why", mote, "c"]))
    out = capsys.readouterr().out
    assert "L1.c = 11" in out
    assert "C rule" in out
    assert "B rule" in out          # reached by recursing into `b`
    assert "property L1.a = 5" in out


def test_why_names_motes_rather_than_uuids(conn, capsys):
    mote = _solved_chain(conn)
    _handle_simulate(conn, parse(["simulate", "run"]))
    capsys.readouterr()

    _handle_why(conn, parse(["why", mote, "c"]))
    assert mote[:8] not in capsys.readouterr().out


def test_why_on_an_underived_value_says_so_without_the_unit_note(conn, capsys):
    mote = _solved_chain(conn)
    _handle_simulate(conn, parse(["simulate", "run"]))
    capsys.readouterr()

    _handle_why(conn, parse(["why", mote, "a"]))
    out = capsys.readouterr().out
    assert "not determined" in out
    # The base-unit caveat is about arithmetic that happened; nothing
    # happened here, so printing it would be noise.
    assert "base SI unit" not in out


def test_why_before_any_solve_says_none_is_recorded(conn, capsys):
    mote = _solved_chain(conn)
    _handle_why(conn, parse(["why", mote, "c"]))
    assert "no solve has been recorded" in capsys.readouterr().out


def test_why_takes_no_run_or_step(conn, capsys):
    mote = _solved_chain(conn)
    with pytest.raises(SystemExit):
        parse(["why", mote, "c", "--run", "abc"])
    capsys.readouterr()


def test_why_respects_an_explicit_depth(conn, capsys):
    mote = _solved_chain(conn)
    _handle_simulate(conn, parse(["simulate", "run"]))
    capsys.readouterr()

    _handle_why(conn, parse(["why", mote, "c", "--depth", "1"]))
    out = capsys.readouterr().out
    assert "beyond the requested depth" in out
    assert "B rule" not in out


### impact / requires / producers / consumers


def test_impact_lists_what_could_move(conn, capsys):
    mote = _solved_chain(conn)
    _handle_incidence(conn, parse(["impact", mote, "a"]))
    out = capsys.readouterr().out

    assert "could move 2 values" in out
    assert "1 step away" in out
    assert "2 steps away" in out
    assert "B rule" in out


def test_impact_of_an_unread_value_says_nothing_depends_on_it(conn, capsys):
    mote = _solved_chain(conn)
    _handle_incidence(conn, parse(["impact", mote, "unrelated"]))
    assert "Nothing depends on" in capsys.readouterr().out


def test_requires_puts_missing_prerequisites_first(conn, capsys):
    species = create_mote(conn, {"name": "Link", "freedoms": "b,c"})
    for name, equation in (("B rule", "b == a * 2"), ("C rule", "c == b + 1")):
        create_relationship(
            conn, create_mote(conn, {"name": name, "equation": equation}),
            species, APPLIES_TO,
        )
    mote = create_mote(conn, {"name": "L1"})   # no `a`
    set_species(conn, mote, species)

    _handle_incidence(conn, parse(["requires", mote, "c"]))
    out = capsys.readouterr().out
    assert "needs (1 missing):" in out
    lines = [line.strip() for line in out.splitlines() if line.strip()]
    first_need = next(line for line in lines if line.startswith(("missing", "derived", "stored")))
    assert first_need.startswith("missing")


def test_requires_explains_a_value_that_is_not_free(conn, capsys):
    mote = _solved_chain(conn)
    _handle_incidence(conn, parse(["requires", mote, "a"]))
    out = capsys.readouterr().out

    assert "Nothing can determine" in out
    assert "not among its mote's declared freedoms" in out


def test_producers_explains_an_empty_result_rather_than_printing_nothing(conn, capsys):
    mote = _solved_chain(conn)
    _handle_incidence(conn, parse(["producers", mote, "a"]))
    assert "not free to move" in capsys.readouterr().out


def test_consumers_lists_the_equations_that_read_a_value(conn, capsys):
    mote = _solved_chain(conn)
    _handle_incidence(conn, parse(["consumers", mote, "a"]))
    assert "B rule on L1" in capsys.readouterr().out


def test_the_queries_need_no_simulation_run(conn, capsys):
    """The index is derived from the rules, so it answers before anything
    has ever been solved -- unlike `why`, which reads the last solve."""
    mote = _solved_chain(conn)
    _handle_incidence(conn, parse(["producers", mote, "c"]))
    assert "C rule on L1" in capsys.readouterr().out


### roles


def test_roles_check_reports_a_correctly_wired_assembly(conn, capsys):
    part = create_mote(conn, {"name": "Left", "value": 1})
    whole = create_mote(conn, {"name": "W1", "roles": ["left"]})
    create_relationship(conn, whole, part, "left")

    _handle_roles(conn, parse(["roles", "check"]))
    assert "wired correctly" in capsys.readouterr().out


def test_roles_check_reports_a_missing_part(conn, capsys):
    create_mote(conn, {"name": "W1", "roles": ["left"]})
    _handle_roles(conn, parse(["roles", "check"]))
    out = capsys.readouterr().out

    assert "1 role problem(s):" in out
    assert "'left'" in out


def test_roles_check_is_quiet_without_roles(conn, capsys):
    create_mote(conn, {"name": "Plain"})
    _handle_roles(conn, parse(["roles", "check"]))
    assert "No motes declare roles." in capsys.readouterr().out


def test_roles_check_lists_rules_asserted_about_one_mote(conn, capsys):
    mote = create_mote(conn, {"name": "Solo", "freedoms": "x"})
    create_relationship(
        conn, create_mote(conn, {"name": "One-off", "equation": "x == 1"}), mote, APPLIES_TO
    )

    _handle_roles(conn, parse(["roles", "check"]))
    out = capsys.readouterr().out
    assert "asserted about a single mote" in out
    assert "'One-off' is asserted once, about Solo" in out


### scenarios


def _scenario_garden(conn):
    """A resistor and a 'High Load' scenario doubling its current."""
    species = create_mote(conn, {"name": "Resistor", "freedoms": "voltage"})
    create_relationship(
        conn,
        create_mote(conn, {"name": "Ohm", "equation": "voltage == current * resistance"}),
        species, APPLIES_TO,
    )
    r1 = create_mote(conn, {"name": "R1", "current": 2, "resistance": 3})
    set_species(conn, r1, species)

    override = create_mote(conn, {"current": 10})
    create_relationship(conn, override, r1, "overrides")
    scenario = create_mote(conn, {"name": "High Load"})
    create_relationship(conn, scenario, override, "varies")
    return {"resistor": r1, "scenario": scenario}


def test_scenario_list_shows_what_each_varies(conn, capsys):
    garden = _scenario_garden(conn)
    _handle_scenario(conn, parse(["scenario", "list"]))
    out = capsys.readouterr().out

    assert "High Load" in out
    assert "1 override(s) over 1 mote(s)" in out
    assert garden["scenario"] in out


def test_scenario_list_is_explicit_when_there_are_none(conn, capsys):
    create_mote(conn, {"name": "Plain"})
    _handle_scenario(conn, parse(["scenario", "list"]))
    assert "No scenarios in this garden." in capsys.readouterr().out


def test_scenario_show_prints_the_before_and_after(conn, capsys):
    garden = _scenario_garden(conn)
    _handle_scenario(conn, parse(["scenario", "show", "--id", garden["scenario"]]))
    out = capsys.readouterr().out

    assert "High Load varies 1 mote(s):" in out
    assert "R1" in out
    assert "current: 2 -> 10" in out


def test_scenario_show_rejects_a_mote_that_is_not_a_scenario(conn, capsys):
    plain = create_mote(conn, {"name": "Plain"})
    _handle_scenario(conn, parse(["scenario", "show", "--id", plain]))
    assert "No scenario found" in capsys.readouterr().out


def test_simulate_run_with_a_scenario_writes_nothing(conn, capsys):
    garden = _scenario_garden(conn)
    _handle_simulate(conn, parse(["simulate", "run", "--scenario", garden["scenario"]]))
    out = capsys.readouterr().out

    assert "Nothing was written" in out
    assert "-> 30" in out
    assert "voltage" not in get_mote(conn, garden["resistor"]).properties


### constraints


def _turbine_over_unity(conn):
    species = create_mote(conn, {"name": "Turbine", "freedoms": "efficiency,work"})
    for name, equation in (
        ("Work", "work == efficiency * drop"),
        ("Efficiency", "efficiency == 1.2"),
        ("Efficiency limit", "efficiency <= 1"),
    ):
        create_relationship(
            conn, create_mote(conn, {"name": name, "equation": equation}),
            species, APPLIES_TO,
        )
    mote = create_mote(conn, {"name": "T1", "drop": 100})
    set_species(conn, mote, species)
    return mote


def test_simulate_run_reports_an_unsatisfied_constraint(conn, capsys):
    _turbine_over_unity(conn)
    _handle_simulate(conn, parse(["simulate", "run"]))
    out = capsys.readouterr().out

    assert "1 constraint problem." in out
    assert "constraint: Efficiency limit on T1" in out
    assert "work: None -> 120" in out      # advisory: the run still solved


def test_simulate_run_with_strict_constraints_fails(conn, capsys):
    _turbine_over_unity(conn)
    _handle_simulate(conn, parse(["simulate", "run", "--strict-constraints"]))
    out = capsys.readouterr().out

    assert "Simulation failed" in out
    assert "constraint check failed" in out


# ---- worksheet ----

FEA = "bd.x = 80 in\nbd.y = 60 in\n\nbd.l = sqrt(bd.x^2 + bd.y^2)"


def test_worksheet_apply_reads_a_file_and_says_what_it_wrote(conn, capsys, tmp_path):
    sheet = tmp_path / "sheet.txt"
    sheet.write_text(FEA)
    _handle_worksheet(conn, parse(["worksheet", "apply", "--file", str(sheet)]))
    out = capsys.readouterr().out
    assert "Wrote 2 given(s) and 1 rule(s)." in out
    assert "Created bd." in out


def test_worksheet_apply_reads_standard_input(conn, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(FEA))
    _handle_worksheet(conn, parse(["worksheet", "apply"]))
    assert "Wrote 2 given(s)" in capsys.readouterr().out


def test_worksheet_read_prints_the_lines_back(conn, capsys, tmp_path):
    sheet = tmp_path / "sheet.txt"
    sheet.write_text(FEA)
    _handle_worksheet(conn, parse(["worksheet", "apply", "--file", str(sheet)]))
    capsys.readouterr()

    _handle_worksheet(conn, parse(["worksheet", "read"]))
    assert capsys.readouterr().out.strip() == FEA


def test_worksheet_read_says_so_on_an_empty_garden(conn, capsys):
    _handle_worksheet(conn, parse(["worksheet", "read"]))
    assert "no equations" in capsys.readouterr().out


def test_worksheet_apply_names_the_rules_it_deleted(conn, capsys, tmp_path):
    sheet = tmp_path / "sheet.txt"
    sheet.write_text(FEA + "\nbd.area = bd.x * bd.y")
    _handle_worksheet(conn, parse(["worksheet", "apply", "--file", str(sheet)]))
    sheet.write_text(FEA)
    capsys.readouterr()

    _handle_worksheet(conn, parse(["worksheet", "apply", "--file", str(sheet)]))
    out = capsys.readouterr().out
    assert "1 rule(s) had no line and were deleted" in out
    assert "bd.area == bd.x * bd.y" in out


def test_worksheet_apply_reports_a_broken_line(conn, capsys, tmp_path):
    sheet = tmp_path / "sheet.txt"
    sheet.write_text("bd.x = 80\nnonsense((")
    _handle_worksheet(conn, parse(["worksheet", "apply", "--file", str(sheet)]))
    assert "problem: line 2" in capsys.readouterr().out


def test_worksheet_rejects_an_unknown_action(conn, capsys):
    args = parse(["worksheet", "read"])
    args.action = "frobnicate"
    _handle_worksheet(conn, args)
    assert "Specify a worksheet action" in capsys.readouterr().out


# ---- health ----

def test_health_prints_the_counts_and_the_verdict(conn, capsys, tmp_path):
    sheet = tmp_path / "sheet.txt"
    sheet.write_text(FEA)
    _handle_worksheet(conn, parse(["worksheet", "apply", "--file", str(sheet)]))
    capsys.readouterr()

    _handle_health(conn, parse(["health"]))
    assert "1 equation, 1 unknown -- balanced" in capsys.readouterr().out


def test_health_names_the_block_that_is_short_an_equation(conn, capsys, tmp_path):
    sheet = tmp_path / "sheet.txt"
    sheet.write_text("bd.Ax = ?\nbd.Bx = ?\nbd.Ax + bd.Bx == 0")
    _handle_worksheet(conn, parse(["worksheet", "apply", "--file", str(sheet)]))
    capsys.readouterr()

    _handle_health(conn, parse(["health"]))
    out = capsys.readouterr().out
    assert "underdetermined" in out
    assert "bd.Ax, bd.Bx" in out
    assert "another equation needed" in out


def test_health_reports_a_dimensional_mismatch_against_its_line(conn, capsys, tmp_path):
    sheet = tmp_path / "sheet.txt"
    sheet.write_text("bd.x = 80 in\nbd.t = 3 s\nbd.l = bd.x + bd.t")
    _handle_worksheet(conn, parse(["worksheet", "apply", "--file", str(sheet)]))
    capsys.readouterr()

    _handle_health(conn, parse(["health"]))
    out = capsys.readouterr().out
    assert "units: bd.l == bd.x + bd.t on bd" in out
    assert "different dimensions" in out


def test_health_on_an_empty_garden_says_nothing_is_wrong(conn, capsys):
    _handle_health(conn, parse(["health"]))
    assert capsys.readouterr().out.strip() == "0 equations, 0 unknowns -- balanced"

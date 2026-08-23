import pytest

from cli.main import (
    _handle_garden,
    _handle_mote,
    _handle_model,
    _handle_simulate,
    _handle_incidence,
    _handle_roles,
    _handle_scenario,
    _handle_unit,
    _handle_why,
    build_parser,
)
from core.gardens import garden_exists
from core.motes import create_mote, get_mote
from core.simulation import list_runs
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


def test_simulate_history_lists_ok_and_failed_runs(conn, capsys):
    _wire_solvable_container(conn)
    _handle_simulate(conn, parse(["simulate"]))  # ok run
    capsys.readouterr()

    container = create_mote(conn, {"name": "Bad Container", "freedoms": "pressure"})
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
    has ever been solved -- unlike `why`, which reads the run log."""
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


def test_simulate_history_names_the_scenario_a_run_used(conn, capsys):
    """Two runs over the same rules differ only by which scenario they
    answered, so the listing has to say."""
    garden = _scenario_garden(conn)
    _handle_simulate(conn, parse(["simulate", "run"]))
    _handle_simulate(conn, parse(["simulate", "run", "--scenario", garden["scenario"]]))
    capsys.readouterr()

    _handle_simulate(conn, parse(["simulate", "history"]))
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]

    assert any("scenario=High Load" in line and "(saved)" in line for line in lines)
    assert any("scenario=" not in line and "(applied)" in line for line in lines)


def test_simulate_show_names_the_scenario(conn, capsys):
    garden = _scenario_garden(conn)
    _handle_simulate(conn, parse(["simulate", "run", "--scenario", garden["scenario"]]))
    capsys.readouterr()

    run_id = list_runs(conn)[0].run_id
    _handle_simulate(conn, parse(["simulate", "show", "--id", run_id]))
    assert "scenario: High Load" in capsys.readouterr().out


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

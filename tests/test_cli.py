import pytest

from cli.main import (
    _handle_garden,
    _handle_mote,
    _handle_model,
    _handle_simulate,
    _handle_unit,
    build_parser,
)
from core.gardens import garden_exists
from core.motes import create_mote
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


# ---- unit list/convert/check ----

def _resistor_with_units(conn, equation, units):
    """A one-instance species carrying declared units, for exercising the
    dimension check through the CLI."""
    species = create_mote(conn, {"name": "Resistor", "unknowns": "voltage"})
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

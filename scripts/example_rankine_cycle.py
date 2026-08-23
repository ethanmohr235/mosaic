#!/usr/bin/env python3
"""Build a worked *roles* example in a garden called `rankine-cycle-example`.

    .venv/bin/python scripts/example_rankine_cycle.py

`scripts/example_steam_turbine.py` proved the property-model architecture,
but its Boiler and Condenser carry no rules at all -- they exist only so
the mote graph looks like the real system. This is that example finished:
a complete Rankine cycle where every component computes its own state, and
an *assembly* mote wires them together and closes the loop.

The assembly is the point. `Rankine Cycle` is a species declaring four
roles:

    Rankine Cycle #1 --boiler----> Boiler
                     --turbine---> Turbine
                     --condenser-> Condenser
                     --pump------> Pump

A role is not a new kind of object: it is an ordinary relationship whose
type is the role's name. What the declaration adds is that the cycle's
rules can address a part by name --

    turbine.h_in == boiler.h_out

-- and that a mis-wired cycle is caught by `roles check` before it is ever
solved, instead of failing mid-solve or quietly solving the wrong thing.

Note what those linking rules do: a rule anchored on the *cycle* solves an
unknown belonging to the *turbine*. Nothing about that is new -- the
voltage-divider example has always solved node voltages from rules
anchored on resistors -- but naming the parts is what makes it readable as
a model rather than as graph navigation.

Two of the cycle's rules are **comparison relations** rather than
equations -- `efficiency <= 1` and `turbine.work >= pump.work`. They are
the same object as every other rule right up until the answer exists:
checked against it, never solved for. A constraint can therefore never be
what determines a value, and `producers` will not offer one.

Thermodynamics, for the curious: states are 1 (pump inlet, condensate),
2 (boiler inlet), 3 (turbine inlet, live steam), 4 (condenser inlet).
Turbine and pump both carry isentropic efficiencies. The condenser outlet
is given as slightly subcooled rather than as saturated liquid, because
the CoolProp provider currently exposes no quality-based capability and
`enthalpy_pt` is ambiguous exactly at saturation.

Re-run it and it rebuilds from scratch; delete the garden file to remove.
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.db import get_connection, initialize_database  # noqa: E402
from core.gardens import create_garden, garden_exists  # noqa: E402
from core.motes import create_mote, get_mote  # noqa: E402
from core.relationships import create_relationship  # noqa: E402
from core.roles import ROLES_KEY  # noqa: E402
from core.rules import APPLIES_TO  # noqa: E402
from core.simulation import SimulationError, check_roles, run_simulation  # noqa: E402
from core.species import set_species  # noqa: E402

GARDEN = "rankine-cycle-example"

# Operating point: 8 MPa / 500 C live steam, 10 kPa condenser.
BOILER_PRESSURE = 8e6
BOILER_TEMPERATURE = 773.15
CONDENSER_PRESSURE = 10e3
CONDENSER_TEMPERATURE = 318.0     # ~45 C, just below Tsat(10 kPa) = 318.96 K
TURBINE_EFFICIENCY = 0.85
PUMP_EFFICIENCY = 0.80

COMPONENTS = {
    "Boiler": {
        "freedoms": "h_in,h_out,s_out,heat_in",
        "properties": {
            "pressure_out": BOILER_PRESSURE,
            "temperature_out": BOILER_TEMPERATURE,
        },
        "rules": [
            ("Boiler outlet enthalpy",
             "h_out == property('working_fluid', 'enthalpy_pt', pressure_out, temperature_out)"),
            ("Boiler outlet entropy",
             "s_out == property('working_fluid', 'entropy_pt', pressure_out, temperature_out)"),
            ("Boiler heat added", "heat_in == h_out - h_in"),
        ],
    },
    "Turbine": {
        "freedoms": "h_in,s_in,h_ideal,h_out,work",
        "properties": {
            "pressure_out": CONDENSER_PRESSURE,
            "efficiency": TURBINE_EFFICIENCY,
        },
        "rules": [
            ("Turbine isentropic outlet",
             "h_ideal == property('working_fluid', 'enthalpy_ps', pressure_out, s_in)"),
            ("Turbine work", "work == efficiency * (h_in - h_ideal)"),
            ("Turbine outlet enthalpy", "h_out == h_in - work"),
        ],
    },
    "Condenser": {
        "freedoms": "h_in,h_out,heat_out",
        "properties": {
            "pressure": CONDENSER_PRESSURE,
            "temperature_out": CONDENSER_TEMPERATURE,
        },
        "rules": [
            ("Condenser outlet enthalpy",
             "h_out == property('working_fluid', 'enthalpy_pt', pressure, temperature_out)"),
            ("Condenser heat rejected", "heat_out == h_in - h_out"),
        ],
    },
    "Pump": {
        "freedoms": "h_in,volume,work,h_out",
        "properties": {
            "pressure_in": CONDENSER_PRESSURE,
            "pressure_out": BOILER_PRESSURE,
            "temperature_in": CONDENSER_TEMPERATURE,
            "efficiency": PUMP_EFFICIENCY,
        },
        "rules": [
            ("Pump specific volume",
             "volume == 1 / property('working_fluid', 'density_pt', pressure_in, temperature_in)"),
            ("Pump work", "work == volume * (pressure_out - pressure_in) / efficiency"),
            ("Pump outlet enthalpy", "h_out == h_in + work"),
        ],
    },
}

# Rules anchored on the cycle, addressing its parts by role. The first four
# close the loop -- each is the connection between two components, stated
# once, in the only place that knows about both.
CYCLE_RULES = [
    ("Steam to turbine", "turbine.h_in == boiler.h_out"),
    ("Steam entropy to turbine", "turbine.s_in == boiler.s_out"),
    ("Exhaust to condenser", "condenser.h_in == turbine.h_out"),
    ("Condensate to pump", "pump.h_in == condenser.h_out"),
    ("Feedwater to boiler", "boiler.h_in == pump.h_out"),
    ("Net work", "net_work == turbine.work - pump.work"),
    ("Heat input", "heat_in == boiler.heat_in"),
    ("Thermal efficiency", "efficiency == net_work / heat_in"),
    # Comparison relations: checked against the answer, never solved for.
    # Both read parts by role exactly as the equations above do -- a
    # constraint is the same object with a different operator.
    ("Efficiency is physical", "efficiency <= 1"),
    ("Turbine outruns pump", "turbine.work >= pump.work"),
]

CYCLE_ROLES = {
    "boiler": {"arity": "one"},
    "turbine": {"arity": "one"},
    "condenser": {"arity": "one"},
    "pump": {"arity": "one"},
}


def _add_rules(conn, species_id: str, rules) -> None:
    for name, equation in rules:
        rule = create_mote(conn, {"name": name, "equation": equation})
        create_relationship(conn, rule, species_id, APPLIES_TO)


def build(conn) -> dict[str, str]:
    water = create_mote(conn, {"name": "Water"})
    model = create_mote(
        conn,
        {
            "name": "IAPWS Water",
            "provider": "coolprop",
            "configuration": {"fluid": "Water", "backend": "HEOS"},
        },
    )
    create_relationship(conn, water, model, "uses_model")

    motes = {"Water": water}
    parts = {}
    for name, spec in COMPONENTS.items():
        species = create_mote(conn, {"name": name, "freedoms": spec["freedoms"]})
        _add_rules(conn, species, spec["rules"])
        part = create_mote(conn, {"name": f"{name} A", **spec["properties"]})
        set_species(conn, part, species)
        create_relationship(conn, part, water, "working_fluid")
        parts[name.lower()] = part
        motes[name] = part

    # The species declares the roles; the instance is wired to fill them.
    # Each role name is the relationship type -- there is no separate
    # binding table, because a relationship already is one.
    cycle_species = create_mote(
        conn,
        {
            "name": "Rankine Cycle",
            "freedoms": "net_work,heat_in,efficiency",
            ROLES_KEY: CYCLE_ROLES,
        },
    )
    _add_rules(conn, cycle_species, CYCLE_RULES)

    cycle = create_mote(conn, {"name": "Rankine Cycle #1"})
    set_species(conn, cycle, cycle_species)
    for role, part in parts.items():
        create_relationship(conn, cycle, part, role)
    motes["Cycle"] = cycle
    return motes


def main() -> int:
    if not garden_exists(GARDEN):
        create_garden(GARDEN)
    conn = get_connection(GARDEN)
    try:
        initialize_database(conn)
        conn.execute("DELETE FROM motes")  # rebuild from scratch; relationships cascade
        conn.commit()
        motes = build(conn)

        report = check_roles(conn)
        print(
            f"Role check: {report.motes_checked} mote(s) with declared roles, "
            f"{len(report.problems)} problem(s)."
        )
        for problem in report.problems:
            print(f"  {problem}")

        try:
            result = run_simulation(conn)
        except SimulationError as e:
            print(f"Simulation failed: {e}")
            return 1

        print(result.summary)
        for violation in result.constraint_violations:
            print(f"  constraint: {violation}")
        print(f"\nOpen the '{GARDEN}' garden in MonOSS to explore it.\n")

        cycle = get_mote(conn, motes["Cycle"]).properties
        boiler = get_mote(conn, motes["Boiler"]).properties
        turbine = get_mote(conn, motes["Turbine"]).properties
        pump = get_mote(conn, motes["Pump"]).properties
        print(
            f"  live steam    {BOILER_PRESSURE/1e6:.3g} MPa / "
            f"{BOILER_TEMPERATURE - 273.15:.4g} C   h = {boiler['h_out']/1e3:.6g} kJ/kg\n"
            f"  turbine work  {turbine['work']/1e3:.6g} kJ/kg "
            f"(eff {TURBINE_EFFICIENCY:.2g})\n"
            f"  pump work     {pump['work']/1e3:.4g} kJ/kg "
            f"(eff {PUMP_EFFICIENCY:.2g})\n"
            f"  heat in       {cycle['heat_in']/1e3:.6g} kJ/kg\n"
            f"  net work      {cycle['net_work']/1e3:.6g} kJ/kg\n"
            f"  efficiency    {cycle['efficiency'] * 100:.3g} %"
        )
        print(
            f"\n  why: .venv/bin/python -m cli.main --garden {GARDEN} "
            f"why {motes['Cycle']} efficiency"
        )
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Build a worked physical-property-model example in a garden called
`steam-turbine-example`.

    .venv/bin/python scripts/example_steam_turbine.py

Two turbines expand steam from a Boiler to a Condenser, each solving for
its own inlet/outlet enthalpy, inlet entropy, and work output -- but the
"Turbine" rule motes never mention CoolProp, or even that the fluid is
water. They ask the *property model* attached to the "Water" mote through
the property() builtin, the same way the ideal-gas example's rules never
mention density/temperature/pressure's relationship to each other:

    Turbine --working_fluid--> Water --uses_model--> IAPWS Water --provider--> coolprop

The turbine's four rules mirror the standard isentropic-turbine-efficiency
calculation: look up inlet enthalpy and entropy from (P_in, T_in), look up
the enthalpy an isentropic expansion to P_out would reach (P_out, s_in),
then scale the ideal enthalpy drop by the turbine's efficiency to get
actual work. Boiler/Condenser are included only so the mote graph looks
like the real system -- they
carry no rules of their own; this example proves the property-model
architecture, and deliberately stays two independent turbines rather than
a cycle, because two instances of one species is the shape it is
demonstrating.

For a complete cycle -- every component solving its own state, wired
together by an assembly mote through declared roles -- see
`scripts/example_rankine_cycle.py`.

`coolprop` (core/coolprop_provider.py) is MonOSS's first provider backed by
a real external library; core/ideal_gas_provider.py, not this, proved the
architecture first.

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
from core.rules import APPLIES_TO  # noqa: E402
from core.simulation import SimulationError, run_simulation  # noqa: E402
from core.species import set_species  # noqa: E402

GARDEN = "steam-turbine-example"

TURBINE_EQUATIONS = [
    ("Turbine inlet enthalpy", "h_in == property('working_fluid', 'enthalpy_pt', pressure_in, temperature_in)"),
    ("Turbine inlet entropy", "s_in == property('working_fluid', 'entropy_pt', pressure_in, temperature_in)"),
    ("Turbine isentropic outlet enthalpy", "h_out == property('working_fluid', 'enthalpy_ps', pressure_out, s_in)"),
    ("Turbine work", "work == efficiency * (h_in - h_out)"),
]

TURBINES = [
    # name, pressure_in (Pa), temperature_in (K), pressure_out (Pa), efficiency
    ("HP Turbine", 8e6, 773.15, 10e3, 0.85),   # 8 MPa / 500 C -> 10 kPa condenser
    ("LP Turbine", 2e6, 573.15, 10e3, 0.85),   # 2 MPa / 300 C -> 10 kPa condenser
]


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

    boiler = create_mote(conn, {"name": "Boiler"})
    condenser = create_mote(conn, {"name": "Condenser"})

    turbine_species = create_mote(
        conn, {"name": "Turbine", "freedoms": "h_in,s_in,h_out,work"}
    )
    for name, equation in TURBINE_EQUATIONS:
        rule = create_mote(conn, {"name": name, "equation": equation})
        create_relationship(conn, rule, turbine_species, APPLIES_TO)

    motes = {"Water": water, "Boiler": boiler, "Condenser": condenser}
    for name, pressure_in, temperature_in, pressure_out, efficiency in TURBINES:
        turbine = create_mote(
            conn,
            {
                "name": name,
                "pressure_in": pressure_in,
                "temperature_in": temperature_in,
                "pressure_out": pressure_out,
                "efficiency": efficiency,
            },
        )
        set_species(conn, turbine, turbine_species)
        create_relationship(conn, turbine, water, "working_fluid")
        create_relationship(conn, turbine, boiler, "inlet")
        create_relationship(conn, turbine, condenser, "outlet")
        motes[name] = turbine

    return motes


def main() -> int:
    # The docstring promises a re-run rebuilds from scratch (the
    # DELETE below does that); create_garden raises if the file is
    # already there, so only create it the first time.
    if not garden_exists(GARDEN):
        create_garden(GARDEN)
    conn = get_connection(GARDEN)
    try:
        initialize_database(conn)
        conn.execute("DELETE FROM motes")  # rebuild from scratch; relationships cascade
        conn.commit()
        motes = build(conn)

        try:
            result = run_simulation(conn)
        except SimulationError as e:
            print(f"Simulation failed: {e}")
            return 1

        print(result.summary)
        print(f"\nOpen the '{GARDEN}' garden in MonOSS to explore it.\n")
        for name, pressure_in, temperature_in, pressure_out, efficiency in TURBINES:
            properties = get_mote(conn, motes[name]).properties
            h_in, s_in, h_out, work = (
                properties["h_in"], properties["s_in"], properties["h_out"], properties["work"]
            )
            print(
                f"  {name:<10} {pressure_in/1e6:.3g} MPa/{temperature_in - 273.15:.4g} C "
                f"-> {pressure_out/1e3:.3g} kPa  eff={efficiency:.2g}\n"
                f"    h_in={h_in/1e3:.6g} kJ/kg  s_in={s_in/1e3:.6g} kJ/(kg K)  "
                f"h_out={h_out/1e3:.6g} kJ/kg  work={work/1e3:.6g} kJ/kg"
            )
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

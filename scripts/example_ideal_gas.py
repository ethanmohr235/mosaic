#!/usr/bin/env python3
"""Build a worked physical-property-model example in a garden called
`ideal-gas-example`.

    .venv/bin/python scripts/example_ideal_gas.py

Two containers of air at different states, each solving for its own
pressure via the ideal gas law -- but the "Ideal gas law" rule mote never
mentions density, temperature, or pressure's relationship to them in
Python. It asks the *property model* attached to the "Air" mote through
the new property() builtin, the same way the circuit example's rules
never import volts or ohms:

    Container --gas--> Air --uses_model--> Ideal Air --provider--> ideal_gas

`ideal_gas` (core/ideal_gas_provider.py) is a plain closed-form equation
of state, not an external library -- see docs/physical_property_proposal
for why it, rather than a real library like CoolProp, is what proves this
architecture first.

Re-run it and it rebuilds from scratch; delete the garden file to remove.
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.db import get_connection, initialize_database  # noqa: E402
from core.gardens import create_garden  # noqa: E402
from core.motes import create_mote, get_mote  # noqa: E402
from core.relationships import create_relationship  # noqa: E402
from core.rules import APPLIES_TO  # noqa: E402
from core.simulation import SimulationError, run_simulation  # noqa: E402
from core.species import set_species  # noqa: E402

GARDEN = "ideal-gas-example"

CONTAINERS = [
    # name, density (kg/m^3), temperature (K)
    ("C1", 1.2, 293.15),   # ~room-temperature air
    ("C2", 0.9, 350.0),    # heated air, lower density
]


def build(conn) -> dict[str, str]:
    air = create_mote(conn, {"name": "Air"})
    model = create_mote(
        conn,
        {
            "name": "Ideal Air",
            "provider": "ideal_gas",
            "configuration": {"gas_constant": 287.05},
        },
    )
    create_relationship(conn, air, model, "uses_model")

    container = create_mote(conn, {"name": "Container", "unknowns": "pressure"})
    create_relationship(
        conn,
        create_mote(
            conn,
            {
                "name": "Ideal gas law",
                "equation": "pressure == property('gas', 'pressure', density, temperature)",
            },
        ),
        container,
        APPLIES_TO,
    )

    motes = {"Air": air}
    for name, density, temperature in CONTAINERS:
        motes[name] = create_mote(
            conn, {"name": name, "density": density, "temperature": temperature}
        )
        set_species(conn, motes[name], container)
        create_relationship(conn, motes[name], air, "gas")

    return motes


def main() -> int:
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
        for name, density, temperature in CONTAINERS:
            properties = get_mote(conn, motes[name]).properties
            pressure = properties["pressure"]
            expected = density * 287.05 * temperature
            print(
                f"  {name:<4} density={density:.4g} kg/m^3  temperature={temperature:.4g} K  "
                f"pressure={pressure:.6g} Pa  (expected {expected:.6g} Pa)"
            )
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Build a worked circuit example in a garden called `circuit-example`.

    .venv/bin/python scripts/example_voltage_divider.py

A 10 V source across a 100 ohm and a 200 ohm resistor in series. Nothing
here is circuit-specific code -- the engine knows nothing about volts or
resistance. Ohm's law and Kirchhoff's current law are written as ordinary
rule motes, which is the whole point: the same machinery models anything
you can write equations for.

    N1 ----[R1 100]---- N2
    |                    |
  [V1 10V]           [R2 200]
    |                    |
    N0 (ground) ---------+

Terminal convention: current enters a component's `a` terminal and leaves
its `b` terminal. That makes the source's own current come out negative,
since conventional current leaves a source's positive terminal.

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

GARDEN = "circuit-example"

RULES = [
    ("Ohm's law", "Resistor",
     "targets('a').voltage - targets('b').voltage == current * resistance"),
    ("Kirchhoff's current law", "Node",
     "sum(sources('b').current) - sum(sources('a').current) == 0"),
    ("Source voltage", "VoltageSource",
     "targets('a').voltage - targets('b').voltage == voltage"),
    ("Ground reference", "Ground",
     "voltage == 0"),
]

SPECIES = [
    ("Node", "voltage"),
    ("Resistor", "current"),
    ("VoltageSource", "current"),
    ("Ground", ""),
]


def build(conn) -> dict[str, str]:
    species = {}
    for name, freedoms in SPECIES:
        properties = {"name": name}
        if freedoms:
            properties["freedoms"] = freedoms
        species[name] = create_mote(conn, properties)

    for name, species_name, equation in RULES:
        rule_id = create_mote(conn, {"name": name, "equation": equation})
        create_relationship(conn, rule_id, species[species_name], APPLIES_TO)

    motes = {}
    for name in ("N0", "N1", "N2"):
        motes[name] = create_mote(conn, {"name": name})
        set_species(conn, motes[name], species["Node"])
    set_species(conn, motes["N0"], species["Ground"])

    motes["V1"] = create_mote(conn, {"name": "V1", "voltage": 10})
    set_species(conn, motes["V1"], species["VoltageSource"])
    motes["R1"] = create_mote(conn, {"name": "R1", "resistance": 100})
    set_species(conn, motes["R1"], species["Resistor"])
    motes["R2"] = create_mote(conn, {"name": "R2", "resistance": 200})
    set_species(conn, motes["R2"], species["Resistor"])

    for element, node_a, node_b in (("V1", "N1", "N0"), ("R1", "N1", "N2"), ("R2", "N2", "N0")):
        create_relationship(conn, motes[element], motes[node_a], "a")
        create_relationship(conn, motes[element], motes[node_b], "b")

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
        for name in ("N0", "N1", "N2", "V1", "R1", "R2"):
            properties = get_mote(conn, motes[name]).properties
            values = {k: v for k, v in sorted(properties.items()) if k != "name"}
            rendered = ", ".join(
                f"{k}={v:.4g}" if isinstance(v, (int, float)) else f"{k}={v}"
                for k, v in values.items()
            )
            print(f"  {name:<4} {rendered}")
        print("\nExpected: N1 at 10 V, N2 at 6.667 V, N0 at 0 V, 1/30 A through both resistors.")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

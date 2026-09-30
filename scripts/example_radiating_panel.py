#!/usr/bin/env python3
"""Build a worked units + nonlinear-solver example in a garden called
`radiating-panel-example`.

    .venv/bin/python scripts/example_radiating_panel.py

Two radiating panels, each solving for the surface temperature it must
reach to shed its heat load, via the Stefan-Boltzmann law:

    power == emissivity * stefan_boltzmann * area * temperature ** 4

That one rule demonstrates both features at once.

**It is genuinely nonlinear in its unknown.** `temperature ** 4` cannot
be folded into a weighted sum of unknowns, and no other rule determines
temperature first, so the substitution loop can't reach it either. The
default `linear` solver refuses it outright; `newton` finds the root.
See docs/simulation.md's Solvers section.

**Its inputs are in four different units.** Panel A is described in
watts, square metres and kelvin; Panel B in kilowatts, square
centimetres and degrees Celsius. They are the *same panel under the same
load*, written down two different ways, and they must come out at the
same temperature. Nothing in the rule mentions any of this -- the solver
normalizes each value to base SI on the way in and writes each answer
back in the unit its own mote declares, so one panel reports 314.61 K
and the other 41.46 degC. Celsius is the interesting case: it is affine
in kelvin, not a multiple of it, which is why a unit carries an offset
at all.

Because the rule is dimensionally consistent (W = m^2 * K^4 *
W/(m^2*K^4)), the dimension checker passes it -- and the last section
shows what it says when a rule doesn't add up.

Re-run it and it rebuilds from scratch; delete the garden file to remove.
"""
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.db import get_connection, initialize_database  # noqa: E402
from core.gardens import create_garden, garden_exists  # noqa: E402
from core.motes import create_mote, delete_mote, get_mote  # noqa: E402
from core.relationships import create_relationship  # noqa: E402
from core.rules import APPLIES_TO  # noqa: E402
from core.simulation import (  # noqa: E402
    SimulationError,
    check_garden,
    run_simulation,
    run_time_series,
)
from core.species import set_species  # noqa: E402
from core.units import convert  # noqa: E402

GARDEN = "radiating-panel-example"

STEFAN_BOLTZMANN = 5.670374419e-8   # W/(m^2*K^4)
EMISSIVITY = 0.9

RULE = "power == emissivity * stefan_boltzmann * area * temperature ** 4"

# name, power, area, and the units each panel is described in. The same
# physical situation twice over, written down two different ways.
PANELS = [
    {
        "name": "Panel A",
        "power": 1000.0,
        "area": 2.0,
        "guess": 300.0,
        "units": {
            "power": "W",
            "area": "m^2",
            "temperature": "K",
            "stefan_boltzmann": "W/(m^2*K^4)",
        },
    },
    {
        "name": "Panel B",
        "power": 1.0,          # kW    (= 1000 W, same as Panel A)
        "area": 20000.0,       # cm^2  (= 2 m^2, same as Panel A)
        "guess": 20.0,         # degC
        "units": {
            "power": "kW",
            "area": "cm^2",
            "temperature": "degC",
            "stefan_boltzmann": "W/(m^2*K^4)",
        },
    },
]


def build(conn) -> tuple[str, dict[str, str]]:
    panel = create_mote(conn, {"name": "Panel", "freedoms": "temperature"})
    create_relationship(
        conn,
        create_mote(conn, {"name": "Stefan-Boltzmann law", "equation": RULE}),
        panel,
        APPLIES_TO,
    )

    motes = {}
    for spec in PANELS:
        motes[spec["name"]] = create_mote(
            conn,
            {
                "name": spec["name"],
                "power": spec["power"],
                "area": spec["area"],
                "emissivity": EMISSIVITY,          # a pure ratio: no unit
                "stefan_boltzmann": STEFAN_BOLTZMANN,
                # A starting point for the iterative solver, not an input:
                # `temperature` is declared unknown, so this value is only
                # ever used as newton's initial guess (and is normalized
                # to kelvin first, like any other declared value).
                "temperature": spec["guess"],
                "units": spec["units"],
            },
        )
        set_species(conn, motes[spec["name"]], panel)

    return panel, motes


def expected_kelvin(spec) -> float:
    """The same answer by hand, in kelvin, from SI-converted inputs."""
    power = convert(spec["power"], spec["units"]["power"], "W")
    area = convert(spec["area"], spec["units"]["area"], "m^2")
    return (power / (EMISSIVITY * STEFAN_BOLTZMANN * area)) ** 0.25


def show_linear_refuses(conn) -> None:
    """The default solver, previewed in memory so nothing is written --
    this run is meant to fail."""
    # The failure is the point here, and the script reports it below, so
    # don't also let the engine log it as a warning to stderr.
    engine_log = logging.getLogger("core.simulation")
    previous_level = engine_log.level
    engine_log.setLevel(logging.ERROR)
    try:
        preview = run_time_series(conn, step_count=1, solver="linear")
    finally:
        engine_log.setLevel(previous_level)
    print("With the default `linear` solver:")
    print(f"  {preview.error}\n")


def show_units_mistake(conn, panel_species: str) -> None:
    """What the dimension checker says about a rule that doesn't add up.
    The broken rule is removed again, leaving the garden as it was."""
    bad = create_mote(
        conn, {"name": "Bad rule", "equation": "power == area + temperature"}
    )
    create_relationship(conn, bad, panel_species, APPLIES_TO)
    report = check_garden(conn)
    for problem in report.problems:
        print(f"  {problem}")
    delete_mote(conn, bad)


def main() -> int:
    if not garden_exists(GARDEN):
        create_garden(GARDEN)
    conn = get_connection(GARDEN)
    try:
        initialize_database(conn)
        conn.execute("DELETE FROM motes")  # rebuild from scratch; relationships cascade
        conn.commit()
        panel_species, motes = build(conn)

        report = check_garden(conn)
        print(
            f"Dimension check: {len(report.problems)} problem(s) "
            f"in {report.pairs_checked} rule/instance pair(s).\n"
        )

        show_linear_refuses(conn)

        try:
            result = run_simulation(conn, solver="newton", strict_units=True)
        except SimulationError as e:
            print(f"Simulation failed: {e}")
            return 1

        print(f"With `newton`: {result.summary}")
        print(f"\nOpen the '{GARDEN}' garden in MonOSS to explore it.\n")

        for spec in PANELS:
            properties = get_mote(conn, motes[spec["name"]]).properties
            unit = spec["units"]["temperature"]
            solved = properties["temperature"]
            in_kelvin = convert(solved, unit, "K")
            print(
                f"  {spec['name']}: {spec['power']:g} {spec['units']['power']} "
                f"over {spec['area']:g} {spec['units']['area']}"
            )
            print(
                f"    temperature = {solved:.4g} {unit}"
                f"  ({in_kelvin:.5g} K, expected {expected_kelvin(spec):.5g} K)"
            )

        print(
            "\nBoth panels are the same size and load, described in different "
            "units, and reach the same temperature."
        )

        print("\nWhat a units mistake looks like:")
        show_units_mistake(conn, panel_species)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

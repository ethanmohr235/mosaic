# moxie/payload.py
"""A garden shaped as things worth plotting, for the JS bridge.

Motion's payload asks "what is this garden as a simulable system"; this
one asks the narrower question a graph needs: *which numbers move, and
which could be made to*. Everything here is derived from what core
already answers -- no mote says it is plottable, and none should have to.

    what can be a Y      <- any numeric property, solved or given
    what can be an X     <- the same set; sweeping a freedom pins it
    what a series costs  <- core/sweep.py's cases, not its series
    what a unit means    <- core/units.py, never a string comparison

The one judgement it makes is what counts as a number: a bool is not one
(Python says otherwise and would offer `done` as an axis), and neither is
a numeric string. Everything else the garden holds is fair game, which is
the whole of Moxie's "any property against any property".

Like motion/payload.py, this module shapes answers and computes none.
"""
import logging

from core import units as units_repo
from core.entities import Mote
from core.sweep import Dot, SurfaceResult, SweepResult

logger = logging.getLogger(__name__)

# How wide a suggested sweep range is around a property's current value.
# Half to double is the range a knob is usually worth turning: wide enough
# that the curve has shape, narrow enough that it stays near the model the
# user actually built.
_SUGGESTION_SPAN = (0.5, 2.0)


def is_number(value) -> bool:
    """A value a graph can plot. `isinstance(True, int)` is True in
    Python, which would otherwise put every boolean flag in the garden on
    the axis menu."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def suggest_range(value: float | None) -> tuple[float, float]:
    """A first range to sweep a property over, from what it currently is.

    Around the current value rather than from zero: the interesting part
    of a curve is almost always near the point the model was built at. A
    value of zero has no scale to derive one from, so it gets a plain unit
    range instead of a degenerate one."""
    if value is None or value == 0:
        return (0.0, 1.0)
    low, high = (v * value for v in _SUGGESTION_SPAN)
    return (low, high) if low < high else (high, low)


def property_row(
    key: str, value, unit: str | None, free: bool
) -> dict:
    """One plottable property as the mote rail draws it.

    `value` may be None: a declared freedom nothing has solved yet holds
    no number, and is exactly the property most worth plotting. Leaving
    it out until something wrote to it would mean a fresh garden could
    graph only its inputs."""
    low, high = suggest_range(float(value) if value is not None else None)
    return {
        "key": key,
        "value": float(value) if value is not None else None,
        "unit": units_repo.format_unit(unit) if unit else None,
        "dimension": _dimension(unit),
        # Whether the solver determines this. A freedom makes a natural Y
        # and a pinned X; a given makes a natural X and a flat Y.
        "free": free,
        "suggestLo": low,
        "suggestHi": high,
    }


def _dimension(unit: str | None) -> str | None:
    """A unit's dimension as a string, for grouping axes by what they
    measure. Two series share an axis when this matches -- which is a
    real question about physics, not about spelling, so `kohm` and `ohm`
    agree here and `ohm` and `volt` do not."""
    if not unit:
        return None
    try:
        return units_repo.describe(units_repo.dimension_of(unit))
    except units_repo.UnitError:
        logger.debug("unknown unit %r on a plottable property", unit)
        return None


def mote_row(
    mote: Mote, freedoms: set[str], species_label: str | None
) -> dict | None:
    """One mote as the left rail draws it, or None if it holds no numbers.

    A mote with nothing numeric on it cannot appear in a plot, and
    listing it would make the rail mostly noise in a garden that is
    largely notes and tasks."""
    units = units_repo.parse_units_property(mote.properties.get(units_repo.UNITS_KEY))
    rows = [
        property_row(key, value, units.get(key), key in freedoms)
        for key, value in sorted(mote.properties.items())
        if is_number(value)
    ]
    # A declared freedom nothing has solved yet is not among the mote's
    # properties at all -- and in a garden that has never been run, those
    # are precisely the interesting curves. A freedom whose key holds
    # something non-numeric is left out: it is that value's key first.
    unsolved = sorted(
        key for key in freedoms
        if key not in mote.properties and key not in {r["key"] for r in rows}
    )
    rows.extend(
        property_row(key, None, units.get(key), True) for key in unsolved
    )
    if not rows:
        return None
    name = mote.properties.get("name")
    return {
        "id": mote.id,
        "name": str(name) if name not in (None, "") else mote.id[:8],
        "species": species_label,
        "properties": rows,
        "freeCount": sum(1 for r in rows if r["free"]),
    }


def sweep_payload(result: SweepResult, series_ids: dict[tuple[str, str, str], str]) -> dict:
    """A SweepResult on the wire, keyed by the caller's own series ids.

    `points` carry their axis index rather than being a dense list,
    because a coarse pass returns every nth one and the frontend merges
    passes by index. The values are keyed by series id rather than by
    (case, mote, key) for the same reason step_payloads() exists in core:
    a tuple is not a JSON key, and inventing a delimiter here would mean
    inventing the same one again on the other side."""
    return {
        "points": [
            {
                "i": point.index,
                "x": point.x,
                "v": {
                    series_ids[k]: value
                    for k, value in point.values.items()
                    if k in series_ids
                },
                "errors": dict(point.errors),
                "violated": sorted(point.violations),
            }
            for point in result.points
        ],
        "axis": {
            "mote": result.axis.mote_id, "key": result.axis.key,
            "lo": result.axis.lo, "hi": result.axis.hi, "steps": result.axis.steps,
        },
        "pinned": result.pinned,
        "cancelled": result.cancelled,
        "solves": result.solves,
        "caseCount": result.case_count,
        "failures": result.failures,
        "seconds": round(result.seconds, 4),
        "brokenConstraints": result.broken_constraints,
    }


def surface_payload(result: SurfaceResult) -> dict:
    """A grid on the wire. Cells keep their real row/col so a finer pass
    lands on top of a coarse one rather than beside it."""
    span = result.span
    return {
        "cells": [
            {"r": c.row, "c": c.col, "x": c.x, "y": c.y,
             "v": c.value, "error": c.error}
            for c in result.cells
        ],
        "rows": result.y_axis.count,
        "cols": result.x_axis.count,
        "lo": span[0] if span else None,
        "hi": span[1] if span else None,
        "solves": result.solves,
        "cancelled": result.cancelled,
        "seconds": round(result.seconds, 4),
    }


def correlation_payload(dots: list[Dot]) -> list[dict]:
    return [{"id": d.mote_id, "name": d.name, "x": d.x, "y": d.y} for d in dots]


def unit_bands(series: list[dict]) -> list[dict]:
    """Series grouped by what they measure, in first-seen order.

    One band is one Y axis. Two bands means a right-hand axis; three or
    more means no honest shared scale exists and the plot normalizes,
    which the frontend decides from the length of this list. Series with
    no declared unit share the unitless band -- a garden that declares no
    units at all therefore gets exactly one axis, as it always did."""
    bands: list[dict] = []
    for item in series:
        dimension = item.get("dimension")
        existing = next((b for b in bands if b["dimension"] == dimension), None)
        if existing is None:
            bands.append({
                "dimension": dimension,
                "unit": item.get("unit"),
                "series": [item["id"]],
            })
            continue
        existing["series"].append(item["id"])
        if existing["unit"] != item.get("unit"):
            # Two commensurable units written differently (kohm and ohm).
            # The axis label stops claiming either.
            existing["unit"] = None
    return bands

# core/uncertainty.py
"""How uncertain each answer is, given how uncertain each given is.

A given carries its ± in the reserved `uncertainty` property, a
per-property map shaped exactly like `units` (decided, `roadmap.md` ->
*Uncertainty*):

    uncertainty:  {"x": 0.5}        ± 0.5, in x's own unit
    uncertainty:  {"x": "2%"}       ± 2% of x's value

The value itself stays a plain number, so nothing that reads it --
the solver, a query, `\\val` -- changes because it is uncertain.

**A result's ± is derived on read and never stored**, like solve health:
`propagate()` answers from the garden as it stands, so there is nothing
to go stale when a given's ± is edited. It is first-order and assumes the
givens are independent, combining them root-sum-square:

    σ_y = sqrt( Σ_i (∂y/∂x_i · σ_i)² )

Each ∂y/∂x_i is a central difference of the solve itself: the garden is
solved with x_i nudged up and down, on copies, writing nothing. To first
order that is exactly the implicit-function derivative −J_y⁻¹·J_x a
Newton step would give, and it has two advantages over reading the
Jacobian out of one backend: it works for every solver backend (the
linear one has no Jacobian to hand over), and it includes everything the
solve does -- unit conversion, substitution, property models -- because
it *is* the solve. It costs two solves per uncertain given.

A freedom's own entry in `uncertainty` is ignored: a solved value's ± is
what this computes, and one written by hand would be a second account of
it."""
import math
import re
import sqlite3
from dataclasses import dataclass, field

from core import motes as motes_repo
from core import relationships as rel_repo
from core import rules as rules_repo
from core import simulation
from core.formula import Unknown

UNCERTAINTY_KEY = "uncertainty"

_PERCENT_RE = re.compile(r"^\s*(?P<n>[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?)\s*%\s*$")

# The nudge, as a fraction of the larger of |x| and σ. Small enough that
# the curvature a nonlinear model has between x-h and x+h is negligible;
# large enough to sit well above the solver's own convergence tolerance,
# which would otherwise be what the difference measured.
_STEP = 1e-4


@dataclass(frozen=True)
class Spread:
    """A given's ±: absolute, in the property's own unit, or relative, as
    a fraction of its value."""
    absolute: float | None = None
    relative: float | None = None

    def sigma(self, value: float) -> float:
        if self.absolute is not None:
            return self.absolute
        return abs(value) * (self.relative or 0.0)


def parse_uncertainty_property(value) -> tuple[dict[str, Spread], list[str]]:
    """A mote's `uncertainty` map, and what in it could not be read.

    A non-negative number is an absolute ±, and `"2%"` a relative one.
    Unlike `units`, an unreadable entry is reported rather than silently
    dropped: a ± that vanished would make a result look more certain than
    it is, which is the one mistake this module exists to prevent."""
    if not isinstance(value, dict):
        return {}, ([] if value is None else ["`uncertainty` is not a map of property to ±"])
    found: dict[str, Spread] = {}
    problems: list[str] = []
    for key, entry in value.items():
        if isinstance(entry, bool):
            problems.append(f"{key}: {entry!r} is not a ±")
        elif isinstance(entry, (int, float)) and entry >= 0 and math.isfinite(entry):
            found[str(key)] = Spread(absolute=float(entry))
        elif isinstance(entry, str) and (match := _PERCENT_RE.match(entry)):
            found[str(key)] = Spread(relative=float(match["n"]) / 100.0)
        else:
            problems.append(f"{key}: {entry!r} is not a ± -- write a number in the "
                            f"property's unit, or a percentage like \"2%\"")
    return found, problems


def sigma_of(properties: dict, key: str) -> float | None:
    """A given's ± in its own unit, or None where it has none (or no
    numeric value for a relative one to be a fraction of)."""
    spread = parse_uncertainty_property(properties.get(UNCERTAINTY_KEY))[0].get(key)
    value = properties.get(key)
    if spread is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return spread.sigma(float(value))


@dataclass(frozen=True)
class Contribution:
    """One given's share of one result's ±. `sensitivity` is ∂y/∂x in the
    two properties' own units; `share` is the fraction of the variance,
    so a budget table's shares add up to 1."""
    mote_id: str
    key: str
    label: str
    sigma: float
    sensitivity: float
    share: float = 0.0


@dataclass(frozen=True)
class Propagated:
    """A solved value and its ±, in the mote's declared unit (`unit`).

    `unit` is the declared one, or else the one a solve infers
    (core/simulation.py::inferred_units). None means neither: the value
    is in base SI."""
    mote_id: str
    key: str
    label: str
    value: float
    sigma: float
    contributions: tuple[Contribution, ...] = ()
    unit: str | None = None


@dataclass(frozen=True)
class Report:
    results: tuple[Propagated, ...] = ()
    inputs: tuple[Contribution, ...] = ()   # the givens with a ±, sensitivity 0
    problems: tuple[str, ...] = field(default_factory=tuple)

    def of(self, mote_id: str, key: str) -> Propagated | None:
        for result in self.results:
            if (result.mote_id, result.key) == (mote_id, key):
                return result
        return None


def has_uncertainty(conn: sqlite3.Connection) -> bool:
    """Whether any mote carries a ± -- the cheap question a document asks
    before paying for two solves per given."""
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM mote_properties WHERE key = ? LIMIT 1", (UNCERTAINTY_KEY,))
    return cur.fetchone() is not None


def propagate(conn: sqlite3.Connection, solver: str = "linear") -> Report:
    """Every solved value's ±, from the givens' ±. Writes nothing.

    Solves the garden as it stands, then once more per given either side
    of it. A result nothing uncertain reaches comes back with σ = 0 --
    exactly known, given what was declared -- rather than being left out,
    since "no ±" and "± 0" look the same only until someone asks."""
    motes = motes_repo.list_motes(conn)
    properties = {m.id: dict(m.properties) for m in motes}
    relationships = rel_repo.list_relationships(conn)
    rules = rules_repo.list_rules(conn)
    freedoms = simulation.garden_freedoms(conn)
    labels = {m.id: _name(m.properties, m.id) for m in motes}
    inferred = simulation.inferred_units_of(properties, relationships, rules)

    inputs, problems = _inputs(properties, freedoms, labels)
    try:
        base = simulation.solve_properties(
            properties, relationships, rules, solver, log_constraints=False)
    except simulation.SimulationError as e:
        return Report((), tuple(inputs), (*problems, f"the garden does not solve: {e}"))

    solved = sorted(base.values)
    derivatives: dict[Unknown, dict[Unknown, float]] = {u: {} for u in solved}
    for given in inputs:
        x = float(properties[given.mote_id][given.key])
        h = _STEP * max(abs(x), given.sigma) or _STEP
        try:
            up = _solve_with(properties, relationships, rules, solver, given, x + h)
            down = _solve_with(properties, relationships, rules, solver, given, x - h)
        except simulation.SimulationError as e:
            problems.append(f"{given.label}: the garden does not solve with it nudged ({e})")
            continue
        for unknown in solved:
            # Only values both nudged solves actually determined: .value()
            # falls back to what the mote stores, which is no derivative.
            if unknown not in up.values or unknown not in down.values:
                continue
            hi, lo = up.value(*unknown), down.value(*unknown)
            derivatives[unknown][(given.mote_id, given.key)] = (hi - lo) / (2 * h)

    results = []
    for unknown in solved:
        value = base.value(*unknown)
        if value is None:
            continue
        parts = [
            Contribution(g.mote_id, g.key, g.label, g.sigma,
                         derivatives[unknown].get((g.mote_id, g.key), 0.0))
            for g in inputs
        ]
        variance = sum((c.sensitivity * c.sigma) ** 2 for c in parts)
        parts = [
            Contribution(c.mote_id, c.key, c.label, c.sigma, c.sensitivity,
                         (c.sensitivity * c.sigma) ** 2 / variance if variance else 0.0)
            for c in parts if c.sensitivity * c.sigma != 0
        ]
        results.append(Propagated(
            unknown[0], unknown[1], f"{labels.get(unknown[0], unknown[0][:8])}.{unknown[1]}",
            value, math.sqrt(variance),
            tuple(sorted(parts, key=lambda c: -c.share)),
            simulation.units_for(properties[unknown[0]], inferred.get(unknown[0])).get(unknown[1]),
        ))
    results.sort(key=lambda r: (r.label.casefold()))
    return Report(tuple(results), tuple(inputs), tuple(problems))


def _inputs(
    properties: dict[str, dict], freedoms: dict[str, set[str]], labels: dict[str, str]
) -> tuple[list[Contribution], list[str]]:
    inputs: list[Contribution] = []
    problems: list[str] = []
    for mote_id, props in properties.items():
        spreads, unreadable = parse_uncertainty_property(props.get(UNCERTAINTY_KEY))
        label = labels[mote_id]
        problems += [f"{label}.{p}" if not p.startswith("`") else f"{label}: {p}"
                     for p in unreadable]
        for key, spread in sorted(spreads.items()):
            if key in freedoms.get(mote_id, set()):
                continue  # a result's ± is computed, never declared
            value = props.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                problems.append(f"{label}.{key} has a ± but no number")
                continue
            sigma = spread.sigma(float(value))
            if sigma > 0:
                inputs.append(Contribution(mote_id, key, f"{label}.{key}", sigma, 0.0))
    inputs.sort(key=lambda c: c.label.casefold())
    return inputs, problems


def _solve_with(properties, relationships, rules, solver, given: Contribution, x: float):
    nudged = dict(properties)
    nudged[given.mote_id] = {**properties[given.mote_id], given.key: x}
    return simulation.solve_properties(
        nudged, relationships, rules, solver, log_constraints=False)


def _name(properties: dict, mote_id: str) -> str:
    name = properties.get("name")
    return name if isinstance(name, str) and name else mote_id[:8]

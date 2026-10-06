# core/fit.py
"""Fitting a model to measured points (docs/console.md -> Settled for
`fit`).

    fit C1.current vs C1.voltage --data I vs V [--from "bench.csv"] --free C1.r_a [--scenario s] [--apply]

The model is the garden: `C1.current` as the garden solves it when
`C1.voltage` is set to a point's x. The data is every mote carrying both
data keys (`I`, `V`) -- or, with `--from`, only the rows of that CSV
import (core/csv_import.py's `imported_from`). The fit moves the `--free`
inputs, starting from their current values, until the model's y at each
point's x is as close to the point's y as least squares can make it.

Levenberg-Marquardt, numpy only: the Jacobian is a forward difference,
one extra solve per free input per iteration, so a fit costs about
(inputs + 1) x points x iterations solves. Each value comes back with
its σ from the covariance (s² (JᵀJ)⁻¹), plus RMS, R², the iteration
count and whether it converged. A point the model cannot solve at the
starting values is reported and left out, never silently dropped; inputs
the data cannot tell apart (a singular JᵀJ) come back with no σ and a
message saying so.

Nothing is written unless asked: apply() writes each fitted value, its σ
into the reserved `uncertainty` map and `source: {kind: fitted, note:
‹the line›}`, as one undo step."""
import math
import shlex
import sqlite3
from dataclasses import dataclass, field, replace
from typing import Sequence

import numpy as np

from core import knobs as knobs_repo
from core import motes as motes_repo
from core import relationships as rel_repo
from core import sweep as sweep_repo
from core import undo
from core import units as units_repo
from core.calc import FITTED, SOURCE_KEY
from core.command_bar import CommandError
from core.csv_import import IMPORTED_FROM
from core.plotcmd import _Garden
from core import uncertainty as uncertainty_repo
from core.uncertainty import UNCERTAINTY_KEY

FIT_VERB = "fit"
DATA_FLAG, FROM_FLAG, FREE_FLAG = "--data", "--from", "--free"
SCENARIO_FLAG, APPLY_FLAG = "--scenario", "--apply"
SET_FLAG = knobs_repo.SET_FLAG
FLAGS = (DATA_FLAG, FROM_FLAG, FREE_FLAG, SCENARIO_FLAG, SET_FLAG, APPLY_FLAG)
VS = "vs"

MAX_ITERATIONS = 50
TOLERANCE = 1e-10       # relative change in the sum of squares that counts as settled
STEP = 1e-6             # forward-difference step, relative to the input
CURVE_POINTS = 120      # the fitted curve a pane draws through the data
_SINGULAR = 1e12        # JᵀJ condition number past which inputs can't be told apart


def usage() -> str:
    return ("usage: fit ‹m.y› vs ‹m.x› --data ‹ykey› vs ‹xkey› [--from ‹import›] "
            "--free ‹m.k›… [--scenario ‹s›] [--set ‹m.k=value [unit]›] [--apply]")


# ---- the line -----------------------------------------------------------

@dataclass(frozen=True)
class FitLine:
    y: str = ""
    x: str = ""
    data_y: str = ""
    data_x: str = ""
    source: str | None = None
    free: tuple[str, ...] = ()
    scenario: str | None = None
    apply: bool = False
    # Knobs (core/knobs.py): the state the fit starts from. They are held
    # fixed -- a knob on a `--free` input is that input's starting value.
    sets: tuple[knobs_repo.KnobSpec, ...] = ()

    @property
    def empty(self) -> bool:
        return not self.y

    def text(self, apply: bool | None = None) -> str:
        if self.empty:
            return FIT_VERB
        q = shlex.quote
        parts = [FIT_VERB, q(self.y), VS, q(self.x), DATA_FLAG, q(self.data_y), VS, q(self.data_x)]
        if self.source:
            parts += [FROM_FLAG, q(self.source)]
        parts += [FREE_FLAG, *map(q, self.free)]
        if self.scenario:
            parts += [SCENARIO_FLAG, q(self.scenario)]
        parts += [q(word) for word in knobs_repo.words(self.sets)]
        if self.apply if apply is None else apply:
            parts.append(APPLY_FLAG)
        return " ".join(parts)


def parse(args: Sequence[str]) -> FitLine:
    """The tokens after `fit`. Raises CommandError with the usage."""
    args = list(args)
    if not args:
        return FitLine()
    fields: dict = {"free": [], "sets": []}
    head: list[str] = []
    i = 0
    while i < len(args):
        token = args[i]
        if token == APPLY_FLAG:
            fields["apply"] = True
        elif token == DATA_FLAG:
            if i + 3 >= len(args) or args[i + 2] != VS:
                raise CommandError(f"--data takes ‹ykey› vs ‹xkey› -- {usage()}")
            fields["data_y"], fields["data_x"] = args[i + 1], args[i + 3]
            i += 3
        elif token in (FROM_FLAG, SCENARIO_FLAG, SET_FLAG):
            if i + 1 >= len(args):
                raise CommandError(f"{token} needs a value -- {usage()}")
            i += 1
            if token == SET_FLAG:
                fields["sets"].append(knobs_repo.parse_set(args[i]))
            else:
                fields["source" if token == FROM_FLAG else "scenario"] = args[i]
        elif token == FREE_FLAG:
            while i + 1 < len(args) and not args[i + 1].startswith("--"):
                i += 1
                fields["free"].append(args[i])
        elif token.startswith("--"):
            raise CommandError(f"unknown flag {token} -- {usage()}")
        else:
            head.append(token)
        i += 1
    if len(head) != 3 or head[1] != VS:
        raise CommandError(f"fit what against what? -- {usage()}")
    if "data_y" not in fields:
        raise CommandError(f"fit needs --data ‹ykey› vs ‹xkey› -- {usage()}")
    if not fields["free"]:
        raise CommandError(f"fit needs at least one --free input -- {usage()}")
    return FitLine(
        y=head[0], x=head[2], data_y=fields["data_y"], data_x=fields["data_x"],
        source=fields.get("source"), free=tuple(dict.fromkeys(fields["free"])),
        scenario=fields.get("scenario"), apply=bool(fields.get("apply")),
        sets=tuple(fields["sets"]),
    )


def parse_text(command: str) -> FitLine:
    try:
        tokens = shlex.split(command or "")
    except ValueError as e:
        raise CommandError(f"fit: {e}") from None
    if not tokens or tokens[0] != FIT_VERB:
        raise CommandError(f"not a fit line: {command!r} -- {usage()}")
    return parse(tokens[1:])


# ---- the answer ---------------------------------------------------------

@dataclass(frozen=True)
class DataPoint:
    mote: str
    name: str
    x: float
    y: float
    # The data mote's own ± (core/uncertainty.py), in the model's units.
    # The fit ignores them; a plot's error bars draw them.
    sx: float | None = None
    sy: float | None = None


@dataclass(frozen=True)
class Param:
    ref: str
    mote: str
    key: str
    start: float
    value: float
    sigma: float | None
    unit: str | None = None


@dataclass(frozen=True)
class FitResult:
    line: FitLine
    params: tuple[Param, ...] = ()
    points: tuple[DataPoint, ...] = ()
    fitted: tuple[float, ...] = ()           # the model's y at each point, fitted
    skipped: tuple[tuple[str, str], ...] = ()  # (point name, why) left out
    curve: tuple[tuple[float, float | None], ...] = ()
    rms: float | None = None
    r2: float | None = None
    iterations: int = 0
    converged: bool = False
    solves: int = 0
    message: str = ""
    x_label: str = ""
    y_label: str = ""


# ---- running ------------------------------------------------------------

class _Model:
    """The garden read once, answering y at x for a set of free values."""

    def __init__(self, ground, scenario_id, x_ref, y_ref, free, base=None):
        self.ground = ground
        self.base = base or {}
        self.scenario = sweep_repo._scenario_of(ground, scenario_id)
        self.x = x_ref
        self.y = y_ref
        self.free = free
        self.solves = 0

    def at(self, values: Sequence[float], x: float) -> tuple[float | None, str | None]:
        overrides = knobs_repo.merged(self.base)
        for (mote, key), value in zip(self.free, values):
            overrides.setdefault(mote, {})[key] = float(value)
        point = {self.x[0]: {self.x[1]: float(x)}}
        self.solves += 1
        solved, error = sweep_repo._solve(self.ground, self.scenario, overrides, point, "linear", None)
        if solved is None:
            return None, error
        value = solved.value(*self.y)
        return (None, f"{self.y[1]} did not solve") if value is None else (float(value), None)

    def residuals(self, values, points) -> np.ndarray | None:
        out = []
        for p in points:
            y, _error = self.at(values, p.x)
            if y is None:
                return None
            out.append(y - p.y)
        return np.array(out)


def run(conn: sqlite3.Connection, line: FitLine) -> FitResult:
    """The fit `line` asks for. Writes nothing. Raises CommandError for a
    name, scenario or import the garden doesn't have."""
    return prepare(conn, line)()


def prepare(conn: sqlite3.Connection, line: FitLine):
    """Everything the fit reads from the garden, read now; returns the
    fit itself as a call that touches no connection -- so a caller can
    read under its lock and solve outside it."""
    if line.empty:
        raise CommandError(usage())
    garden = _Garden(conn)
    x_ref, y_ref = garden.ref(line.x), garden.ref(line.y)
    free = [garden.ref(ref) for ref in line.free]
    scenario_id = garden.scenario(line.scenario)
    points = _data(conn, garden, line, x_ref, y_ref)
    tried = knobs_repo.overrides(knobs_repo.resolve(garden, line.sets))
    starts = []
    for ref, (mote, key) in zip(line.free, free):
        start = tried.get(mote, {}).get(key, garden.value(mote, key))
        if start is None:
            raise CommandError(f"{ref} has no number to start the fit from -- give it one first")
        starts.append(start)
    model = _Model(sweep_repo.read_garden(conn, [scenario_id] if scenario_id else []),
                   scenario_id, x_ref, y_ref, free, tried)
    units = [garden.unit(mote, key) for mote, key in free]
    return lambda: _fit(line, units, model, points, starts, free)


def _data(conn, garden: _Garden, line: FitLine, x_ref, y_ref) -> list[DataPoint]:
    """Every mote carrying both data keys as numbers, converted into the
    model's units where both sides declare one -- the model's own motes
    excepted, and narrowed to one import's rows with --from."""
    allowed = None
    if line.source:
        imports = [m for m in garden.motes if m.properties.get("name") == line.source]
        if not imports:
            raise CommandError(f"no import named {line.source!r}")
        ids = {m.id for m in imports}
        allowed = {r.from_id for r in rel_repo.list_relationships(conn, type_=IMPORTED_FROM) if r.to_id in ids}
    x_unit, y_unit = garden.unit(*x_ref), garden.unit(*y_ref)
    points = []
    for mote in garden.motes:
        if mote.id in (x_ref[0], y_ref[0]) or (allowed is not None and mote.id not in allowed):
            continue
        x, y = garden.value(mote.id, line.data_x), garden.value(mote.id, line.data_y)
        if x is None or y is None:
            continue
        units = units_repo.parse_units_property(mote.properties.get(units_repo.UNITS_KEY))
        try:
            x = _convert(x, units.get(line.data_x), x_unit)
            y = _convert(y, units.get(line.data_y), y_unit)
        except units_repo.UnitError as e:
            raise CommandError(f"{mote.properties.get('name', mote.id[:8])}: {e}") from None
        name = mote.properties.get("name")
        sx = _scaled_sigma(mote.properties, line.data_x, units.get(line.data_x), x_unit)
        sy = _scaled_sigma(mote.properties, line.data_y, units.get(line.data_y), y_unit)
        points.append(DataPoint(mote.id, name if isinstance(name, str) else mote.id[:8], x, y, sx, sy))
    if not points:
        where = f" in {line.source!r}" if line.source else ""
        raise CommandError(f"no mote{where} carries both {line.data_y!r} and {line.data_x!r}")
    return sorted(points, key=lambda p: p.x)


def _scaled_sigma(properties: dict, key: str, have: str | None, want: str | None) -> float | None:
    """A property's ±, carried into the model's unit as a *difference*
    (so an offset unit like degC does not shift it)."""
    sigma = uncertainty_repo.sigma_of(properties, key)
    if not sigma:
        return None
    value = properties.get(key)
    if not have or not want or have == want or not isinstance(value, (int, float)):
        return float(sigma)
    return abs(_convert(float(value) + sigma, have, want) - _convert(float(value), have, want))


def _convert(value: float, have: str | None, want: str | None) -> float:
    if have and want and have != want:
        return units_repo.convert(value, have, want)
    return value


def _fit(line, units, model: _Model, points, starts, free) -> FitResult:
    values = np.array(starts, dtype=float)
    # Points the model can't solve even at the start are left out, and said.
    usable, skipped = [], []
    for p in points:
        y, error = model.at(values, p.x)
        (usable.append(p) if y is not None else skipped.append((p.name, error or "did not solve")))
    common = dict(line=line, points=tuple(points), skipped=tuple(skipped),
                  x_label=line.x, y_label=line.y)
    if len(usable) < len(values):
        return FitResult(**common, params=_params(line, units, free, starts, starts, None),
                         solves=model.solves,
                         message=f"{len(usable)} usable point(s) for {len(values)} free input(s) -- "
                                 f"a fit needs at least as many points as inputs")

    r = model.residuals(values, usable)
    sse = float(r @ r)
    lam, converged, iterations = 1e-3, False, 0
    jacobian = None
    while iterations < MAX_ITERATIONS:
        iterations += 1
        jacobian = _jacobian(model, values, usable, r)
        if jacobian is None:
            break
        settled = False
        jtj, jtr = jacobian.T @ jacobian, jacobian.T @ r
        improved = False
        while lam < 1e12:
            damped = jtj + lam * np.diag(np.diag(jtj) + 1e-30)
            step = np.linalg.lstsq(damped, -jtr, rcond=None)[0]
            trial = values + step
            trial_r = model.residuals(trial, usable)
            if trial_r is not None and float(trial_r @ trial_r) <= sse:
                trial_sse = float(trial_r @ trial_r)
                settled = sse - trial_sse <= TOLERANCE * max(sse, 1e-300)
                values, r, sse = trial, trial_r, trial_sse
                lam = max(lam / 3, 1e-12)
                improved = True
                break
            lam *= 4
        # No step that improves (the damping ran out) means a minimum to
        # the solver's precision -- as settled as a shrinking step is.
        if not improved or settled or sse == 0.0:
            converged = True
            break

    sigmas, message = _sigmas(jacobian, sse, len(usable), len(values), line)
    ys = np.array([p.y for p in usable])
    sst = float(((ys - ys.mean()) ** 2).sum())
    fitted = tuple(float(p.y + ri) for p, ri in zip(usable, r))
    return FitResult(
        **common, params=_params(line, units, free, starts, list(values), sigmas),
        fitted=fitted, curve=_curve(model, values, points),
        rms=math.sqrt(sse / len(usable)), r2=(1 - sse / sst) if sst > 0 else None,
        iterations=iterations, converged=converged, solves=model.solves,
        message=message or ("" if converged else f"did not settle in {MAX_ITERATIONS} iterations"),
    )


def _jacobian(model: _Model, values, points, r) -> np.ndarray | None:
    columns = []
    for j, value in enumerate(values):
        h = STEP * max(abs(value), 1e-3)
        moved = values.copy()
        moved[j] += h
        moved_r = model.residuals(moved, points)
        if moved_r is None:
            return None
        columns.append((moved_r - r) / h)
    return np.array(columns).T


def _sigmas(jacobian, sse, n, m, line: FitLine):
    if jacobian is None:
        return None, "a free input moved the model somewhere it can't solve"
    jtj = jacobian.T @ jacobian
    if np.linalg.cond(jtj) > _SINGULAR:
        return None, (f"the data can't tell {', '.join(line.free)} apart -- "
                      f"free fewer inputs (no σ given)")
    if n <= m:
        return None, "as many points as inputs: an exact fit, with no σ"
    covariance = np.linalg.inv(jtj) * (sse / (n - m))
    return [math.sqrt(max(v, 0.0)) for v in np.diag(covariance)], ""


def _params(line, units, free, starts, values, sigmas) -> tuple[Param, ...]:
    return tuple(
        Param(ref, mote, key, float(start), float(value),
              None if sigmas is None else float(sigmas[i]), units[i])
        for i, (ref, (mote, key), start, value) in enumerate(zip(line.free, free, starts, values))
    )


def _curve(model: _Model, values, points) -> tuple:
    lo, hi = min(p.x for p in points), max(p.x for p in points)
    if lo == hi:
        return ((lo, model.at(values, lo)[0]),)
    xs = [lo + (hi - lo) * i / (CURVE_POINTS - 1) for i in range(CURVE_POINTS)]
    return tuple((x, model.at(values, x)[0]) for x in xs)


# ---- the one write ------------------------------------------------------

def apply(conn: sqlite3.Connection, result: FitResult) -> tuple[str, undo.Command]:
    """Write each fitted value, its σ (into `uncertainty`) and `source:
    {kind: fitted, note: ‹the line›}` -- one undo step. Refuses a fit that
    did not produce values (too few points, a model that failed)."""
    if not result.params or result.rms is None:
        raise CommandError(result.message or "nothing was fitted, so nothing to apply")
    note = result.line.text(apply=False)
    steps = []
    for param in result.params:
        old = motes_repo.get_mote(conn, param.mote).properties
        updates: dict = {param.key: param.value}
        source = dict(old.get(SOURCE_KEY) or {}) if isinstance(old.get(SOURCE_KEY), dict) else {}
        source[param.key] = {"kind": FITTED, "note": note}
        updates[SOURCE_KEY] = source
        spread = dict(old.get(UNCERTAINTY_KEY) or {}) if isinstance(old.get(UNCERTAINTY_KEY), dict) else {}
        if param.sigma is not None:
            spread[param.key] = param.sigma
        else:
            spread.pop(param.key, None)
        updates[UNCERTAINTY_KEY] = spread
        before = {k: old[k] for k in updates if k in old}
        motes_repo.update_mote(conn, param.mote, updates)
        steps.append(undo.for_update_mote(conn, param.mote, before, updates, label="fit"))
    names = ", ".join(f"{p.ref} = {p.value:.6g}" for p in result.params)
    return f"fitted {names}", undo.combine("fit", steps)


# ---- completion ---------------------------------------------------------

def choices(conn: sqlite3.Connection, rest: str) -> list:
    """Refs where a model value goes, keys after --data, import names
    after --from, flags after a `-`."""
    from core.plotcmd import Choice, _refs
    words = rest.split()
    partial = "" if not rest or rest[-1].isspace() else words[-1]
    done = words if not partial else words[:-1]
    before = done[-1] if done else None
    if partial.startswith("-"):
        return [Choice(flag) for flag in FLAGS]
    if before == FROM_FLAG:
        names = {rel.to_id for rel in rel_repo.list_relationships(conn, type_=IMPORTED_FROM)}
        return [Choice(m.properties["name"], "import") for m in motes_repo.get_motes(conn, list(names))
                if isinstance(m.properties.get("name"), str)]
    if before == DATA_FLAG or (len(done) >= 2 and done[-2] == DATA_FLAG and before == VS):
        keys = sorted({k for m in motes_repo.list_motes(conn) for k, v in m.properties.items()
                       if isinstance(v, (int, float)) and not isinstance(v, bool)})
        return [Choice(k) for k in keys]
    if len(done) == 1:
        return [Choice(VS)]
    return _refs(conn)

# core/calculus.py
"""Definite integrals and derivatives of a function, numerically.

`integrate(phi, 0, R)` and `deriv(phi, x0)` in a formula ask about a
function mote along one of its parameters (core/formula.py samples the
body; docs/simulation.md -> *Calculus*). The answer is a numeric estimate,
so every one a solve takes is recorded -- `why` and `\\work` show it -- and
an estimate that misses its tolerance is an error rather than a quiet
number. Pure numerics, like core/limits.py: nothing here knows about
formulas, so it takes a plain `float -> float`.

Integrals: adaptive Gauss-Kronrod (7-point Gauss inside 15-point Kronrod),
bisecting the interval whose estimate is worst until the whole is within
tolerance. Kronrod nodes never sit on an interval's ends, so an integrand
undefined at a bound (sin(r)/r from 0) is integrated without asking it
there.

Derivatives: Ridders' method -- central differences at a step shrinking
by a constant factor, Richardson-extrapolated, keeping the estimate whose
error is smallest. A central difference is blind to a symmetric kink
(|x| at 0 gives exactly 0), so the one-sided quotients are compared at two
steps as well: on a smooth function their gap shrinks with the step, and
on a kink it does not.
"""
import heapq
import math
from collections.abc import Callable
from dataclasses import dataclass

# 15-point Kronrod nodes on [-1, 1] (the positive half; the last is the
# centre) and weights, with the embedded 7-point Gauss weights on every
# other node. QUADPACK's qk15.
_KRONROD_NODES = (
    0.991455371120812639206854697526329, 0.949107912342758524526189684047851,
    0.864864423359769072789712788640926, 0.741531185599394439863864773280788,
    0.586087235467691130294144845693013, 0.405845151377397166906606412076961,
    0.207784955007898467600689403773245, 0.0,
)
_KRONROD_WEIGHTS = (
    0.022935322010529224963732008058970, 0.063092092629978553290700663189204,
    0.104790010322250183839876322541518, 0.140653259715525918745189590510238,
    0.169004726639267902826583426598550, 0.190350578064785409913256402421014,
    0.204432940075298892414161999234649, 0.209482141084727828012999174891714,
)
_GAUSS_WEIGHTS = (
    0.129484966168869693270611432679082, 0.279705391489276667901467771423780,
    0.381830050505118944950369775488975, 0.417959183673469387755102040816327,
)

# Asked of an integral: relative to its size, with a floor relative to the
# integral of |f|, so one that cancels to zero (sin over a full period)
# settles once what is left is negligible beside what cancelled.
_RELATIVE = 1e-10
_CANCELLED = 1e-12
_MAX_INTERVALS = 200
# An estimate's relative error below this is float roundoff, not worth
# saying.
_ROUNDOFF = 1e-14
# A piece this narrow, relative to where it is, is at float resolution.
_NARROWEST = 1e-12

# Ridders: the step shrinks by _SHRINK per round, for at most _ROUNDS
# rounds, stopping early once a round's best estimate is _SAFE times worse
# than the best so far (float cancellation has taken over).
_SHRINK = 1.4
_ROUNDS = 10
_SAFE = 2.0
# Asked of a derivative: its error estimate against its size, where a
# derivative of 0 is sized by _CANCELLATION of |f| / step.
_DERIVATIVE_TOLERANCE = 1e-6
_CANCELLATION = 1e-3
# A one-sided gap that has not fallen below this fraction when the step
# fell a hundredfold is a kink, not curvature.
_KINK_STEP = 1e-2
_KINK_RATIO = 0.1


class CalculusError(ValueError):
    """No estimate within tolerance: a pole, a kink, a function undefined
    beside the point, or one that never settles."""


@dataclass(frozen=True)
class Estimate:
    value: float
    error: float


@dataclass(frozen=True)
class Integral:
    """One integral a solve took: `function` along `parameter` from
    `shown_lower` to `shown_upper` (the bounds as the parameter's unit
    writes them) is `value`, in base SI, to within `error`. `held` shows
    the other parameters' values (`x = 2 cm`), empty for a function of one
    -- it is part of what was asked, so two integrals of one function at
    different `x` are two answers."""
    function_id: str
    function: str
    parameter: str
    shown_lower: str
    shown_upper: str
    value: float
    error: float
    held: str = ""

    @property
    def approach(self) -> str:
        return _with_held(f"∫ {self.parameter} {self.shown_lower} → {self.shown_upper}", self.held)


@dataclass(frozen=True)
class Derivative:
    """One derivative a solve took: d`function`/d`parameter` at
    `shown_point` is `value`, in base SI, to within `error`, with the other
    parameters at `held` as Integral's are."""
    function_id: str
    function: str
    parameter: str
    shown_point: str
    value: float
    error: float
    held: str = ""

    @property
    def approach(self) -> str:
        return _with_held(f"d/d{self.parameter} at {self.shown_point}", self.held)


def reported(record: "Integral | Derivative") -> str:
    """What was asked and how close the answer is, for a reader: the
    approach and the error estimate, relative to the value unless the
    value is 0 -- `∫ r 0 → 5 cm ± 2e-11 relative`. An error at float
    roundoff (a polynomial, integrated exactly) is not worth stating, so
    that estimate is just the approach."""
    relative = record.error / abs(record.value) if record.value else None
    if not record.error or (relative is not None and relative < _ROUNDOFF):
        return record.approach
    if relative is not None:
        return f"{record.approach} ± {relative:.0e} relative"
    return f"{record.approach} ± {record.error:.0e}"


def _with_held(approach: str, held: str) -> str:
    return f"{approach}, {held}" if held else approach


def first_step(point: float, unit_scale: float | None) -> float:
    """Where Ridders starts. A declared unit says what size of step is
    small for this quantity (a tenth of a cm, for `r [cm]`); without one,
    a tenth of the point's own size, or of 1."""
    if unit_scale:
        return 0.1 * abs(unit_scale)
    return 0.1 * max(abs(point), 1.0)


def integrate(f: Callable[[float], float], lower: float, upper: float) -> Estimate:
    """∫ f from `lower` to `upper`. Reversed bounds give the negated
    integral, and equal ones 0. Raises CalculusError when the estimate does
    not reach tolerance within _MAX_INTERVALS pieces."""
    if lower == upper:
        return Estimate(0.0, 0.0)
    if lower > upper:
        flipped = integrate(f, upper, lower)
        return Estimate(-flipped.value, flipped.error)
    first = _kronrod(f, lower, upper)
    pieces = [(-first[1], lower, upper, first)]
    while True:
        value = math.fsum(p[3][0] for p in pieces)
        error = math.fsum(p[3][1] for p in pieces)
        size = math.fsum(p[3][2] for p in pieces)
        if error <= max(_RELATIVE * abs(value), _CANCELLED * size):
            return Estimate(value, error)
        if len(pieces) >= _MAX_INTERVALS:
            raise CalculusError(
                f"the integral does not settle: error {error:.3g} on {value:.6g} after "
                f"{len(pieces)} pieces -- a pole or a jump inside the bounds?")
        _bisect_worst(f, pieces)


def _bisect_worst(f: Callable[[float], float], pieces: list) -> None:
    """Split the piece whose error is largest. One too narrow to split
    further (a pole pulls every bisection into itself) is where the
    integral fails to settle."""
    _, a, b, _ = heapq.heappop(pieces)
    middle = (a + b) / 2.0
    if b - a <= _NARROWEST * max(abs(a), abs(b)):
        raise CalculusError(
            f"the integral does not settle near {middle:.6g} -- a pole there?")
    for left, right in ((a, middle), (middle, b)):
        estimate = _kronrod(f, left, right)
        heapq.heappush(pieces, (-estimate[1], left, right, estimate))


def _kronrod(f: Callable[[float], float], a: float, b: float) -> tuple[float, float, float]:
    """The 15-point estimate of ∫ f over [a, b], its error (the gap to the
    embedded 7-point Gauss estimate), and the 15-point estimate of ∫ |f|."""
    centre, half = (a + b) / 2.0, (b - a) / 2.0
    kronrod = gauss = absolute = 0.0
    for i, (node, weight) in enumerate(zip(_KRONROD_NODES, _KRONROD_WEIGHTS)):
        samples = (_sample(f, centre),) if node == 0.0 else (
            _sample(f, centre - half * node), _sample(f, centre + half * node))
        kronrod += weight * sum(samples)
        absolute += weight * sum(abs(s) for s in samples)
        if i % 2 == 1:
            gauss += _GAUSS_WEIGHTS[i // 2] * sum(samples)
    return kronrod * half, abs(kronrod - gauss) * abs(half), absolute * abs(half)


def derivative(f: Callable[[float], float], point: float, step: float) -> Estimate:
    """f'(point), starting from `step`. Raises CalculusError on a kink, a
    function undefined beside the point, or an estimate whose error is not
    small beside its size.

    A first step can be too coarse for the function (sin(1e4 x) turns over
    many times in a tenth of a unit), so an estimate that does not settle
    is tried again from a step a thousand and a million times smaller, as
    core/limits.py's two_sided is. A kink is a kink at any step."""
    failure: CalculusError | None = None
    for start in (step, step * 1e-3, step * 1e-6):
        try:
            return _derivative_from(f, point, start)
        except _Unsettled as e:
            failure = failure or e
    raise failure


class _Unsettled(CalculusError):
    """An estimate that did not reach tolerance from one starting step."""


def _derivative_from(f: Callable[[float], float], point: float, step: float) -> Estimate:
    value, error = _ridders(f, point, step)
    centre = _sample(f, point)
    # The derivative's size, floored for one that is 0: float cancellation
    # in a difference quotient is relative to |f| / step, so beside that a
    # derivative's error can be made no smaller.
    size = max(abs(value), _CANCELLATION * max(abs(centre), abs(_sample(f, point + step))) / step)
    tolerance = _DERIVATIVE_TOLERANCE * size
    if error > tolerance:
        raise _Unsettled(f"the derivative does not settle: {value:.6g} ± {error:.3g}")
    small = step * _KINK_STEP
    if _one_sided_gap(f, point, centre, small) > max(
            _KINK_RATIO * _one_sided_gap(f, point, centre, step), tolerance):
        raise CalculusError(
            f"no derivative: from the left {_quotient(f, point, centre, -small):.6g}, "
            f"from the right {_quotient(f, point, centre, small):.6g}")
    return Estimate(value, error)


def _ridders(f: Callable[[float], float], point: float, step: float) -> tuple[float, float]:
    """Richardson's tableau over central differences, one column per
    step. The best (value, error) any entry reached."""
    best, best_error = 0.0, math.inf
    previous: list[float] = []
    h = step
    for _ in range(_ROUNDS):
        row = [(_sample(f, point + h) - _sample(f, point - h)) / (2.0 * h)]
        factor = _SHRINK ** 2
        for j, earlier in enumerate(previous, start=1):
            row.append((row[j - 1] * factor - earlier) / (factor - 1.0))
            factor *= _SHRINK ** 2
            error = max(abs(row[j] - row[j - 1]), abs(row[j] - previous[j - 1]))
            if error <= best_error:
                best, best_error = row[j], error
        if previous and abs(row[-1] - previous[-1]) >= _SAFE * best_error:
            break
        previous = row
        h /= _SHRINK
    return best, best_error


def _one_sided_gap(f: Callable[[float], float], point: float, centre: float, h: float) -> float:
    return abs(_quotient(f, point, centre, h) - _quotient(f, point, centre, -h))


def _quotient(f: Callable[[float], float], point: float, centre: float, h: float) -> float:
    return (_sample(f, point + h) - centre) / h


def _sample(f: Callable[[float], float], x: float) -> float:
    value = f(x)
    if not math.isfinite(value):
        raise CalculusError(f"the function is not finite at {x:.6g} ({value})")
    return value

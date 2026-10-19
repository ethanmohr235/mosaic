# core/limits.py
"""The value a function approaches where its body is 0/0.

`sin(B r)/r` is 0/0 at r = 0, and the number anyone means there is the
limit, B. At a function call (core/formula.py::_function_call) the
parameter is the variable to approach along, so a call that lands exactly
on 0/0 is answered with a two-sided numeric limit instead of an error --
and **says so**: every limit taken is a `Limit` the solve reports, and
`\\work` and `why` show it. A plain rule that hits 0/0 has no such
variable, so it stays an error (docs/simulation.md -> *Limits*).

The method: approach from each side in steps that halve, extrapolate the
sequence to a zero step (Richardson, assuming the error is a power series
in the step, which holds for the removable singularities this is for),
and accept only when that converges on each side and the two sides agree.
A pole or a jump fails loudly. Pure numerics: nothing here knows about
formulas, so it takes a plain `float -> float`.
"""
import math
from collections.abc import Callable
from dataclasses import dataclass

# Rounds of halving. Twelve takes the step to h0/4096: far enough to
# extrapolate well, not so far that the difference quotient drowns in
# float cancellation.
_ROUNDS = 12
# Relative agreement asked of successive extrapolations on one side, and of
# the two sides with each other.
_CONVERGED = 1e-8
_SIDES_AGREE = 1e-6


class LimitError(ValueError):
    """No finite two-sided limit."""


@dataclass(frozen=True)
class Limit:
    """One limit a solve took: `function(parameter -> point)` is `value`.
    `point` and `value` are in base SI; `shown_point` is the point as the
    parameter's declared unit writes it, for a reader."""
    function_id: str
    function: str
    parameter: str
    point: float
    shown_point: str
    value: float

    @property
    def key(self) -> tuple[str, str, float]:
        return (self.function_id, self.parameter, self.point)

    @property
    def approach(self) -> str:
        return f"{self.parameter} → {self.shown_point}"

    @property
    def label(self) -> str:
        return f"{self.function}: limit {self.approach}"


def first_step(point: float, unit_scale: float | None) -> float:
    """Where to start approaching from. A declared unit says what size of
    step is small for this quantity (a thousandth of a cm, for `r [cm]`);
    without one, a thousandth of the point's own size, or of 1."""
    if unit_scale:
        return 1e-3 * abs(unit_scale)
    return 1e-3 * max(abs(point), 1.0)


def two_sided(f: Callable[[float], float], point: float, step: float) -> float:
    """lim f(x) as x -> point, from both sides. Raises LimitError when a
    side does not converge or the two sides disagree.

    A first step can be too coarse for the function (sin(1e9 x)/x has not
    started to settle a thousandth of a unit away), so a side that does
    not settle is tried again from a step a thousand and a million times
    smaller before the answer is "no limit". Two sides that settle and
    disagree are a jump, and no smaller step changes that."""
    failure: LimitError | None = None
    for start in (step, step * 1e-3, step * 1e-6):
        try:
            right, right_scale = _one_sided(f, point, start)
            left, left_scale = _one_sided(f, point, -start)
        except LimitError as e:
            failure = failure or e
            continue
        if not _close(left, right, max(left_scale, right_scale), _SIDES_AGREE):
            raise LimitError(f"no limit: from the left {left:.6g}, from the right {right:.6g}")
        return (left + right) / 2.0
    raise failure


def _one_sided(f: Callable[[float], float], point: float, step: float) -> tuple[float, float]:
    """Richardson extrapolation of f(point + step / 2^k) to a zero step,
    and the size of f near the point (what "close to zero" is measured
    against when the limit itself is 0). The diagonal of the tableau is
    the sequence of ever-better estimates; the first pair that agrees is
    the answer."""
    previous_row: list[float] = []
    scale = 0.0
    for k in range(_ROUNDS + 1):
        row = [_sample(f, point + step / 2 ** k)]
        scale = max(scale, abs(row[0]))
        for j, earlier in enumerate(previous_row, start=1):
            row.append(row[j - 1] + (row[j - 1] - earlier) / (2 ** j - 1))
        if previous_row and _close(row[-1], previous_row[-1], scale, _CONVERGED):
            return row[-1], scale
        previous_row = row
    raise LimitError(
        f"no finite limit: approaching from the {'right' if step > 0 else 'left'} "
        f"does not settle (last estimate {previous_row[-1]:.6g})")


def _close(a: float, b: float, scale: float, tolerance: float) -> bool:
    """Relative agreement, with an absolute floor at `tolerance` of the
    function's size near the point -- so a limit of exactly 0, which no
    relative test can ever agree on, settles once it is negligible beside
    the values around it."""
    return abs(a - b) <= tolerance * max(abs(a), abs(b), scale)


def _sample(f: Callable[[float], float], x: float) -> float:
    value = f(x)
    if not math.isfinite(value):
        raise LimitError(f"no finite limit: the value near the point is {value}")
    return value

# core/formula.py
"""Restricted expression language for simulation rules.

Expressions are parsed with the stdlib `ast` module and walked against a
strict node whitelist -- never `eval()`. Evaluation produces a Ratio: one
LinearExpr (a weighted sum of unknowns plus a constant) over another, so
an expression that happens to contain no unknowns is simply a number.
That means one evaluator serves both assignment-style rules and coupled
equations.

The denominator is what makes an equation accept the algebra a modeller
actually writes rather than the arrangement the solver wants. `I / I0 ==
0.1` is not linear as written -- an unknown sits under a division -- but
it is *equivalent* to one, and rearranging it by hand (`I == 0.1 * I0`)
is bookkeeping the engine can do itself. Carrying numerator and
denominator separately all the way to the relation, then clearing the
denominator once at the end (see `_clear_denominator`), recovers the
linear form for every equation that has one, and leaves the genuinely
nonlinear ones (`x * y == 1`) to defer to a solver backend exactly as
before.
"""
import ast
import dataclasses
import math
from collections.abc import Callable
from dataclasses import dataclass, field

from core import functions as functions_repo
from core import limits as limits_repo
from core import roles as roles_repo
from core.units import UnitError, from_base, to_base

# An unknown the solver may determine: which mote, which property key.
Unknown = tuple[str, str]

_TOLERANCE = 1e-9

# The callable names a formula may use, grouped by how _call() dispatches
# them. Hoisted into module constants rather than written inline because
# this whitelist *is* the security boundary, and core/dimensions.py walks
# the same grammar to check units -- two copies of the list would let a
# builtin get added here and silently ignored there. See
# tests/test_dimensions.py's drift test, which asserts the two agree.
NAVIGATION_CALLS = frozenset({"targets", "sources"})
AGGREGATE_CALLS = frozenset({"sum", "count"})
NUMERIC_CALLS = frozenset({"abs", "min", "max"})
# sin/cos/tan take radians, matching math.sin() -- a value with no declared
# unit already *is* radians (see units.py's `deg`), and a value declared
# `deg` arrives here already converted to radians by the time a formula
# reads it, the same normalization every other unit gets. arcsin/arccos/
# arctan are their inverses: dimensionless in, radians out -- which is
# still dimensionless in this system, the same way sin/cos/tan's own
# output is. `sinc` is the unnormalized sin(x)/x with sinc(0) = 1 -- the
# physics convention, not numpy's sin(pi x)/(pi x) -- so a profile like
# sin(B r)/r can be written `B * sinc(B * r)` and read at r = 0.
TRIG_CALLS = frozenset({"sin", "cos", "tan", "arcsin", "arccos", "arctan", "sinc"})
# `log` is the natural logarithm, matching math.log(), numpy, and every
# other scientific-computing default -- the same "follow the stdlib"
# choice TRIG_CALLS makes about radians. `ln` is an alias for anyone who
# wants to be explicit about it, and `log10` is there so nobody has to
# rely on guessing which one `log` meant.
LOG_CALLS = frozenset({"log", "ln", "log10", "exp"})
SQRT_CALL = "sqrt"
PREVIOUS_CALL = "prev"
PROPERTY_CALL = "property"
ALLOWED_CALLS = (
    NAVIGATION_CALLS | AGGREGATE_CALLS | NUMERIC_CALLS | TRIG_CALLS | LOG_CALLS
    | {SQRT_CALL, PREVIOUS_CALL, PROPERTY_CALL}
)

# The current simulation step's index, exposed to formulas as a reserved
# bare name rather than a per-mote property.
TICK_NAME = "t"

# Common math constants, exposed as reserved bare names the same way
# TICK_NAME is -- a formula writes `2 * pi * radius` rather than pasting
# in a truncated decimal. Shadows a property of the same name inside a
# formula only, exactly like `t`: real-world quantities named `e`
# (elasticity, voltage) or `pi` are vanishingly unlikely, and a fixed,
# well-known handful of names is worth the same small shadowing risk `t`
# already accepts, rather than a per-garden opt-in nobody would find.
MATH_CONSTANTS: dict[str, float] = {"pi": math.pi, "e": math.e}


def function_name_problem(name: object) -> str | None:
    """Why `name` cannot name a function mote, or None when it can. A call
    to a builtin never reaches a function mote, and `t`, `pi` and `e` are
    bare names a formula already reads, so a function spelled like any of
    them could never be called."""
    if not isinstance(name, str) or not name.isidentifier():
        return f"a function's name must be a plain identifier, not {name!r}"
    if name in ALLOWED_CALLS:
        return f"{name!r} is a builtin, so a function of that name could never be called"
    if name == TICK_NAME or name in MATH_CONSTANTS:
        return f"{name!r} is a reserved name in a formula"
    return None


class FormulaError(ValueError):
    """A formula could not be parsed or evaluated."""


class IndeterminateError(FormulaError):
    """0/0: both sides of a division are exactly zero. Distinct from a
    plain division by zero (a pole), because at a function call this is
    the one failure a limit can answer (see core/limits.py)."""


def _divide_by_zero(numerator_is_zero: bool) -> FormulaError:
    if numerator_is_zero:
        return IndeterminateError(
            "0/0 is indeterminate -- if the value is a limit, write the "
            "expression as a function (the solver takes the limit at a call) "
            "or use sinc()"
        )
    return FormulaError("division by zero")


class NonlinearError(FormulaError):
    """A formula is nonlinear in its unknowns, so it can't be solved as a
    linear system. Raised lazily so the substitution loop can retry once
    the offending unknowns have been determined by other equations."""


@dataclass(frozen=True)
class LinearExpr:
    """`sum(coefficient * unknown) + constant`.

    The arithmetic operators enforce linearity structurally: there is no
    way to build a LinearExpr that multiplies two unknowns together.

    `scale` is the largest magnitude any constant has taken anywhere
    during this expression's construction -- not just the final
    `constant`, which may have cancelled back down to something small.
    A rule mixing base units far apart (kW against MeV, say) routinely
    produces intermediate values around 1e15; float64 has only ~16
    significant digits, so two mathematically-equal ways of reaching
    that same value can differ by more than an absolute epsilon ever
    anticipates. `scale` lets a consistency check size its tolerance to
    the computation instead of guessing a single constant for every
    magnitude (see core/linear_solver.py's `_check_consistency`)."""
    terms: dict[Unknown, float] = field(default_factory=dict)
    constant: float = 0.0
    scale: float = 0.0

    @staticmethod
    def number(value: float) -> "LinearExpr":
        value = float(value)
        return LinearExpr({}, value, abs(value))

    @staticmethod
    def unknown(key: Unknown) -> "LinearExpr":
        return LinearExpr({key: 1.0}, 0.0, 0.0)

    @property
    def is_constant(self) -> bool:
        return not self.terms

    def value(self) -> float:
        if not self.is_constant:
            raise NonlinearError(f"expected a number, got unknowns: {describe(self.terms)}")
        return self.constant

    def __add__(self, other: "LinearExpr") -> "LinearExpr":
        terms = dict(self.terms)
        for key, coefficient in other.terms.items():
            terms[key] = terms.get(key, 0.0) + coefficient
        constant = self.constant + other.constant
        scale = max(self.scale, other.scale, abs(constant))
        return LinearExpr(_prune(terms), constant, scale)

    def __sub__(self, other: "LinearExpr") -> "LinearExpr":
        return self + (-other)

    def __neg__(self) -> "LinearExpr":
        return LinearExpr({k: -v for k, v in self.terms.items()}, -self.constant, self.scale)

    def __mul__(self, other: "LinearExpr") -> "LinearExpr":
        if self.is_constant:
            return other.scaled(self.constant)
        if other.is_constant:
            return self.scaled(other.constant)
        raise NonlinearError(
            f"cannot multiply two unknown quantities: "
            f"{describe(self.terms)} * {describe(other.terms)}"
        )

    def __truediv__(self, other: "LinearExpr") -> "LinearExpr":
        if not other.is_constant:
            raise NonlinearError(f"cannot divide by an unknown quantity: {describe(other.terms)}")
        # Exact zero, not `abs(x) < _TOLERANCE`: a divisor is a physical
        # value that may legitimately be tiny in SI base units (a neutron
        # mass is ~1.6749e-27 kg, an electron charge ~1.6e-19 C) without
        # being zero. `_TOLERANCE` exists to absorb float rounding noise
        # in *coefficients* built from small-integer arithmetic (see
        # `_prune`); applying that same absolute threshold to a divisor's
        # actual magnitude would reject real division by real quantities
        # that just happen to live at a subatomic scale. IEEE-754 divides
        # a merely-tiny float by another cleanly -- only true zero raises.
        if other.constant == 0.0:
            raise _divide_by_zero(self.is_constant and self.constant == 0.0)
        return self.scaled(1.0 / other.constant)

    def __pow__(self, other: "LinearExpr") -> "LinearExpr":
        # Which side holds the unknown decides what the modeller has to
        # change, so the message says which rather than blaming the base
        # for an exponent's unknown -- `e ** (-k)` fails on k, and being
        # told "an unknown quantity" sends someone looking at `e`.
        if not other.is_constant:
            raise NonlinearError(
                f"cannot raise to an unknown power: {describe(other.terms)}"
            )
        if not self.is_constant:
            raise NonlinearError(
                f"cannot raise an unknown quantity to a power: {describe(self.terms)}"
            )
        return LinearExpr.number(_power(self.constant, other.constant))

    def scaled(self, factor: float) -> "LinearExpr":
        # Exact zero, for the same reason as __truediv__ above: a factor
        # of e.g. 1.602176634e-19 (an eV-to-joule conversion) is a real,
        # tiny number, not float noise standing in for zero, and scaling
        # by it should shrink a term's coefficient rather than erase the
        # term (and the unknown along with it) from the expression.
        if factor == 0.0:
            return LinearExpr.number(0.0)
        constant = self.constant * factor
        scale = max(self.scale * abs(factor), abs(constant))
        return LinearExpr(
            {k: v * factor for k, v in self.terms.items()}, constant, scale
        )


def _power(base: float, exponent: float) -> float:
    """`base ** exponent`, with every way float exponentiation can fail
    turned into a FormulaError.

    There are three, and none of them reached this module's error
    handling before: OverflowError (an iterative backend probing `e **
    (-k * x)` walks x far enough negative to blow the float range -- see
    core/newton_solver.py), ZeroDivisionError (`0 ** -1`), and a
    *complex* result, which Python returns rather than raises for a
    negative base under a fractional exponent and which would otherwise
    flow on into a solver as a number that isn't one. `_log` has wrapped
    the same failures since it was written; this is that guard on the
    other numeric operator, so a bad power fails the run with a readable
    message instead of escaping as a raw arithmetic error."""
    try:
        result = base ** exponent
    except OverflowError as e:
        raise FormulaError(
            f"{base:g} ** {exponent:g} is too large to represent"
        ) from e
    except ZeroDivisionError as e:
        raise FormulaError(
            f"{base:g} ** {exponent:g}: zero to a negative power"
        ) from e
    if isinstance(result, complex):
        raise FormulaError(
            f"{base:g} ** {exponent:g} is not a real number -- a negative base "
            f"needs a whole-number exponent"
        )
    return result


def _one() -> "LinearExpr":
    return LinearExpr.number(1.0)


@dataclass(frozen=True)
class Ratio:
    """`numerator / denominator`, each a LinearExpr -- the value every
    node of an expression evaluates to.

    A plain linear expression is just one with a constant denominator,
    which is what almost every formula produces and what everything
    downstream of `evaluate_equation` still sees. The denominator only
    becomes interesting when an unknown is divided *by*: rather than
    failing there (the old behavior -- LinearExpr.__truediv__ still does,
    for anyone holding one directly), the division is carried along
    symbolically and cleared once, against the relation as a whole.

    The arithmetic keeps the same structural guarantee LinearExpr has:
    numerator and denominator are each linear, so nothing here can build
    a polynomial. `x * y` and `x / y * z` still raise NonlinearError from
    the underlying LinearExpr multiplication -- they are nonlinear in a
    way no rearrangement fixes, and belong to a solver backend.

    Cancelling a common factor (in `__mul__`, and in `__add__`'s matching
    denominators) assumes that factor is not zero, the same assumption
    clearing a denominator makes. Writing `a / x` already asserts x != 0;
    an equation whose numerator and denominator vanish together (`(x - 1)
    / (x - 1) == 1`) is degenerate as written, and the engine reads it as
    the cleared form rather than refusing it.
    """
    numerator: LinearExpr
    denominator: LinearExpr = field(default_factory=_one)

    @staticmethod
    def number(value: float) -> "Ratio":
        return Ratio(LinearExpr.number(value))

    @staticmethod
    def unknown(key: Unknown) -> "Ratio":
        return Ratio(LinearExpr.unknown(key))

    @property
    def is_divided(self) -> bool:
        """Whether an unknown sits under the division bar -- the only case
        where this is more than a LinearExpr in a wrapper."""
        return not self.denominator.is_constant

    @property
    def is_constant(self) -> bool:
        return self.numerator.is_constant and not self.is_divided

    @property
    def constant(self) -> float:
        return self.linear().constant

    @property
    def terms(self) -> dict[Unknown, float]:
        return self.linear().terms

    def linear(self) -> LinearExpr:
        """This value as a plain linear expression, dividing through by a
        constant denominator. Raises NonlinearError when the denominator
        still holds an unknown -- a caller that wants a number cannot use
        one, and `_clear_denominator` is the one place that can."""
        if self.is_divided:
            raise NonlinearError(
                f"cannot divide by an unknown quantity: {describe(self.denominator.terms)}"
            )
        return self.numerator / self.denominator

    def value(self) -> float:
        return self.linear().value()

    def __add__(self, other: "Ratio") -> "Ratio":
        # Matching denominators add directly. Cross-multiplying them
        # instead would square the denominator and multiply the two
        # numerators by the *other's* denominator -- turning `a / x + b /
        # x`, which is perfectly linear once cleared, into a nonlinear
        # dead end.
        if self.denominator == other.denominator:
            return Ratio(self.numerator + other.numerator, self.denominator)
        return Ratio(
            self.numerator * other.denominator + other.numerator * self.denominator,
            self.denominator * other.denominator,
        )

    def __sub__(self, other: "Ratio") -> "Ratio":
        return self + (-other)

    def __neg__(self) -> "Ratio":
        return Ratio(-self.numerator, self.denominator)

    def __mul__(self, other: "Ratio") -> "Ratio":
        if self.denominator == other.numerator:
            return Ratio(self.numerator, other.denominator)
        if other.denominator == self.numerator:
            return Ratio(other.numerator, self.denominator)
        return Ratio(self.numerator * other.numerator, self.denominator * other.denominator)

    def __truediv__(self, other: "Ratio") -> "Ratio":
        if other.numerator.is_constant and other.numerator.constant == 0.0:
            raise _divide_by_zero(
                self.numerator.is_constant and self.numerator.constant == 0.0)
        return self * Ratio(other.denominator, other.numerator)

    def __pow__(self, other: "Ratio") -> "Ratio":
        """A constant to a constant power is a number. An unknown is only
        allowed the two exponents that stay within this algebra: `x^1`,
        and `x^-1`, which is the reciprocal the denominator exists for.
        Anything else (`x^2`) is genuinely nonlinear."""
        if not other.is_constant:
            raise NonlinearError(
                f"cannot raise to an unknown power: {_describe_ratio(other)}"
            )
        exponent = other.constant
        if self.is_constant:
            return Ratio.number(_power(self.constant, exponent))
        if exponent == 1.0:
            return self
        if exponent == -1.0:
            return Ratio.number(1.0) / self
        raise NonlinearError(
            f"cannot raise an unknown quantity to the power {exponent:g}: "
            f"{_describe_ratio(self)} -- only ** 1 and ** -1 stay linear"
        )


def _describe_ratio(ratio: "Ratio") -> str:
    """The unknowns anywhere in a Ratio, for an error message. Reads both
    sides of the bar rather than calling `linear()`, which would raise on
    a divided Ratio -- an error path must not fail while explaining an
    error."""
    unknowns = set(ratio.numerator.terms) | set(ratio.denominator.terms)
    return describe(unknowns)


def describe(unknowns) -> str:
    """Render unknowns for error messages -- full UUIDs are unreadable, and
    the first 8 characters are what the UI shows too."""
    return ", ".join(f"{mote_id[:8]}.{key}" for mote_id, key in sorted(unknowns))


def _prune(terms: dict[Unknown, float]) -> dict[Unknown, float]:
    """Drop terms whose coefficients cancelled out, so `is_constant` stays
    honest after e.g. `voltage - voltage`.

    Exact zero, not `abs(v) < _TOLERANCE`: a coefficient is a physical
    scale factor (an elementary charge is ~1.602e-19 in base units) that
    may legitimately be far smaller than `_TOLERANCE` without having
    cancelled to nothing. `voltage - voltage` cancels to exactly 0.0 in
    IEEE-754 regardless -- the same value subtracted from itself never
    leaves rounding residue -- so an absolute floor here buys no real
    cancellation-noise protection, and it was silently dropping an
    unknown (and its real coefficient) whenever a rule scaled it by a
    small unit-conversion constant."""
    return {k: v for k, v in terms.items() if v != 0.0}


# The comparisons a relation may use. `==` is an equation, which the
# solver may satisfy by choosing values; the rest are constraints, which
# it may only check. Both are `lhs - rhs <op> 0` after evaluation, so one
# evaluator serves both and there is no second expression language.
EQUALS = "=="
_COMPARISONS = {
    ast.Eq: EQUALS,
    ast.LtE: "<=",
    ast.GtE: ">=",
    ast.Lt: "<",
    ast.Gt: ">",
}


def _rewrite_caret(text: str) -> str:
    """Rewrite bare `^` to `**` before parsing.

    Everyone arriving from math notation, Excel, or units.py's own
    compound-unit grammar (`kg*m/s^2`) writes `^` for exponentiation.
    Python's grammar hands it to bitwise XOR instead -- confusing on its
    own ("bit XOR is not allowed"), and not fixable by swapping the
    operator *after* parsing: XOR binds looser than `*`/`/`, so `2*x^2`
    would already have parsed as `(2*x)^2` by then, not `2*(x**2)` as
    intended. Doing the rewrite in the source text, before parsing, gets
    `**`'s correct (tighter) precedence for free. `^` never appears
    legitimately inside a quoted string in this grammar (the only string
    literals are call arguments like `targets('contains')`), but the
    quote-tracking below skips them anyway rather than assuming it."""
    out = []
    quote = None
    for char in text:
        if quote:
            out.append(char)
            if char == quote:
                quote = None
        elif char in "'\"":
            quote = char
            out.append(char)
        elif char == "^":
            out.append("**")
        else:
            out.append(char)
    return "".join(out)


def _parse_ast(text: str) -> ast.AST:
    try:
        tree = ast.parse(_rewrite_caret(text).strip(), mode="eval")
    except SyntaxError as e:
        raise FormulaError(f"could not parse formula {text!r}: {e.msg}") from e
    return tree.body


@dataclass(frozen=True)
class Relation:
    """One parsed `lhs <op> rhs`, before either side is evaluated."""
    left: ast.AST
    right: ast.AST
    operator: str = EQUALS

    @property
    def is_equation(self) -> bool:
        return self.operator == EQUALS


@dataclass(frozen=True)
class Equation:
    """`sum(coefficient * unknown) + constant <op> 0`.

    Still called an Equation because that is what it is for every
    operator the solver ever sees: only `==` reaches a solver backend. A
    constraint carries the same shape so that checking one is reading the
    sign of a number the existing evaluator already produced, rather than
    a second kind of thing."""
    terms: dict[Unknown, float]
    constant: float
    source: str = ""
    operator: str = EQUALS
    scale: float = 0.0

    @property
    def is_equation(self) -> bool:
        return self.operator == EQUALS

    @staticmethod
    def from_difference(
        expr: LinearExpr, source: str = "", operator: str = EQUALS
    ) -> "Equation":
        return Equation(expr.terms, expr.constant, source, operator, expr.scale)


@dataclass(frozen=True)
class MoteSet:
    """Motes reached by navigating one relationship type."""
    mote_ids: list[str]
    label: str


@dataclass(frozen=True)
class TermList:
    """Several motes' values for one property. Only usable inside sum() or
    count() -- arithmetic on it is rejected, since collapsing many motes
    into one number silently is exactly the kind of guess that hides
    modelling mistakes."""
    values: list[Ratio]
    label: str


def _model_evaluation_unavailable(*_args, **_kwargs):
    raise FormulaError("model evaluation is not available in this context")


@dataclass(frozen=True)
class Context:
    """Everything a formula can see. Plain callables rather than a
    connection, so this module stays free of any database import and
    stays trivially testable with dicts.

    `tick` and `previous` exist for time-based simulation: `tick` is the
    current step's index within a run (exposed to formulas as the
    reserved name `t`), and `previous` is each mote's properties as they
    stood *entering* the current step (read via the `prev(x)` builtin).
    Both default to values that make time-based formulas simply
    unavailable rather than silently wrong, so every non-time-based
    caller (a plain one-shot evaluation, or an existing test) is
    unaffected.

    `units` gives each mote's declared key -> unit-name map, so a value
    read out of a mote can be normalized to its dimension's base unit
    before it enters the linear system. Everything downstream -- the
    substitution loop, both solver backends -- then works in one
    consistent set of units without knowing units exist at all. Defaults
    to declaring nothing, which is exactly today's behavior.

    `neighbors` lists the motes immediately connected to a given mote --
    any relationship type, either direction -- for connected-name
    references (`R1.current` reaching a wired-up mote literally named
    "R1", without a declared role). Defaults to nobody connected, which
    makes the feature simply absent for a caller that doesn't wire it up
    (core/modoc.py's `expression_context`, existing tests).

    `named` is the same question asked of the whole garden rather than of
    one mote's neighbors -- the last fallback, after roles and connected
    names (see _named_in_garden). Defaults to nothing, on the same terms
    as `neighbors`."""
    mote_id: str
    properties: Callable[[str], dict]
    unknown_keys: Callable[[str], set[str]]
    targets: Callable[[str, str], list[str]]
    sources: Callable[[str, str], list[str]]
    neighbors: Callable[[str], list[str]] = lambda mote_id: []
    named: Callable[[str], list[str]] = lambda name: []
    solved: dict[Unknown, float] = field(default_factory=dict)
    tick: float = 0.0
    previous: Callable[[str], dict] = lambda mote_id: {}
    evaluate_model: Callable[[str, str, str, list[float]], float] = _model_evaluation_unavailable
    units: Callable[[str], dict] = lambda mote_id: {}
    roles: Callable[[str], dict] = lambda mote_id: {}
    # Inside a function's body: each parameter's argument, and the
    # function motes being called, outermost first (for refusing
    # recursion). Empty everywhere else.
    bindings: dict[str, "Ratio"] = field(default_factory=dict)
    calls: tuple[str, ...] = ()
    # Told of every limit a function call takes (core/limits.py), so a
    # solve can say its answer came from one. Ignored by default.
    limit_taken: Callable[[limits_repo.Limit], None] = lambda limit: None


def relation_operator(text: str) -> str:
    """Which comparison a relation uses, without evaluating either side.

    Lets core/simulation.py sort rules into equations and constraints
    once per rule, before any solving, rather than discovering it
    mid-solve."""
    return parse_equation(text).operator


def parse_equation(text: str) -> Relation:
    """Split `lhs <op> rhs` into its parts, rejecting anything else.

    Named for the case that existed first and is still the only one the
    solver acts on. `==` asks for values that satisfy it; `<=`, `>=`, `<`
    and `>` only ever assert something about values found elsewhere."""
    body = _parse_ast(text)
    if not isinstance(body, ast.Compare):
        raise FormulaError(
            f"formula {text!r} is not a relation (expected `lhs == rhs`, or a "
            f"comparison such as `lhs <= rhs`)"
        )
    if len(body.ops) != 1:
        raise FormulaError(
            f"formula {text!r} must use exactly one comparison -- chained "
            f"comparisons (`0 <= x <= 1`) are two relations, so write them as two"
        )
    operator = _COMPARISONS.get(type(body.ops[0]))
    if operator is None:
        raise FormulaError(
            f"formula {text!r} uses a comparison that is not allowed; use one of "
            f"{', '.join(sorted(_COMPARISONS.values()))}"
        )
    return Relation(body.left, body.comparators[0], operator)


def attribute_bases(text: str) -> list[str]:
    """Every bare identifier used as the left side of a dotted access in
    `text` (e.g. `["R1", "B"]` for `"R1.voltage == B.x"`), in source
    order with duplicates removed, without evaluating anything.

    An editor that wants to know what an equation *references* before
    running it reads this rather than parsing formulas itself --
    core/simulation.py's `unresolved_equation_names()` is what an
    ambiguous name here turns into: an offer to draw the one relationship
    that picks which mote was meant (docs/simulation.md's "Name
    references"). A formula that fails to parse simply yields nothing
    here -- this never
    raises the parse errors evaluate_equation() would; a checker's job is
    to inform an editor, not to be a second place a bad formula fails."""
    try:
        relation = parse_equation(text)
    except FormulaError:
        return []
    bases: list[str] = []
    for side in (relation.left, relation.right):
        for node in ast.walk(side):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                if node.value.id not in bases:
                    bases.append(node.value.id)
    return bases


def evaluate_equation(text: str, ctx: Context) -> Equation:
    """Evaluate `lhs <op> rhs` into `lhs - rhs <op> 0`.

    The subtraction is the same whichever comparison was written, which
    is why one evaluator serves equations and constraints alike: the
    operator only decides what is done with the result -- solved for
    zero, or checked for a sign."""
    relation = parse_equation(text)
    difference = (
        _as_ratio(_evaluate(relation.left, ctx)) - _as_ratio(_evaluate(relation.right, ctx))
    )
    return Equation.from_difference(
        _clear_denominator(difference, relation.operator, text),
        source=text,
        operator=relation.operator,
    )


def _clear_denominator(difference: Ratio, operator: str, text: str) -> LinearExpr:
    """`n / d <op> 0` reduced to a plain linear `n' <op> 0`.

    With a constant denominator that is just dividing through, which
    changes nothing about the relation. With an unknown still under the
    bar, `n / d == 0` is equivalent to `n == 0` for every d that isn't
    zero -- which is what writing the division asserted in the first
    place -- and that is what makes `I / I0 == 0.1` solvable without the
    modeller rearranging it into `I == 0.1 * I0` by hand.

    A *constraint* gets no such treatment: multiplying `a / x <= b`
    through by x flips the comparison when x is negative, so the answer
    depends on a sign nothing here knows yet. Deferring (NonlinearError)
    is right rather than picking one: by the time constraints are checked
    the unknown is normally solved, the denominator is a number, and the
    first branch handles it. If it genuinely isn't solved,
    core/simulation.py reports the constraint as unchecked, which is the
    honest answer."""
    if not difference.is_divided:
        return difference.linear()
    if operator != EQUALS:
        raise NonlinearError(
            f"{text!r} divides by an unknown quantity "
            f"({describe(difference.denominator.terms)}); whether multiplying through "
            f"flips {operator} depends on its sign, so this can only be checked once "
            f"that value is known"
        )
    return difference.numerator


def evaluate_expression(text: str, ctx: Context) -> LinearExpr:
    """Evaluate a bare (non-equation) expression -- used by tests and by
    anything that just wants a value.

    A LinearExpr rather than a Ratio: with no relation to clear a
    denominator against, an unknown left under a division bar has nowhere
    to go, so `linear()` raises the same NonlinearError this always
    did."""
    return _as_ratio(_evaluate(_parse_expression(text), ctx)).linear()


def _parse_expression(text: str) -> ast.AST:
    return _parse_ast(text)


_BIN_OPS = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
    ast.Pow: lambda a, b: a ** b,
}


def _evaluate(node: ast.AST, ctx: Context):
    """Walk one node. Anything not explicitly handled here is rejected --
    this whitelist is the entire security boundary, so it stays a flat
    dispatch rather than a set of `isinstance` fall-throughs."""
    if isinstance(node, ast.Constant):
        return _constant(node)
    if isinstance(node, ast.Name):
        if node.id in ctx.bindings:
            return ctx.bindings[node.id]
        if node.id == TICK_NAME:
            return Ratio.number(ctx.tick)
        if node.id in MATH_CONSTANTS:
            return Ratio.number(MATH_CONSTANTS[node.id])
        return _lookup(ctx, ctx.mote_id, node.id)
    if isinstance(node, ast.BinOp):
        return _binary(node, ctx)
    if isinstance(node, ast.UnaryOp):
        return _unary(node, ctx)
    if isinstance(node, ast.Call):
        return _call(node, ctx)
    if isinstance(node, ast.Attribute):
        return _attribute(node, ctx)
    raise FormulaError(f"{type(node).__name__} is not allowed in a formula")


def _constant(node: ast.Constant) -> Ratio:
    if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
        raise FormulaError(f"{node.value!r} is not a number")
    return Ratio.number(node.value)


def _binary(node: ast.BinOp, ctx: Context) -> Ratio:
    op = _BIN_OPS.get(type(node.op))
    if op is None:
        raise FormulaError(f"operator {type(node.op).__name__} is not allowed in a formula")
    return op(_as_ratio(_evaluate(node.left, ctx)), _as_ratio(_evaluate(node.right, ctx)))


def _unary(node: ast.UnaryOp, ctx: Context) -> Ratio:
    operand = _as_ratio(_evaluate(node.operand, ctx))
    if isinstance(node.op, ast.USub):
        return -operand
    if isinstance(node.op, ast.UAdd):
        return operand
    raise FormulaError(f"unary {type(node.op).__name__} is not allowed in a formula")


def _attribute(node: ast.Attribute, ctx: Context):
    """`targets('a').voltage`, or `turbine.work` for a declared role.
    Handled explicitly rather than through __getattr__ on MoteSet, so
    there is no attribute-escape surface.

    A role and a property never compete: a bare name is always a property
    of the anchor mote, and a name in *this* position is always a role.
    Nothing changes meaning, because reading an attribute off anything
    other than targets()/sources() was already an error."""
    role, value = _role_or_value(node, ctx)
    if not isinstance(value, MoteSet):
        raise FormulaError(
            f"can only read a property off targets(...), sources(...) or a declared "
            f"role, not {node.attr!r} off a {type(value).__name__}"
        )
    values = [_lookup(ctx, mid, node.attr) for mid in value.mote_ids]
    label = f"{value.label}.{node.attr}"
    # A `many` role keeps its list even when it reaches exactly one mote,
    # so a formula that will need sum() the day a second one is wired
    # needs it from the start. Untyped navigation keeps collapsing, which
    # is what every existing garden relies on.
    if len(values) == 1 and (role is None or role.collapses):
        return values[0]
    return TermList(values, label)


def _role_or_value(node: ast.Attribute, ctx: Context):
    """Resolve the left-hand side of an attribute access: a declared
    role first, then a mote connected to the anchor by name, then a mote
    of that name anywhere in the garden, then whatever a plain property
    lookup makes of it (which -- unless it happens to be a MoteSet
    already -- _attribute() will reject). Returns the Role it matched (or
    None) alongside the value, because arity decides whether one mote
    collapses to a scalar; a name match always reaches at most one mote
    (see name_match)."""
    if not isinstance(node.value, ast.Name):
        return None, _evaluate(node.value, ctx)
    identifier = node.value.id
    role = ctx.roles(ctx.mote_id).get(identifier)
    if role is not None:
        mote_ids = ctx.targets(ctx.mote_id, role.name)
        problem = roles_repo.arity_problem(role, len(mote_ids))
        if problem is not None:
            raise FormulaError(f"mote {ctx.mote_id[:8]}: {problem}")
        return role, MoteSet(mote_ids, role.name)

    # Each scope is only asked once the one before it came up empty -- a
    # garden-wide collision must not be raised for a name a relationship
    # has already settled.
    for scope in (_connected_by_name, _named_in_garden):
        matches = scope(ctx, identifier)
        if matches is not None:
            return None, MoteSet(matches, identifier)

    return None, _evaluate(node.value, ctx)


def name_match(matches: list[str], name: str, where: str) -> list[str] | None:
    """The one mote a name reaches, or None for none at all.

    The single place the rule "a name picks exactly one thing" lives, for
    both scopes a name is searched in (a mote's neighbors, then the whole
    garden) and both walks that search them (this module and
    core/dimensions.py). None sends a caller on to the next scope, and
    finally to the ordinary property-lookup path -- and that path's
    existing error, if `name` isn't a property of the anchor either.

    Several matches is a hard error naming every one of them, never a
    silently collapsed set the way a `many` role or `sum()` would be: a
    name is meant to pick one specific thing, and a garden with two motes
    called `bd` has a modelling problem no solve should paper over."""
    if not matches:
        return None
    if len(matches) > 1:
        listed = ", ".join(sorted(mid[:8] for mid in matches))
        raise FormulaError(
            f"'{name}' matches {len(matches)} motes {where} ({listed}) -- rename all "
            f"but one, or reach it with targets('relationship-type')"
        )
    return matches


def _connected_by_name(ctx: Context, name: str) -> list[str] | None:
    """A mote immediately wired to the anchor -- any relationship type,
    either direction -- whose `name` property equals `name`.

    Tried before the garden-wide scope below, so a deliberately drawn
    relationship still decides which `bd` a rule means in a garden that
    holds two of them."""
    return name_match(
        [mid for mid in ctx.neighbors(ctx.mote_id) if ctx.properties(mid).get("name") == name],
        name,
        f"connected to mote {ctx.mote_id[:8]}",
    )


def _named_in_garden(ctx: Context, name: str) -> list[str] | None:
    """A mote of that name anywhere in the garden -- the last scope a name
    is searched in, after roles and connected names.

    This is what deletes the `related_to`-for-resolution edge: a garden
    had to wire `bd` to a rule's anchor for `bd.l` to resolve, so six of
    ten relationships in a real model existed only to make a reference
    legal. A name is already unique enough to identify a mote *to the
    person writing the equation*, and where it isn't, name_match refuses
    rather than guessing.

    The anchor itself is included: a rule about `bd` can write `bd.x` for
    its own property, which is what someone copying a line out of a
    worksheet means."""
    return name_match(list(ctx.named(name)), name, "in the garden")


def _call(node: ast.Call, ctx: Context):
    if not isinstance(node.func, ast.Name):
        raise FormulaError("only plain function calls are allowed in a formula")
    name = node.func.id
    if node.keywords:
        raise FormulaError(f"{name}() does not take keyword arguments")

    if name in NAVIGATION_CALLS:
        return _navigate(name, node, ctx)
    if name in AGGREGATE_CALLS:
        return _aggregate(name, node, ctx)
    if name in NUMERIC_CALLS:
        return _numeric(name, node, ctx)
    if name in TRIG_CALLS:
        return _trig(name, node, ctx)
    if name in LOG_CALLS:
        return _log(name, node, ctx)
    if name == SQRT_CALL:
        return _sqrt(node, ctx)
    if name == PREVIOUS_CALL:
        return _previous(node, ctx)
    if name == PROPERTY_CALL:
        return _property(node, ctx)
    return _function_call(name, node, ctx)


def function_named(ctx: Context, name: str) -> str:
    """The one function mote `name` calls: a mote of that name, anywhere
    in the garden, carrying a `function` body. Several is the same hard
    error a name reference gets; none is an unknown function."""
    candidates = [
        mid for mid in ctx.named(name)
        if functions_repo.is_function(ctx.properties(mid))
    ]
    found = name_match(candidates, name, "with a function body in the garden")
    if found is None:
        raise FormulaError(f"unknown function {name!r} -- no builtin or function mote has that name")
    return found[0]


def is_bare_number(node: ast.AST) -> bool:
    """Whether an argument is a number as written -- literals, `pi`/`e` and
    arithmetic on them, with no property in it. Only a bare number takes
    its unit from the parameter it is passed to; anything that reads a
    property already carries that property's unit."""
    for inner in ast.walk(node):
        if isinstance(inner, (ast.Attribute, ast.Call)):
            return False
        if isinstance(inner, ast.Name) and inner.id not in MATH_CONSTANTS:
            return False
    return True


def _function_call(name: str, node: ast.Call, ctx: Context) -> Ratio:
    """`phi(x)`: the function mote's body, evaluated with each parameter
    bound to its argument. Arguments are evaluated where the call is, so
    an unknown passes into the body as itself and the body is exactly as
    linear -- or as deferred -- as it would be written inline."""
    function_id = function_named(ctx, name)
    if function_id in ctx.calls:
        raise FormulaError(f"{name}() calls itself -- a function cannot be recursive")
    properties = ctx.properties(function_id)
    try:
        parameters = functions_repo.parameters_of(properties)
    except functions_repo.FunctionDefinitionError as e:
        raise FormulaError(f"function {name}: {e}") from e
    if len(node.args) != len(parameters):
        raise FormulaError(
            f"{name}() takes {len(parameters)} argument"
            f"{'' if len(parameters) == 1 else 's'} ({', '.join(parameters)}), "
            f"got {len(node.args)}"
        )
    arguments = {
        parameter: _argument(ctx, function_id, parameter, arg)
        for parameter, arg in zip(parameters, node.args)
    }
    body = functions_repo.body_of(properties)
    try:
        return _call_body(name, function_id, body, arguments, ctx)
    except IndeterminateError:
        if not all(a.is_constant for a in arguments.values()):
            raise
        return _limit_call(name, function_id, body, arguments, ctx)


def _argument(ctx: Context, function_id: str, parameter: str, node: ast.AST) -> Ratio:
    """One argument, in base units. A bare number is in the unit the
    parameter declares (`phi(0)` into `r [cm]` is 0 cm); with none
    declared, it is already base SI -- which the unit check warns about."""
    value = _as_ratio(_evaluate(node, ctx))
    if not is_bare_number(node):
        return value
    return Ratio.number(_normalize(ctx, function_id, parameter, value.constant))


def _limit_call(
    name: str, function_id: str, body: str, arguments: dict[str, Ratio], ctx: Context
) -> Ratio:
    """The call's value as a limit, approached along one parameter at a
    time, in order -- the first that settles is the answer, and the
    context is told. Only at a call: there the parameter is the variable
    to approach along, which a bare rule does not have."""
    first_failure = None
    for parameter in arguments:
        try:
            limit = _limit_along(name, function_id, body, arguments, parameter, ctx)
        except limits_repo.LimitError as e:
            first_failure = first_failure or e
            continue
        ctx.limit_taken(limit)
        return Ratio.number(limit.value)
    shown = ", ".join(f"{p} = {a.constant:g}" for p, a in arguments.items())
    raise FormulaError(f"in {name}(): 0/0 at {shown}, and {first_failure}")


def _limit_along(
    name: str, function_id: str, body: str, arguments: dict[str, Ratio],
    parameter: str, ctx: Context,
) -> limits_repo.Limit:
    def at(x: float) -> float:
        try:
            moved = {**arguments, parameter: Ratio.number(x)}
            return _call_body(name, function_id, body, moved, ctx).constant
        except NonlinearError:
            raise
        except FormulaError as e:
            raise limits_repo.LimitError(f"it is not defined beside the point: {e}") from e

    point = arguments[parameter].constant
    unit_name = ctx.units(function_id).get(parameter)
    try:
        scale = (to_base(1.0, unit_name) - to_base(0.0, unit_name)) if unit_name else None
    except UnitError:
        unit_name = scale = None  # reported by the unit check, not here
    value = limits_repo.two_sided(at, point, limits_repo.first_step(point, scale))
    shown = f"{from_base(point, unit_name):g} {unit_name}" if unit_name else f"{point:g}"
    return limits_repo.Limit(function_id, name, parameter, point, shown, value)


def _call_body(
    name: str, function_id: str, body: str, arguments: dict[str, Ratio], ctx: Context
) -> Ratio:
    inner = dataclasses.replace(
        ctx, mote_id=function_id, bindings=arguments, calls=(*ctx.calls, function_id))
    try:
        return _as_ratio(_evaluate(_parse_ast(body), inner))
    except FormulaError as e:
        # Same type, so a NonlinearError still defers and an
        # IndeterminateError can still be answered with a limit.
        raise type(e)(f"in {name}(): {e}") from e


def _previous(node: ast.Call, ctx: Context) -> Ratio:
    """prev(x) -- the value x had entering the current simulation step,
    for expressing a recurrence (`population == prev(population) *
    growth_rate`). x must be a bare property/unknown name on the current
    mote, the same way targets()/sources() require a bare string literal
    rather than an evaluated expression."""
    if len(node.args) != 1 or not isinstance(node.args[0], ast.Name):
        raise FormulaError("prev() takes a bare property name, e.g. prev(voltage)")
    key = node.args[0].id
    properties = ctx.previous(ctx.mote_id)
    if key not in properties:
        raise FormulaError(f"mote {ctx.mote_id[:8]} has no previous value for {key!r}")
    value = properties[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FormulaError(f"previous value of {key!r} is {value!r}, which is not a number")
    # The previous step's state is kept in the same representation the
    # database uses (declared units), so it needs the same normalization
    # an ordinary property read gets.
    return Ratio.number(_normalize(ctx, ctx.mote_id, key, value))


def _property(node: ast.Call, ctx: Context) -> Ratio:
    """property(<relationship type>, <operation>, <arg>, ...) -- reads a
    physical property off the single mote reached by navigating
    <relationship type> from the current mote, via that mote's resolved
    property model. The relationship type and operation must be quoted
    string literals, the same constraint _navigate() already puts on
    targets()/sources()'s argument. The remaining arguments must already
    be known numbers: a property model is an opaque, nonlinear function
    of its inputs, so -- like abs()/min()/max() in _numeric() -- this
    defers via NonlinearError until the substitution loop has determined
    them, rather than trying to invert the model itself."""
    if len(node.args) < 2:
        raise FormulaError(
            "property() takes a relationship type, an operation name, and its "
            "inputs, e.g. property('working_fluid', 'enthalpy', pressure, temperature)"
        )
    relationship_arg, operation_arg, *value_nodes = node.args
    if not isinstance(relationship_arg, ast.Constant) or not isinstance(relationship_arg.value, str):
        raise FormulaError("property()'s first argument must be a relationship type in quotes")
    if not isinstance(operation_arg, ast.Constant) or not isinstance(operation_arg.value, str):
        raise FormulaError("property()'s second argument must be an operation name in quotes")

    args = [_as_ratio(_evaluate(a, ctx)) for a in value_nodes]
    if any(not a.is_constant for a in args):
        raise NonlinearError(
            f"property({operation_arg.value!r}, ...) cannot be evaluated until its "
            f"inputs are known"
        )

    result = ctx.evaluate_model(
        ctx.mote_id, relationship_arg.value, operation_arg.value, [a.constant for a in args]
    )
    if isinstance(result, bool) or not isinstance(result, (int, float)):
        raise FormulaError(
            f"property({operation_arg.value!r}, ...) returned {result!r}, which is not a number"
        )
    return Ratio.number(result)


def _navigate(name: str, node: ast.Call, ctx: Context) -> MoteSet:
    if len(node.args) != 1:
        raise FormulaError(f"{name}() takes exactly one relationship type")
    arg = node.args[0]
    if not isinstance(arg, ast.Constant) or not isinstance(arg.value, str):
        raise FormulaError(f"{name}() takes a relationship type in quotes, e.g. {name}('a')")

    navigate = ctx.targets if name == "targets" else ctx.sources
    return MoteSet(navigate(ctx.mote_id, arg.value), f"{name}({arg.value!r})")


def _aggregate(name: str, node: ast.Call, ctx: Context):
    if len(node.args) != 1:
        raise FormulaError(f"{name}() takes exactly one argument")
    value = _evaluate(node.args[0], ctx)

    if name == "count":
        if isinstance(value, MoteSet):
            return Ratio.number(len(value.mote_ids))
        if isinstance(value, TermList):
            return Ratio.number(len(value.values))
        return Ratio.number(1)

    # sum(): a single mote collapses to a scalar upstream, so summing one
    # value (or none) has to keep working -- an unconnected mote should
    # give 0, not an error.
    if isinstance(value, TermList):
        total = Ratio.number(0.0)
        for item in value.values:
            total = total + item
        return total
    return _as_ratio(value)


def _numeric(name: str, node: ast.Call, ctx: Context) -> Ratio:
    args = [_as_ratio(_evaluate(a, ctx)) for a in node.args]
    if not args:
        raise FormulaError(f"{name}() requires at least one argument")
    if any(not a.is_constant for a in args):
        raise NonlinearError(f"{name}() cannot be applied to an unknown quantity")

    values = [a.constant for a in args]
    if name == "abs":
        if len(values) != 1:
            raise FormulaError("abs() takes exactly one argument")
        return Ratio.number(abs(values[0]))
    return Ratio.number(min(values) if name == "min" else max(values))


_TRIG_FUNCS = {
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "arcsin": math.asin,
    "arccos": math.acos,
    "arctan": math.atan,
    "sinc": lambda x: 1.0 if x == 0.0 else math.sin(x) / x,
}
_ARC_TRIG_DOMAIN = {"arcsin", "arccos"}


def _trig(name: str, node: ast.Call, ctx: Context) -> Ratio:
    """sin(x)/cos(x)/tan(x) and their inverses -- deferred, like
    abs()/min()/max(), until x is a known number rather than an unsolved
    unknown. arcsin/arccos are only defined for arguments in [-1, 1]."""
    if len(node.args) != 1:
        raise FormulaError(f"{name}() takes exactly one argument")
    arg = _as_ratio(_evaluate(node.args[0], ctx))
    if not arg.is_constant:
        raise NonlinearError(f"{name}() cannot be applied to an unknown quantity")
    value = arg.constant
    if name in _ARC_TRIG_DOMAIN and not -1.0 <= value <= 1.0:
        raise FormulaError(f"{name}({value!r}) -- the argument must be between -1 and 1")
    return Ratio.number(_TRIG_FUNCS[name](value))


_LOG_FUNCS = {
    "log": math.log,
    "ln": math.log,
    "log10": math.log10,
    "exp": math.exp,
}


def _log(name: str, node: ast.Call, ctx: Context) -> Ratio:
    """log/ln/log10/exp -- deferred, like the trig calls, until the
    argument is a known number rather than an unsolved unknown.

    These are what let a rule state a relation in whichever direction is
    the exact one. An attenuation law written `I == I0 * exp(-mxs * x)`
    has its unknown in an exponent, which no amount of rearranging inside
    this algebra makes linear -- it needs the iterative `newton` backend.
    Written the other way round, `mxs * x == -ln(I / I0)`, the same
    physics is linear in x and the default solver gets it exactly, with
    no initial guess and no convergence to worry about."""
    if len(node.args) != 1:
        raise FormulaError(f"{name}() takes exactly one argument")
    arg = _as_ratio(_evaluate(node.args[0], ctx))
    if not arg.is_constant:
        raise NonlinearError(f"{name}() cannot be applied to an unknown quantity")
    value = arg.constant
    if name != "exp" and value <= 0:
        raise FormulaError(f"{name}() of {value!r} -- the argument must be positive")
    try:
        return Ratio.number(_LOG_FUNCS[name](value))
    except (ValueError, OverflowError) as e:
        raise FormulaError(f"{name}({value!r}): {e}") from e


def _sqrt(node: ast.Call, ctx: Context) -> Ratio:
    if len(node.args) != 1:
        raise FormulaError("sqrt() takes exactly one argument")
    arg = _as_ratio(_evaluate(node.args[0], ctx))
    if not arg.is_constant:
        raise NonlinearError("sqrt() cannot be applied to an unknown quantity")
    if arg.constant < 0:
        raise FormulaError(f"sqrt() of a negative number ({arg.constant!r})")
    return Ratio.number(math.sqrt(arg.constant))


def _normalize(ctx: Context, mote_id: str, key: str, value: float) -> float:
    """A stored property value converted into its dimension's base unit.

    This is the single point where declared units enter the solver: past
    here every number is a plain base-unit float, which is what LinearExpr
    and both solver backends already assume. A key with no declared unit
    is returned untouched, so an un-annotated garden takes the same path
    it always has.

    An unrecognized unit name is fatal rather than ignored: the value was
    explicitly labelled, and treating `kohm` as if it were already ohms
    would be a silent factor-of-1000 error in the answer.
    """
    unit_name = ctx.units(mote_id).get(key)
    if not unit_name:
        return value
    try:
        return to_base(value, unit_name)
    except UnitError as e:
        raise FormulaError(
            f"property {key!r} of mote {mote_id[:8]} declares unit {unit_name!r}: {e}"
        ) from e


def _lookup(ctx: Context, mote_id: str, key: str) -> Ratio:
    # TICK_NAME and MATH_CONSTANTS are only reserved for a bare `t`/`pi`/`e`
    # referring to the anchor mote (shadowed by the caller in _evaluate,
    # ast.Name branch), not for one reached via a role/relationship (e.g.
    # `targetp3.t`) -- that's unambiguously a real property of a specific
    # other mote.
    if key in ctx.unknown_keys(mote_id):
        unknown = (mote_id, key)
        if unknown in ctx.solved:
            return Ratio.number(ctx.solved[unknown])
        return Ratio.unknown(unknown)

    properties = ctx.properties(mote_id)
    if key not in properties:
        raise FormulaError(
            f"mote {mote_id[:8]} has no property {key!r}, and its species does not "
            f"declare {key!r} as an unknown"
        )
    value = properties[key]
    # bool is an int subclass in Python; a true/false property is almost
    # certainly a modelling mistake rather than 1/0, so reject it.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FormulaError(
            f"property {key!r} of mote {mote_id[:8]} is {value!r}, which is not a number"
        )
    return Ratio.number(_normalize(ctx, mote_id, key, value))


def _as_ratio(value) -> Ratio:
    if isinstance(value, Ratio):
        return value
    if isinstance(value, MoteSet):
        raise FormulaError(
            f"{value.label} is a set of motes, not a number -- read a property off it "
            f"(e.g. {value.label}.voltage)"
        )
    if isinstance(value, TermList):
        count = len(value.values)
        raise FormulaError(
            f"{value.label} refers to {count} mote{'' if count == 1 else 's'} -- wrap "
            f"it in sum() to combine them into one value"
        )
    raise FormulaError(f"unexpected value {value!r} in formula")


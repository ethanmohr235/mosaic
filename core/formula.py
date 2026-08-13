# core/formula.py
"""Restricted expression language for simulation rules.

Expressions are parsed with the stdlib `ast` module and walked against a
strict node whitelist -- never `eval()`. Evaluation produces a LinearExpr
(a weighted sum of unknowns plus a constant), so an expression that
happens to contain no unknowns is simply a number. That means one
evaluator serves both assignment-style rules and coupled equations.
"""
import ast
from collections.abc import Callable
from dataclasses import dataclass, field

# An unknown the solver may determine: which mote, which property key.
Unknown = tuple[str, str]

_TOLERANCE = 1e-9


class FormulaError(ValueError):
    """A formula could not be parsed or evaluated."""


class NonlinearError(FormulaError):
    """A formula is nonlinear in its unknowns, so it can't be solved as a
    linear system. Raised lazily so the substitution loop can retry once
    the offending unknowns have been determined by other equations."""


@dataclass(frozen=True)
class LinearExpr:
    """`sum(coefficient * unknown) + constant`.

    The arithmetic operators enforce linearity structurally: there is no
    way to build a LinearExpr that multiplies two unknowns together.
    """
    terms: dict[Unknown, float] = field(default_factory=dict)
    constant: float = 0.0

    @staticmethod
    def number(value: float) -> "LinearExpr":
        return LinearExpr({}, float(value))

    @staticmethod
    def unknown(key: Unknown) -> "LinearExpr":
        return LinearExpr({key: 1.0}, 0.0)

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
        return LinearExpr(_prune(terms), self.constant + other.constant)

    def __sub__(self, other: "LinearExpr") -> "LinearExpr":
        return self + (-other)

    def __neg__(self) -> "LinearExpr":
        return LinearExpr({k: -v for k, v in self.terms.items()}, -self.constant)

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
        if abs(other.constant) < _TOLERANCE:
            raise FormulaError("division by zero")
        return self.scaled(1.0 / other.constant)

    def __pow__(self, other: "LinearExpr") -> "LinearExpr":
        if not self.is_constant or not other.is_constant:
            raise NonlinearError("cannot raise an unknown quantity to a power")
        return LinearExpr.number(self.constant ** other.constant)

    def scaled(self, factor: float) -> "LinearExpr":
        if abs(factor) < _TOLERANCE:
            return LinearExpr.number(0.0)
        return LinearExpr(
            {k: v * factor for k, v in self.terms.items()}, self.constant * factor
        )


def describe(unknowns) -> str:
    """Render unknowns for error messages -- full UUIDs are unreadable, and
    the first 8 characters are what the UI shows too."""
    return ", ".join(f"{mote_id[:8]}.{key}" for mote_id, key in sorted(unknowns))


def _prune(terms: dict[Unknown, float]) -> dict[Unknown, float]:
    """Drop terms whose coefficients cancelled out, so `is_constant` stays
    honest after e.g. `voltage - voltage`."""
    return {k: v for k, v in terms.items() if abs(v) > _TOLERANCE}


@dataclass(frozen=True)
class Equation:
    """`sum(coefficient * unknown) + constant == 0`."""
    terms: dict[Unknown, float]
    constant: float
    source: str = ""

    @staticmethod
    def from_difference(expr: LinearExpr, source: str = "") -> "Equation":
        return Equation(expr.terms, expr.constant, source)


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
    values: list[LinearExpr]
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
    unaffected."""
    mote_id: str
    properties: Callable[[str], dict]
    unknown_keys: Callable[[str], set[str]]
    targets: Callable[[str, str], list[str]]
    sources: Callable[[str, str], list[str]]
    solved: dict[Unknown, float] = field(default_factory=dict)
    tick: float = 0.0
    previous: Callable[[str], dict] = lambda mote_id: {}
    evaluate_model: Callable[[str, str, str, list[float]], float] = _model_evaluation_unavailable


def parse_equation(text: str) -> tuple[ast.AST, ast.AST]:
    """Split `lhs == rhs` into its two sides, rejecting anything else."""
    try:
        tree = ast.parse(text.strip(), mode="eval")
    except SyntaxError as e:
        raise FormulaError(f"could not parse formula {text!r}: {e.msg}") from e

    body = tree.body
    if not isinstance(body, ast.Compare):
        raise FormulaError(f"formula {text!r} is not an equation (expected `lhs == rhs`)")
    if len(body.ops) != 1 or not isinstance(body.ops[0], ast.Eq):
        raise FormulaError(f"formula {text!r} must use exactly one `==` comparison")
    return body.left, body.comparators[0]


def evaluate_equation(text: str, ctx: Context) -> Equation:
    """Evaluate `lhs == rhs` into the equation `lhs - rhs == 0`."""
    left, right = parse_equation(text)
    difference = _as_expr(_evaluate(left, ctx)) - _as_expr(_evaluate(right, ctx))
    return Equation.from_difference(difference, source=text)


def evaluate_expression(text: str, ctx: Context) -> LinearExpr:
    """Evaluate a bare (non-equation) expression -- used by tests and by
    anything that just wants a value."""
    try:
        tree = ast.parse(text.strip(), mode="eval")
    except SyntaxError as e:
        raise FormulaError(f"could not parse formula {text!r}: {e.msg}") from e
    return _as_expr(_evaluate(tree.body, ctx))


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


def _constant(node: ast.Constant) -> LinearExpr:
    if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
        raise FormulaError(f"{node.value!r} is not a number")
    return LinearExpr.number(node.value)


def _binary(node: ast.BinOp, ctx: Context) -> LinearExpr:
    op = _BIN_OPS.get(type(node.op))
    if op is None:
        raise FormulaError(f"operator {type(node.op).__name__} is not allowed in a formula")
    return op(_as_expr(_evaluate(node.left, ctx)), _as_expr(_evaluate(node.right, ctx)))


def _unary(node: ast.UnaryOp, ctx: Context) -> LinearExpr:
    operand = _as_expr(_evaluate(node.operand, ctx))
    if isinstance(node.op, ast.USub):
        return -operand
    if isinstance(node.op, ast.UAdd):
        return operand
    raise FormulaError(f"unary {type(node.op).__name__} is not allowed in a formula")


def _attribute(node: ast.Attribute, ctx: Context):
    """`targets('a').voltage`. Handled explicitly rather than through
    __getattr__ on MoteSet, so there is no attribute-escape surface."""
    value = _evaluate(node.value, ctx)
    if not isinstance(value, MoteSet):
        raise FormulaError(
            f"can only read a property off targets(...) or sources(...), not {node.attr!r} "
            f"off a {type(value).__name__}"
        )
    values = [_lookup(ctx, mid, node.attr) for mid in value.mote_ids]
    label = f"{value.label}.{node.attr}"
    if len(values) == 1:
        return values[0]
    return TermList(values, label)


def _call(node: ast.Call, ctx: Context):
    if not isinstance(node.func, ast.Name):
        raise FormulaError("only plain function calls are allowed in a formula")
    name = node.func.id
    if node.keywords:
        raise FormulaError(f"{name}() does not take keyword arguments")

    if name in ("targets", "sources"):
        return _navigate(name, node, ctx)
    if name in ("sum", "count"):
        return _aggregate(name, node, ctx)
    if name in ("abs", "min", "max"):
        return _numeric(name, node, ctx)
    if name == "prev":
        return _previous(node, ctx)
    if name == "property":
        return _property(node, ctx)
    raise FormulaError(f"unknown function {name!r} in formula")


def _previous(node: ast.Call, ctx: Context) -> LinearExpr:
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
    return LinearExpr.number(value)


def _property(node: ast.Call, ctx: Context) -> LinearExpr:
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

    args = [_as_expr(_evaluate(a, ctx)) for a in value_nodes]
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
    return LinearExpr.number(result)


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
            return LinearExpr.number(len(value.mote_ids))
        if isinstance(value, TermList):
            return LinearExpr.number(len(value.values))
        return LinearExpr.number(1)

    # sum(): a single mote collapses to a scalar upstream, so summing one
    # value (or none) has to keep working -- an unconnected mote should
    # give 0, not an error.
    if isinstance(value, TermList):
        total = LinearExpr.number(0.0)
        for item in value.values:
            total = total + item
        return total
    return _as_expr(value)


def _numeric(name: str, node: ast.Call, ctx: Context) -> LinearExpr:
    args = [_as_expr(_evaluate(a, ctx)) for a in node.args]
    if not args:
        raise FormulaError(f"{name}() requires at least one argument")
    if any(not a.is_constant for a in args):
        raise NonlinearError(f"{name}() cannot be applied to an unknown quantity")

    values = [a.constant for a in args]
    if name == "abs":
        if len(values) != 1:
            raise FormulaError("abs() takes exactly one argument")
        return LinearExpr.number(abs(values[0]))
    return LinearExpr.number(min(values) if name == "min" else max(values))


def _lookup(ctx: Context, mote_id: str, key: str) -> LinearExpr:
    # A reserved word in the formula language, not a per-mote property --
    # the current simulation step's index, for time-dependent rules
    # (e.g. `height == 10 - 0.5 * t`). Checked before both the unknowns
    # and the properties lookup, so a mote with a real property literally
    # named `t` has it shadowed inside formulas specifically (still an
    # ordinary, visible property everywhere else in the UI).
    if key == "t":
        return LinearExpr.number(ctx.tick)
    if key in ctx.unknown_keys(mote_id):
        unknown = (mote_id, key)
        if unknown in ctx.solved:
            return LinearExpr.number(ctx.solved[unknown])
        return LinearExpr.unknown(unknown)

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
    return LinearExpr.number(value)


def _as_expr(value) -> LinearExpr:
    if isinstance(value, LinearExpr):
        return value
    if isinstance(value, MoteSet):
        raise FormulaError(
            f"{value.label} is a set of motes, not a number -- read a property off it "
            f"(e.g. {value.label}.voltage)"
        )
    if isinstance(value, TermList):
        raise FormulaError(
            f"{value.label} refers to {len(value.values)} motes -- wrap it in sum() "
            f"to combine them into one value"
        )
    raise FormulaError(f"unexpected value {value!r} in formula")


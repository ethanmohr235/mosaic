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

from core import roles as roles_repo
from core.units import UnitError, to_base

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
PREVIOUS_CALL = "prev"
PROPERTY_CALL = "property"
ALLOWED_CALLS = (
    NAVIGATION_CALLS | AGGREGATE_CALLS | NUMERIC_CALLS | {PREVIOUS_CALL, PROPERTY_CALL}
)

# The current simulation step's index, exposed to formulas as a reserved
# bare name rather than a per-mote property.
TICK_NAME = "t"


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

    @property
    def is_equation(self) -> bool:
        return self.operator == EQUALS

    @staticmethod
    def from_difference(
        expr: LinearExpr, source: str = "", operator: str = EQUALS
    ) -> "Equation":
        return Equation(expr.terms, expr.constant, source, operator)


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
    (core/modoc.py's `expression_context`, existing tests)."""
    mote_id: str
    properties: Callable[[str], dict]
    unknown_keys: Callable[[str], set[str]]
    targets: Callable[[str, str], list[str]]
    sources: Callable[[str, str], list[str]]
    neighbors: Callable[[str], list[str]] = lambda mote_id: []
    solved: dict[Unknown, float] = field(default_factory=dict)
    tick: float = 0.0
    previous: Callable[[str], dict] = lambda mote_id: {}
    evaluate_model: Callable[[str, str, str, list[float]], float] = _model_evaluation_unavailable
    units: Callable[[str], dict] = lambda mote_id: {}
    roles: Callable[[str], dict] = lambda mote_id: {}


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
    try:
        tree = ast.parse(text.strip(), mode="eval")
    except SyntaxError as e:
        raise FormulaError(f"could not parse formula {text!r}: {e.msg}") from e

    body = tree.body
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
    unconnected name here turns into: an offer to wire up the missing
    relationship (docs/simulation.md's "Connected-name references"). A
    formula that fails to parse simply yields nothing here -- this never
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
        _as_expr(_evaluate(relation.left, ctx)) - _as_expr(_evaluate(relation.right, ctx))
    )
    return Equation.from_difference(difference, source=text, operator=relation.operator)


def evaluate_expression(text: str, ctx: Context) -> LinearExpr:
    """Evaluate a bare (non-equation) expression -- used by tests and by
    anything that just wants a value."""
    return _as_expr(_evaluate(_parse_expression(text), ctx))


def evaluate_selection(text: str, ctx: Context) -> list[str]:
    """The mote ids an expression *selects*, e.g. `targets('contains')`.

    The counterpart to evaluate_expression(), which insists on a number
    and so rejects a bare navigation. Some callers want the motes
    themselves: core/modoc_bind.py's `\\graph{targets('contains') by
    status}` groups them by a property rather than reducing them to one
    value. Kept here, beside the walker, so "what selects motes" stays a
    fact about the language rather than something an adapter reaches
    through a private function to find out."""
    value = _evaluate(_parse_expression(text), ctx)
    if isinstance(value, MoteSet):
        return list(value.mote_ids)
    raise FormulaError(
        f"{text!r} does not select motes -- expected a navigation "
        f"like targets('contains') or sources('works_on')"
    )


def _parse_expression(text: str) -> ast.AST:
    try:
        tree = ast.parse(text.strip(), mode="eval")
    except SyntaxError as e:
        raise FormulaError(f"could not parse formula {text!r}: {e.msg}") from e
    return tree.body


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
    role first, then a mote connected to the anchor by name, then
    whatever a plain property lookup makes of it (which -- unless it
    happens to be a MoteSet already -- _attribute() will reject). Returns
    the Role it matched (or None) alongside the value, because arity
    decides whether one mote collapses to a scalar; a name match always
    reaches at most one mote (see _connected_by_name)."""
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

    matches = _connected_by_name(ctx, identifier)
    if matches is not None:
        return None, MoteSet(matches, identifier)

    return None, _evaluate(node.value, ctx)


def _connected_by_name(ctx: Context, name: str) -> list[str] | None:
    """A mote immediately wired to the anchor -- any relationship type,
    either direction -- whose `name` property equals `name`. None means
    "no such neighbor", which sends the caller on to the ordinary
    property-lookup path (and that path's existing error, if `name`
    isn't a property of the anchor either).

    Deliberately narrower than a role: this exists so a garden wired by
    hand doesn't need a role declared for a one-off reference, not to
    replace targets()/sources() for real navigation -- which is why two
    connected motes sharing a name is an error rather than a silently
    collapsed set. A name is meant to pick one specific thing."""
    matches = [
        mid for mid in ctx.neighbors(ctx.mote_id)
        if ctx.properties(mid).get("name") == name
    ]
    if not matches:
        return None
    if len(matches) > 1:
        raise FormulaError(
            f"'{name}' matches {len(matches)} motes connected to mote "
            f"{ctx.mote_id[:8]} -- rename one of them, or reach it with "
            f"targets('relationship-type')"
        )
    return matches


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
    if name == PREVIOUS_CALL:
        return _previous(node, ctx)
    if name == PROPERTY_CALL:
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
    # The previous step's state is kept in the same representation the
    # database uses (declared units), so it needs the same normalization
    # an ordinary property read gets.
    return LinearExpr.number(_normalize(ctx, ctx.mote_id, key, value))


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


def _lookup(ctx: Context, mote_id: str, key: str) -> LinearExpr:
    # A reserved word in the formula language, not a per-mote property --
    # the current simulation step's index, for time-dependent rules
    # (e.g. `height == 10 - 0.5 * t`). Checked before both the unknowns
    # and the properties lookup, so a mote with a real property literally
    # named `t` has it shadowed inside formulas specifically (still an
    # ordinary, visible property everywhere else in the UI).
    if key == TICK_NAME:
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
    return LinearExpr.number(_normalize(ctx, mote_id, key, value))


def _as_expr(value) -> LinearExpr:
    if isinstance(value, LinearExpr):
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


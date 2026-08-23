# core/dimensions.py
"""Dimensional consistency checking for simulation rules.

Walks the same equation AST core/formula.py evaluates, but propagates
*dimensions* instead of values, so `voltage == current * resistance`
can be confirmed as V = A·Ω before anyone trusts the number it produces.

Two properties of this checker matter more than how much it catches:

1. **It never produces a false positive on an un-annotated garden.** A
   property with no declared unit contributes "unspecified", which is
   compatible with everything. A garden that never sets `units` gets
   exactly zero warnings, which is what makes units opt-in rather than a
   migration.
2. **It never fails a run on its own.** Problems come back as strings for
   the caller to surface; only an explicit `strict_units` opt-in turns
   them into an error (see core/simulation.py).

Like core/formula.py, this module is pure: plain callables, no database
import, testable with dicts.

The grammar accepted here must stay identical to the one core/formula.py
evaluates -- it reuses that module's call-name constants rather than
repeating them, and tests/test_dimensions.py fails if a builtin is added
there without being handled here. See docs/units_proposal.
"""
import ast
from collections.abc import Callable
from dataclasses import dataclass, field

from core import roles as roles_repo
from core.formula import (
    AGGREGATE_CALLS,
    NAVIGATION_CALLS,
    NUMERIC_CALLS,
    PREVIOUS_CALL,
    PROPERTY_CALL,
    FormulaError,
    TICK_NAME,
    parse_equation,
)
from core.units import DIMENSIONLESS, Dimension, UnitError, describe, dimension_of

# A dimension, or None meaning "not declared" -- deliberately distinct
# from DIMENSIONLESS, which is a positive claim that something is a pure
# ratio. Unspecified is compatible with everything; dimensionless is not.
Dim = Dimension | None


class _LiteralDimension(Dimension):
    """A bare number written in a formula.

    Dimensionless when it takes part in arithmetic (`2 * voltage` is
    still volts), but willing to stand for whatever it is added to or
    compared against -- `resistance == 100` is the ordinary way to state
    a known value, and the literal is read as being in that quantity's
    own unit. That flexibility is exactly what a *computed* dimensionless
    value must not have: if `a / b` cancels to a pure ratio, comparing it
    to a voltage is a real mistake worth reporting.
    """


LITERAL = _LiteralDimension()


def _is_literal(dimension: Dim) -> bool:
    return isinstance(dimension, _LiteralDimension)


@dataclass(frozen=True)
class _Motes:
    """The result of targets(...)/sources(...) -- mirrors
    core/formula.py's MoteSet, carrying only what this walk needs."""
    mote_ids: list[str]
    label: str


@dataclass(frozen=True)
class _Values:
    """Several motes' dimensions for one property, awaiting sum()/count().
    Mirrors core/formula.py's TermList."""
    dimensions: list[Dim]
    label: str


def _no_capability_units(*_args) -> str | None:
    return None


@dataclass(frozen=True)
class DimensionContext:
    """Everything the dimension walk can see. Plain callables, the same
    shape as core/formula.py's Context.

    `units` returns a mote's declared key -> unit-name map (already
    cleaned by core.units.parse_units_property). `capability_units`
    resolves a property() call's declared output unit -- the units a
    provider's Capability already declares -- and returns None when the
    model, operation, or unit can't be determined, which simply makes
    that call unspecified rather than an error.
    """
    mote_id: str
    units: Callable[[str], dict[str, str]]
    targets: Callable[[str, str], list[str]]
    sources: Callable[[str, str], list[str]]
    capability_units: Callable[[str, str, str], str | None] = _no_capability_units
    label: Callable[[str], str] = lambda mote_id: mote_id[:8]
    roles: Callable[[str], dict] = lambda mote_id: {}


def check_equation(text: str, ctx: DimensionContext) -> list[str]:
    """Dimensional problems with one equation, as readable sentences.

    An empty list means "consistent, or not annotated enough to tell" --
    the two are deliberately indistinguishable to a caller, since both
    mean "nothing to report".

    An equation that doesn't parse returns no problems: core/formula.py
    already reports that, with a better message, when the run evaluates
    it. This checker's job is units, not syntax.
    """
    try:
        relation = parse_equation(text)
        left, right = relation.left, relation.right
    except FormulaError:
        return []

    checker = _Checker(ctx)
    try:
        left_dim = checker.dimension(left)
        right_dim = checker.dimension(right)
    except FormulaError:
        # Anything the real evaluator would reject too. Same reasoning as
        # the parse failure above -- don't double-report it here.
        return checker.problems

    if _conflict(left_dim, right_dim):
        checker.report(
            f"left side is {describe(left_dim)} but right side is "
            f"{describe(right_dim)} -- these are different dimensions"
        )
    return checker.problems


def _conflict(a: Dim, b: Dim) -> bool:
    """Two dimensions disagree only when both are actually declared, and
    neither is a literal free to adopt the other's unit."""
    if a is None or b is None or _is_literal(a) or _is_literal(b):
        return False
    return a != b


class _Checker:
    """One walk over one equation, accumulating problems rather than
    raising, so a single pass reports everything wrong at once."""

    def __init__(self, ctx: DimensionContext):
        self.ctx = ctx
        self.problems: list[str] = []
        self._reported_units: set[str] = set()

    def report(self, message: str) -> None:
        if message not in self.problems:
            self.problems.append(message)

    def dimension(self, node: ast.AST) -> Dim:
        """A node's dimension, collapsing mote sets the way an arithmetic
        context would."""
        value = self._walk(node)
        if isinstance(value, _Motes):
            raise FormulaError(f"{value.label} is a set of motes, not a number")
        if isinstance(value, _Values):
            raise FormulaError(f"{value.label} refers to several motes")
        return value

    def _walk(self, node: ast.AST):
        if isinstance(node, ast.Constant):
            return LITERAL
        if isinstance(node, ast.Name):
            return self._name(self.ctx.mote_id, node.id)
        if isinstance(node, ast.BinOp):
            return self._binary(node)
        if isinstance(node, ast.UnaryOp):
            return self.dimension(node.operand)
        if isinstance(node, ast.Call):
            return self._call(node)
        if isinstance(node, ast.Attribute):
            return self._attribute(node)
        raise FormulaError(f"{type(node).__name__} is not allowed in a formula")

    def _name(self, mote_id: str, key: str) -> Dim:
        if key == TICK_NAME:
            # A step index, not a time. Whether a rule means it as a count
            # or as elapsed seconds is genuinely ambiguous (`height == 10 -
            # 0.5 * t` in docs/simulation.md reads it as the latter), so it
            # stays unspecified rather than inventing a false positive.
            return None
        declared = self.ctx.units(mote_id).get(key)
        if declared is None:
            return None
        try:
            return dimension_of(declared)
        except UnitError as e:
            # A typo'd unit name is a real mistake worth surfacing, but it
            # shouldn't cascade -- report once, then treat as undeclared.
            if declared not in self._reported_units:
                self._reported_units.add(declared)
                self.report(
                    f"{self.ctx.label(mote_id)}.{key} declares unit {declared!r}, "
                    f"which is not recognized ({e})"
                )
            return None

    def _binary(self, node: ast.BinOp) -> Dim:
        op = type(node.op)
        if op in (ast.Add, ast.Sub):
            return self._additive(node)
        left = self.dimension(node.left)
        if op is ast.Pow:
            return self._power(left, node.right)
        right = self.dimension(node.right)
        if left is None or right is None:
            # Unknown in, unknown out: guessing here is exactly how a
            # checker starts crying wolf on partly-annotated gardens.
            return None
        if op is ast.Mult:
            return left + right
        if op is ast.Div:
            return left - right
        raise FormulaError(f"operator {op.__name__} is not allowed in a formula")

    def _additive(self, node: ast.BinOp) -> Dim:
        left = self.dimension(node.left)
        right = self.dimension(node.right)
        # A literal takes on the unit of whatever it's added to, so it
        # carries no information of its own here.
        if _is_literal(left):
            return right
        if _is_literal(right):
            return left
        if _conflict(left, right):
            verb = "added to" if isinstance(node.op, ast.Add) else "subtracted from"
            self.report(
                f"{describe(right)} cannot be {verb} {describe(left)} -- "
                f"different dimensions"
            )
        return left if left is not None else right

    def _power(self, base: Dim, exponent_node: ast.AST) -> Dim:
        """`x ** n` scales x's dimension by n, which requires n to be a
        literal. core/formula.py already refuses a non-constant exponent
        (LinearExpr.__pow__), so nothing is lost by only handling that."""
        exponent = _literal_number(exponent_node)
        if base is None or exponent is None:
            return None
        if _is_literal(base):
            return LITERAL
        return base * exponent

    def _attribute(self, node: ast.Attribute):
        """`targets('a').voltage`, or `turbine.work` for a declared role.

        Roles are resolved here exactly as core/formula.py resolves them,
        through the shared rules in core/roles.py. The two walks must
        agree about what a name means -- a role this checker did not
        recognize would make every equation using one unverifiable."""
        role, value = self._role_or_value(node)
        if not isinstance(value, _Motes):
            raise FormulaError(
                "can only read a property off targets(...), sources(...) or a declared role"
            )
        dimensions = [self._name(mid, node.attr) for mid in value.mote_ids]
        label = f"{value.label}.{node.attr}"
        if len(dimensions) == 1 and (role is None or role.collapses):
            return dimensions[0]
        return _Values(dimensions, label)

    def _role_or_value(self, node: ast.Attribute):
        if not isinstance(node.value, ast.Name):
            return None, self._walk(node.value)
        role = self.ctx.roles(self.ctx.mote_id).get(node.value.id)
        if role is None:
            return None, self._walk(node.value)

        mote_ids = self.ctx.targets(self.ctx.mote_id, role.name)
        problem = roles_repo.arity_problem(role, len(mote_ids))
        if problem is not None:
            raise FormulaError(f"mote {self.ctx.mote_id[:8]}: {problem}")
        return role, _Motes(mote_ids, role.name)

    def _call(self, node: ast.Call):
        if not isinstance(node.func, ast.Name):
            raise FormulaError("only plain function calls are allowed in a formula")
        name = node.func.id

        if name in NAVIGATION_CALLS:
            return self._navigate(name, node)
        if name in AGGREGATE_CALLS:
            return self._aggregate(name, node)
        if name in NUMERIC_CALLS:
            return self._combine([self.dimension(a) for a in node.args], name)
        if name == PREVIOUS_CALL:
            # prev(x) is the same quantity as x, one step earlier.
            if len(node.args) != 1 or not isinstance(node.args[0], ast.Name):
                raise FormulaError("prev() takes a bare property name")
            return self._name(self.ctx.mote_id, node.args[0].id)
        if name == PROPERTY_CALL:
            return self._property(node)
        raise FormulaError(f"unknown function {name!r} in formula")

    def _navigate(self, name: str, node: ast.Call) -> _Motes:
        if len(node.args) != 1 or not isinstance(node.args[0], ast.Constant):
            raise FormulaError(f"{name}() takes a relationship type in quotes")
        relationship = node.args[0].value
        navigate = self.ctx.targets if name == "targets" else self.ctx.sources
        return _Motes(navigate(self.ctx.mote_id, relationship), f"{name}({relationship!r})")

    def _aggregate(self, name: str, node: ast.Call) -> Dim:
        if len(node.args) != 1:
            raise FormulaError(f"{name}() takes exactly one argument")
        if name == "count":
            return DIMENSIONLESS
        value = self._walk(node.args[0])
        if isinstance(value, _Values):
            # Summing motes that measure different things is a modelling
            # error the value-level evaluator can't see, because by then
            # they're all just floats.
            return self._combine(value.dimensions, f"sum({value.label})")
        if isinstance(value, _Motes):
            raise FormulaError(f"{value.label} is a set of motes, not a number")
        return value

    def _combine(self, dimensions: list[Dim], label: str) -> Dim:
        """One dimension out of several that all ought to agree. Literals
        (`max(floor, 0)`) adopt the others' unit and constrain nothing."""
        known = [d for d in dimensions if d is not None and not _is_literal(d)]
        if not known:
            return LITERAL if any(_is_literal(d) for d in dimensions) else None
        for other in known[1:]:
            if other != known[0]:
                self.report(
                    f"{label} combines {describe(known[0])} with {describe(other)} -- "
                    f"different dimensions"
                )
                break
        return known[0]

    def _property(self, node: ast.Call) -> Dim:
        """A property() call's dimension is whatever unit the provider's
        Capability declares for that operation. Undeclared, unparseable,
        or unresolvable means unspecified -- a property model shouldn't
        have to annotate its units before a rule using it can be checked
        at all."""
        if len(node.args) < 2:
            raise FormulaError("property() takes a relationship type and an operation")
        relationship, operation = node.args[0], node.args[1]
        if not isinstance(relationship, ast.Constant) or not isinstance(operation, ast.Constant):
            raise FormulaError("property()'s first two arguments must be quoted strings")

        for argument in node.args[2:]:
            self.dimension(argument)  # surfaces problems inside the inputs

        declared = self.ctx.capability_units(
            self.ctx.mote_id, relationship.value, operation.value
        )
        if declared is None:
            return None
        try:
            return dimension_of(declared)
        except UnitError:
            # A provider declaring a unit this registry can't parse is a
            # provider-side gap, not a fault in the rule being checked.
            return None


def _literal_number(node: ast.AST) -> float | None:
    """A plain numeric literal, with an optional sign. Anything else --
    including a property that merely happens to hold a number -- is not
    usable as an exponent at check time."""
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        inner = _literal_number(node.operand)
        if inner is None:
            return None
        return -inner if isinstance(node.op, ast.USub) else inner
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        if isinstance(node.value, bool):
            return None
        return float(node.value)
    return None

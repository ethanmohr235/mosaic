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
there without being handled here. See docs/simulation.md, Units.
"""
import ast
from collections.abc import Callable
from dataclasses import dataclass, field

from core import roles as roles_repo
from core.formula import (
    AGGREGATE_CALLS,
    MATH_CONSTANTS,
    NAVIGATION_CALLS,
    NUMERIC_CALLS,
    LOG_CALLS,
    SQRT_CALL,
    TRIG_CALLS,
    PREVIOUS_CALL,
    PROPERTY_CALL,
    FormulaError,
    TICK_NAME,
    name_match,
    parse_equation,
)
from core.units import (
    DIMENSIONLESS,
    Dimension,
    UnitError,
    describe,
    describe_expression,
    dimension_of,
    is_unit,
)

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

    `neighbors`/`mote_name` and `named` back the two scopes a name is
    searched in -- the anchor's neighbors, then the whole garden --
    mirroring core/formula.py's Context. The two walks must agree about
    what a name means, exactly as they already agree about roles. All
    three default to matching nothing, so a caller that doesn't wire
    them up simply never resolves a name.
    """
    mote_id: str
    units: Callable[[str], dict[str, str]]
    targets: Callable[[str, str], list[str]]
    sources: Callable[[str, str], list[str]]
    capability_units: Callable[[str, str, str], str | None] = _no_capability_units
    label: Callable[[str], str] = lambda mote_id: mote_id[:8]
    roles: Callable[[str], dict] = lambda mote_id: {}
    neighbors: Callable[[str], list[str]] = lambda mote_id: []
    mote_name: Callable[[str], str | None] = lambda mote_id: None
    named: Callable[[str], list[str]] = lambda name: []


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


def infer_unit(text: str, key: str, ctx: DimensionContext) -> str | None:
    """A reasonable unit for `key` on `ctx.mote_id`, read off the *other*
    side of an equation that determines it -- for a solved freedom with
    no declared unit of its own (see core/simulation.py's write-back).

    Only fires when `key` is the entire left- or right-hand side of the
    equation (`resistance == voltage / current`): a name buried inside a
    larger expression doesn't pin down a dimension on its own
    (`voltage == 2 * resistance` says nothing about resistance in
    isolation), so that case is left alone rather than guessed at.

    Returns a name that round-trips through core.units.unit() -- the
    canonical symbol for a familiar dimension (`V`, `N`), or the
    base-dimension product for one that has no name of its own (`m*s^-1`),
    both of which core/units.py's compound grammar can parse back. Written
    in caret notation via describe_expression() rather than describe()'s
    unicode superscripts, so an automatically inferred unit is typed the
    same way a person typing one by hand would type it -- core/units.py's
    format_unit() renders either one with superscripts for display. None
    means the other side's dimension is unknown, a bare literal,
    dimensionless (nothing to name), or -- there is no such dimension --
    not really possible, but checked anyway since a guess that doesn't
    parse is worse than no guess."""
    try:
        relation = parse_equation(text)
    except FormulaError:
        return None

    checker = _Checker(ctx)
    try:
        if checker.is_own_property(relation.left, key):
            other = relation.right
        elif checker.is_own_property(relation.right, key):
            other = relation.left
        else:
            return None
        dimension = checker.dimension(other)
    except FormulaError:
        return None

    if dimension is None or _is_literal(dimension) or dimension.is_dimensionless:
        return None
    name = describe_expression(dimension)
    return name if is_unit(name) else None




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
            if node.id == TICK_NAME:
                return None
            if node.id in MATH_CONSTANTS:
                return LITERAL
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
        # TICK_NAME and MATH_CONSTANTS are handled by callers that reach a
        # *bare* `t`/`pi`/`e` on the anchor mote (_walk's ast.Name branch,
        # prev()'s bare-name check below) -- not here, so `targetp3.t`
        # reached through _attribute still resolves to that mote's real
        # declared unit.
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
        (Ratio.__pow__), so nothing is lost by only handling that."""
        exponent = _literal_number(exponent_node)
        if base is None or exponent is None:
            return None
        if _is_literal(base):
            return LITERAL
        return base * exponent

    def is_own_property(self, node: ast.AST, key: str) -> bool:
        """Whether one whole side of a relation is `key` on this context's
        own mote -- either written bare (`resistance`) or by name
        (`R1.resistance`, which is how a worksheet line writes it).

        Both spellings mean the same property, so unit inference has to
        accept both or a worksheet's own equations would be the one shape
        it never fires for."""
        if isinstance(node, ast.Name):
            return node.id == key
        if not isinstance(node, ast.Attribute) or node.attr != key:
            return False
        try:
            _role, value = self._role_or_value(node)
        except FormulaError:
            return False
        return isinstance(value, _Motes) and value.mote_ids == [self.ctx.mote_id]

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
        identifier = node.value.id
        role = self.ctx.roles(self.ctx.mote_id).get(identifier)
        if role is not None:
            mote_ids = self.ctx.targets(self.ctx.mote_id, role.name)
            problem = roles_repo.arity_problem(role, len(mote_ids))
            if problem is not None:
                raise FormulaError(f"mote {self.ctx.mote_id[:8]}: {problem}")
            return role, _Motes(mote_ids, role.name)

        # Asked in order, and only as far as needed -- see
        # core/formula.py's _role_or_value.
        for scope in (self._connected_by_name, self._named_in_garden):
            matches = scope(identifier)
            if matches is not None:
                return None, _Motes(matches, identifier)

        return None, self._walk(node.value)

    def _connected_by_name(self, name: str) -> list[str] | None:
        """Mirrors core/formula.py's _connected_by_name, down to sharing
        its name_match()."""
        return name_match(
            [
                mid for mid in self.ctx.neighbors(self.ctx.mote_id)
                if self.ctx.mote_name(mid) == name
            ],
            name,
            f"connected to mote {self.ctx.mote_id[:8]}",
        )

    def _named_in_garden(self, name: str) -> list[str] | None:
        """Mirrors core/formula.py's _named_in_garden."""
        return name_match(list(self.ctx.named(name)), name, "in the garden")

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
        if name in TRIG_CALLS:
            return self._trig(name, node)
        if name in LOG_CALLS:
            return self._log(name, node)
        if name == SQRT_CALL:
            return self._sqrt(node)
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

    def _trig(self, name: str, node: ast.Call) -> Dim:
        """sin/cos/tan always produce a dimensionless ratio, regardless of
        what their argument declares -- unlike NUMERIC_CALLS's abs/min/max,
        whose output shares its input's dimension. Still walks the
        argument first so a problem inside it (an unrecognized unit, say)
        is reported rather than silently discarded."""
        if len(node.args) != 1:
            raise FormulaError(f"{name}() takes exactly one argument")
        self.dimension(node.args[0])
        return DIMENSIONLESS

    def _log(self, name: str, node: ast.Call) -> Dim:
        """log/ln/log10/exp take a dimensionless argument and produce a
        dimensionless result. Stricter than _trig, which lets an angle
        through whatever it declares: there is no unit in which the
        logarithm of a length means anything, so a dimensioned argument
        is a modelling mistake worth reporting rather than a convention
        worth accommodating."""
        if len(node.args) != 1:
            raise FormulaError(f"{name}() takes exactly one argument")
        argument = self.dimension(node.args[0])
        if (
            argument is not None
            and not _is_literal(argument)
            and not argument.is_dimensionless
        ):
            self.report(
                f"{name}() takes a dimensionless argument, but its argument is "
                f"{describe(argument)}"
            )
        return DIMENSIONLESS

    def _sqrt(self, node: ast.Call) -> Dim:
        """sqrt(x) halves x's dimension exponents (sqrt(m^2) == m), the
        same rule _power() applies for a literal `** 0.5`."""
        if len(node.args) != 1:
            raise FormulaError("sqrt() takes exactly one argument")
        base = self.dimension(node.args[0])
        if base is None:
            return None
        if _is_literal(base):
            return LITERAL
        return base * 0.5

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

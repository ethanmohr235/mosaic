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
import dataclasses
from collections.abc import Callable
from dataclasses import dataclass, field

from core import roles as roles_repo
from core.formula import (
    AGGREGATE_CALLS,
    CALCULUS_CALLS,
    INTEGRATE_CALL,
    MATH_CONSTANTS,
    QUANTITY_CALL,
    NAVIGATION_CALLS,
    NUMERIC_CALLS,
    HYPERBOLIC_CALLS,
    LOG_CALLS,
    SQRT_CALL,
    TRIG_CALLS,
    PREVIOUS_CALL,
    PROPERTY_CALL,
    FormulaError,
    TICK_NAME,
    function_named,
    is_bare_number,
    quantity_of,
    unparse,
    name_match,
    parse_equation,
    parse_expression,
)
from core import functions as functions_repo
from core.units import (
    DIMENSIONLESS,
    Dimension,
    UnitError,
    describe,
    describe_expression,
    dimension_of,
    is_unit,
    to_base,
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
class _NameScope:
    """The two Context fields core/formula.py's function_named reads."""
    named: Callable[[str], list[str]]
    properties: Callable[[str], dict]


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
    # A mote's properties, for reading a function mote's body and
    # parameters at a call. Defaults to nothing, so no call resolves.
    properties: Callable[[str], dict] = lambda mote_id: {}
    # For unit inference: which keys the solver determines (a solved
    # number is not a bare number -- its unit may simply not be inferred
    # yet), and which of those inference already found to be pure numbers.
    freedoms: Callable[[str], set] = lambda mote_id: set()
    scalars: Callable[[str], set] = lambda mote_id: set()


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
    problem = step_declared_absolute(text, ctx)
    if problem:
        checker.report(problem)
    if not checker.problems:
        for mote_id, key, needed in missing_units(text, ctx):
            value = ctx.properties(mote_id).get(key)
            checker.report(
                f"{ctx.label(mote_id)}.{key} has no unit, but this equation needs it in "
                f"{describe(needed)} -- write it with one (e.g. "
                f"`{ctx.label(mote_id)}.{key} = {value:g} {suggested_unit(needed, ctx)}`), "
                f"or declare [1] if it is a pure number")
    return checker.problems


# A dimension no real quantity has, bound to a unitless given to see how
# its dimension enters an equation (see missing_units).
_PROBE = Dimension(amount=1000.0)


def missing_units(text: str, ctx: DimensionContext) -> list[tuple[str, str, Dimension]]:
    """Each given in `text` that has a number but no unit, and the dimension
    the equation needs it to have -- `p29.cp = 1.3` in a heat balance needs
    J/(kg·K). Reported only when the rest of the equation is dimensioned
    well enough to say so, and the answer is not a pure number.

    Each pair that must measure the same -- the two sides, or the two
    operands of a sum inside them -- is checked with the given bound in
    turn to no dimension and to a probe one: the two residuals (left over
    right) differ by the probe times the power the given enters with, so
    the dimension it needs is the residual over that power. The first
    pair that can tell decides. Other unitless givens are read as the pure numbers they
    are taken for when inferring units."""
    try:
        relation = parse_equation(text)
    except FormulaError:
        return []
    candidates = _unitless_givens(relation, ctx)
    # Every pair of things that must measure the same: the two sides, and
    # the two operands of each sum or difference anywhere in them.
    pairs = [(relation.left, relation.right)] + [
        (node.left, node.right) for side in (relation.left, relation.right)
        for node in ast.walk(side)
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub))]
    found: list[tuple[str, str, Dimension]] = []
    for ref, nodes in candidates.items():
        others = {other: LITERAL for other in candidates if other != ref}
        inside = {id(n) for n in nodes}
        for left, right in pairs:
            if not any(id(n) in inside for side in (left, right) for n in ast.walk(side)):
                continue
            needed = _needed_by_probe(left, right, ref, ctx, others)
            if needed is not None:
                if not needed.is_dimensionless:
                    found.append((*ref, needed))
                break
    return found


def _unitless_givens(relation, ctx: DimensionContext) -> dict[tuple[str, str], list[ast.AST]]:
    checker = _Checker(ctx)
    found: dict[tuple[str, str], list[ast.AST]] = {}
    for side in (relation.left, relation.right):
        for node in ast.walk(side):
            ref = checker.ref_of(node)
            if ref is None:
                continue
            mote_id, key = ref
            if (ctx.units(mote_id).get(key) is None and key not in ctx.freedoms(mote_id)
                    and key not in ctx.scalars(mote_id)
                    and isinstance(ctx.properties(mote_id).get(key), (int, float))
                    and not isinstance(ctx.properties(mote_id).get(key), bool)):
                found.setdefault(ref, []).append(node)
    return found


def _needed_by_probe(left_node, right_node, ref, ctx, others) -> Dim:
    residuals = []
    for bound in (DIMENSIONLESS, _PROBE):
        checker = _Checker(ctx, overrides={**others, ref: bound})
        try:
            left, right = checker.dimension(left_node), checker.dimension(right_node)
        except FormulaError:
            return None
        if left is None or right is None:
            return None
        residuals.append((DIMENSIONLESS if _is_literal(left) else left)
                         - (DIMENSIONLESS if _is_literal(right) else right))
    base, probed = residuals
    power = (probed - base).amount / _PROBE.amount
    if power == 0 or (probed - base) != _PROBE * power:
        return None
    return base * (-1.0 / power)


def suggested_unit(dimension: Dimension, ctx: DimensionContext) -> str:
    """A unit to suggest for `dimension`: its name (see unit_named), else
    the base-SI product."""
    return unit_named(dimension) or dimension.expression


def infer_unit(text: str, key: str, ctx: DimensionContext) -> str | None:
    """infer_dimension's answer, named (see unit_named)."""
    return unit_named(infer_dimension(text, key, ctx))


def infer_dimension(text: str, key: str, ctx: DimensionContext) -> Dim:
    """The dimension of `key`'s value as an equation determines it -- from
    which infer_unit names a unit. DIMENSIONLESS (or a literal) means a
    pure number; None means it cannot be told.

    A reasonable unit for `key` on `ctx.mote_id`, read off the *other*
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

    checker = _Checker(ctx, bare_numbers=True)
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

    return dimension


def unit_named(dimension: Dim) -> str | None:
    """A unit name for `dimension` that round-trips through core.units.unit()
    -- the canonical symbol, else the base-SI product in caret notation --
    or None for an unknown, literal or dimensionless one (nothing to name).
    What infer_unit and a plotted expression's axis both call a dimension."""
    if dimension is None or _is_literal(dimension) or dimension.is_dimensionless:
        return None
    name = describe_expression(dimension)
    return name if is_unit(name) else None


ABSOLUTE, STEP = "absolute", "step"
# The degree each affine temperature unit counts in.
_STEP_OF = {"degF": "degR", "degC": "K"}
# A Rankine value is, in practice, a Fahrenheit-sized difference -- what a
# difference of two degF values is named -- so it keeps the family going.
_FAHRENHEIT_STEP = "degR"
_NAMES = {"degF": "Fahrenheit", "degC": "Celsius"}


def temperature_unit(text: str, key: str, ctx: DimensionContext) -> str | None:
    """The unit a solved temperature is named in, read off the equation
    that determines it: the garden's own temperature unit (degF) for an
    absolute temperature (`Ti + rise`), its degree (degR) for a difference
    (`Thi - Tco`). None when the equation reads no degF or degC; UNDECIDED
    when it does but cannot yet tell which it is (a value it reads has no
    unit yet) -- the caller may wait for one, then names the dimension (K)."""
    try:
        relation = parse_equation(text)
    except FormulaError:
        return None
    checker = _Checker(ctx)
    if checker.is_own_property(relation.left, key):
        other = relation.right
    elif checker.is_own_property(relation.right, key):
        other = relation.left
    else:
        return None
    affine = checker.affine_unit_in(other)
    if affine is None:
        return None
    if affine == _FAHRENHEIT_STEP:
        affine = "degF"
    kind = checker.side_kind(other)
    if kind == ABSOLUTE:
        return affine
    if kind == STEP:
        return _STEP_OF.get(affine)
    return UNDECIDED


UNDECIDED = "?"


def step_declared_absolute(text: str, ctx: DimensionContext) -> str | None:
    """A problem when one side of an equation is a single value declared in
    degF or degC and the other is a temperature *difference* -- 150 °F of
    difference read as a temperature of 150 °F is -309.67 °F."""
    try:
        relation = parse_equation(text)
    except FormulaError:
        return None
    checker = _Checker(ctx)
    for one, other in ((relation.left, relation.right), (relation.right, relation.left)):
        unit_name = checker.unit_of(one)
        if unit_name in _STEP_OF and checker.side_kind(other) == STEP:
            return (f"{_written(other)} is a temperature difference, but {_written(one)} is "
                    f"declared {unit_name}, which reads it as a temperature -- declare it "
                    f"[{_STEP_OF[unit_name]}] (a {_NAMES[unit_name]}-sized degree)")
    return None


def expression_dimension(
    text: str, ctx: DimensionContext, bindings: dict[str, Dim] | None = None,
) -> Dim:
    """What an expression measures, with `bindings` naming free variables'
    dimensions (`r` in `P(r, 0)`, as a plot binds it). None when it cannot
    be told; a bare literal comes back as DIMENSIONLESS. Never raises: a
    malformed expression is the evaluator's to report."""
    dimension = _walk_expression(text, ctx, bindings)
    return DIMENSIONLESS if _is_literal(dimension) else dimension


def _walk_expression(
    text: str, ctx: DimensionContext, bindings: dict[str, Dim] | None,
    calls: tuple[str, ...] = (),
) -> Dim:
    checker = _Checker(ctx, bindings, calls, bare_numbers=True)
    try:
        return checker.dimension(parse_expression(text))
    except FormulaError:
        return None


def function_problem(function_id: str, ctx: DimensionContext) -> str | None:
    """Whether a function's body contradicts the unit it declares for its
    value, walked with each parameter at its declared dimension. None when
    they agree, or when there are not enough units to tell."""
    properties = ctx.properties(function_id)
    declared_name = functions_repo.result_unit(properties)
    if declared_name is None:
        return None
    try:
        declared = dimension_of(declared_name)
        parameters = functions_repo.parameters_of(properties)
    except (UnitError, functions_repo.FunctionDefinitionError):
        return None
    inner = dataclasses.replace(ctx, mote_id=function_id)
    names = _Checker(inner)
    bindings = {p: names._name(function_id, p) for p in parameters}
    body = _walk_expression(functions_repo.body_of(properties), inner, bindings, (function_id,))
    if not _conflict(body, declared):
        return None
    shown = describe(declared)
    named = declared_name if shown == declared_name else f"{declared_name} ({shown})"
    return f"its body is {describe(body)}, but it declares {named} -- these are different dimensions"




_ANGLE_CALLS = frozenset({"sin", "cos", "tan", "sinc"})


def _written(node: ast.AST) -> str:
    """A node as a person would have typed it (`^`, `5 m`)."""
    return unparse(node)


def _has_bare_number(node: ast.AST) -> bool:
    return any(isinstance(n, ast.Constant) and isinstance(n.value, (int, float))
               and not isinstance(n.value, bool) for n in ast.walk(node))


def _single_value(node: ast.AST) -> bool:
    """A lone property, parameter or role read -- what a bare number beside
    it takes its unit from (core/formula.py's _companion_unit)."""
    while isinstance(node, ast.UnaryOp):
        node = node.operand
    return isinstance(node, (ast.Name, ast.Attribute))


def _conflict(a: Dim, b: Dim) -> bool:
    """Two dimensions disagree only when both are actually declared, and
    neither is a literal free to adopt the other's unit."""
    if a is None or b is None or _is_literal(a) or _is_literal(b):
        return False
    return a != b


class _Checker:
    """One walk over one equation, accumulating problems rather than
    raising, so a single pass reports everything wrong at once."""

    def __init__(
        self, ctx: DimensionContext, bindings: dict[str, Dim] | None = None,
        calls: tuple[str, ...] = (), parent: "_Checker | None" = None,
        bare_numbers: bool = False, overrides: dict | None = None,
    ):
        self.ctx = ctx
        # (mote id, key) -> the dimension to read it as, whatever it
        # declares: how missing_units() asks "what would this need to be?"
        self.overrides = overrides if overrides is not None else (
            parent.overrides if parent else {})
        # Inference only: a stored number with no declared unit scales
        # whatever it multiplies or divides, as a literal does. The
        # checker leaves it unknown, which keeps a partly-annotated
        # garden quiet.
        self.bare_numbers = bare_numbers or (parent.bare_numbers if parent else False)
        # Inside a function's body: each parameter's dimension, and the
        # functions being walked (to stop on recursion). A body's problems
        # are the calling equation's problems, so they share one list.
        self.bindings = bindings or {}
        self.calls = calls
        self.problems: list[str] = parent.problems if parent else []
        self._reported_units: set[str] = parent._reported_units if parent else set()

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
            if node.id in self.bindings:
                return self.bindings[node.id]
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
        if (mote_id, key) in self.overrides:
            return self.overrides[(mote_id, key)]
        declared = self.ctx.units(mote_id).get(key)
        if declared is None:
            if self.bare_numbers and self._is_bare_number(mote_id, key):
                return LITERAL
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
        # carries no information of its own here -- beside a single value
        # the evaluator reads it in that value's unit (core/formula.py's
        # _beside). Beside a computed one there is no one unit to adopt,
        # so it is base SI, which is worth saying when the garden works in
        # other units: `1 - (r/51)^2` with r in cm is not what was meant.
        if _is_literal(left) or _is_literal(right):
            bare, other, dim = ((node.left, node.right, right) if _is_literal(left)
                                else (node.right, node.left, left))
            self._bare_beside_computed(bare, other, dim, node.op)
            return right if _is_literal(left) else left
        if _conflict(left, right):
            verb = "added to" if isinstance(node.op, ast.Add) else "subtracted from"
            self.report(
                f"{describe(right)} cannot be {verb} {describe(left)} -- "
                f"different dimensions"
            )
        return left if left is not None else right

    def ref_of(self, node: ast.AST) -> tuple[str, str] | None:
        """(mote id, key) a node reads as a single property, or None."""
        if isinstance(node, ast.Name):
            if (node.id in self.bindings or node.id == TICK_NAME
                    or node.id in MATH_CONSTANTS):
                return None
            return self.ctx.mote_id, node.id
        if isinstance(node, ast.Attribute):
            try:
                _role, value = self._role_or_value(node)
            except FormulaError:
                return None
            if isinstance(value, _Motes) and len(value.mote_ids) == 1:
                return value.mote_ids[0], node.attr
        return None

    def unit_of(self, node: ast.AST) -> str | None:
        """The declared unit of a single value (a property, a role or named
        mote's property, a parameter), or None for anything else."""
        while isinstance(node, ast.UnaryOp):
            node = node.operand
        if isinstance(node, ast.Name) and node.id not in MATH_CONSTANTS:
            return self.ctx.units(self.ctx.mote_id).get(node.id)
        if isinstance(node, ast.Attribute):
            try:
                _role, value = self._role_or_value(node)
            except FormulaError:
                return None
            if isinstance(value, _Motes) and value.mote_ids:
                return self.ctx.units(value.mote_ids[0]).get(node.attr)
        return None

    def affine_unit_in(self, node: ast.AST) -> str | None:
        """The first degF or degC that `node` reads, if any."""
        for inner in ast.walk(node):
            if isinstance(inner, (ast.Name, ast.Attribute)):
                unit_name = self.unit_of(inner)
                if unit_name in _STEP_OF or unit_name == _FAHRENHEIT_STEP:
                    return unit_name
            if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name):
                found = quantity_of(inner)
                result = found[1] if found else self._called_result_unit(inner.func.id)
                if result in _STEP_OF or (found and result == _FAHRENHEIT_STEP):
                    return result
        return None

    def side_kind(self, node: ast.AST) -> str | None:
        """temperature_kind of a whole side of a relation, where a written
        `100 degF` alone is a temperature, as the evaluator reads it."""
        found = quantity_of(node)
        if found is not None and found[1] in _STEP_OF:
            return ABSOLUTE
        return self.temperature_kind(node)

    def temperature_kind(self, node: ast.AST) -> str | None:
        """Whether `node` is an absolute temperature or a step of one: a
        degF value is absolute; absolute minus absolute is a step; absolute
        plus or minus a step (or a bare number) is absolute; a product, a
        quotient or a power is a step. None when it cannot be told -- a
        kelvin value may be either."""
        if isinstance(node, ast.Constant):
            return "literal"
        if isinstance(node, (ast.Name, ast.Attribute)):
            unit_name = self.unit_of(node)
            if unit_name in _STEP_OF:
                return ABSOLUTE
            if unit_name == _FAHRENHEIT_STEP:
                return STEP
            return "kelvin" if unit_name == "K" else None
        if isinstance(node, ast.UnaryOp):
            inner = self.temperature_kind(node.operand)
            return STEP if inner in (ABSOLUTE, STEP) else inner
        if isinstance(node, ast.BinOp):
            if isinstance(node.op, (ast.Add, ast.Sub)):
                a, b = self.temperature_kind(node.left), self.temperature_kind(node.right)
                # A kelvin value beside a degF or degC one is its step --
                # `Tc - dT` with the rise solved in K.
                steps = (STEP, "literal", "kelvin")
                if isinstance(node.op, ast.Sub) and a == ABSOLUTE and b == ABSOLUTE:
                    return STEP
                if ABSOLUTE in (a, b) and ({a, b} - {ABSOLUTE}) <= set(steps):
                    return ABSOLUTE
                if a in (STEP, "literal") and b in (STEP, "literal"):
                    return STEP if STEP in (a, b) else "literal"
                return None
            return STEP
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            found = quantity_of(node)
            if found is not None:
                return STEP if found[1] in _STEP_OF or found[1] == _FAHRENHEIT_STEP else None
            if node.func.id in NUMERIC_CALLS:
                kinds = [self.temperature_kind(a) for a in node.args]
                return ABSOLUTE if ABSOLUTE in kinds else (STEP if STEP in kinds else None)
            result = self._called_result_unit(node.func.id)
            if result in _STEP_OF:
                return ABSOLUTE
            return STEP
        return None

    def _called_result_unit(self, name: str) -> str | None:
        try:
            function_id = function_named(self._evaluator_view(), name)
        except FormulaError:
            return None
        if function_id is None:
            return None
        return functions_repo.result_unit(self.ctx.properties(function_id))

    def _bare_beside_computed(self, bare: ast.AST, other: ast.AST, dim: Dim, op) -> None:
        if (dim is None or _is_literal(dim) or dim.is_dimensionless
                or not is_bare_number(bare) or _single_value(other)
                or not self._reads_non_si(other)):
            return
        verb = "added to" if isinstance(op, ast.Add) else "subtracted from"
        base = unit_named(dim) or describe(dim)
        self.report(
            f"{_written(bare)} is {verb} {_written(other)} (in {describe(dim)}), so it is "
            f"read as {_written(bare)} {base} -- a bare number beside a computed value is in "
            f"base SI units; give it a unit if it means something else")

    def _reads_non_si(self, node: ast.AST) -> bool:
        """Whether `node` reads a value declared in a unit other than its
        dimension's base one (cm, degF, psi) -- where a bare number read
        in base SI is likely a slip rather than the intent."""
        for inner in ast.walk(node):
            unit_name = None
            if isinstance(inner, ast.Name) and inner.id not in MATH_CONSTANTS:
                unit_name = self.ctx.units(self.ctx.mote_id).get(inner.id)
            elif isinstance(inner, ast.Attribute):
                try:
                    _role, value = self._role_or_value(inner)
                except FormulaError:
                    continue
                if isinstance(value, _Motes) and value.mote_ids:
                    unit_name = self.ctx.units(value.mote_ids[0]).get(inner.attr)
            if unit_name:
                try:
                    if to_base(1.0, unit_name) != 1.0 or to_base(0.0, unit_name) != 0.0:
                        return True
                except UnitError:
                    continue
        return False

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

        if name == QUANTITY_CALL:
            found = quantity_of(node)
            try:
                return dimension_of(found[1]) if found else None
            except UnitError:
                return None
        if name in NAVIGATION_CALLS:
            return self._navigate(name, node)
        if name in AGGREGATE_CALLS:
            return self._aggregate(name, node)
        if name in NUMERIC_CALLS:
            return self._combine([self.dimension(a) for a in node.args], name)
        if name in TRIG_CALLS:
            return self._trig(name, node)
        if name in LOG_CALLS or name in HYPERBOLIC_CALLS:
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
        if name in CALCULUS_CALLS:
            return self._calculus(name, node)
        return self._function_call(name, node)

    def _function_call(self, name: str, node: ast.Call) -> Dim:
        """A function mote's body, walked with each parameter bound to a
        dimension: the one it declares, else its argument's. Anything the
        evaluator itself refuses (an unknown name, the wrong arity,
        recursion) is raised for check_equation to leave to the run."""
        function_id, parameters = self._function(name)
        if len(parameters) != len(node.args):
            raise FormulaError(f"{name}() takes {len(parameters)} arguments")
        bindings = {
            parameter: self._argument(name, function_id, parameter, arg)
            for parameter, arg in zip(parameters, node.args)
        }
        body = self._body(function_id, bindings)
        return body if body is not None else self._result(function_id)

    def _is_bare_number(self, mote_id: str, key: str) -> bool:
        """A number stored with no unit that is a given -- or a solved
        value inference already found to be a pure number (`N = 193 *
        204`). A solved value whose unit is merely not inferred yet is
        unknown, not a scalar: reading it as one names the quantities
        downstream of it wrongly (`A = Q / (U * LMTD)` read as m²·K)."""
        if key in self.ctx.scalars(mote_id):
            return True
        if key in self.ctx.freedoms(mote_id):
            return False
        value = self.ctx.properties(mote_id).get(key)
        return isinstance(value, (int, float)) and not isinstance(value, bool)

    def _result(self, function_id: str) -> Dim:
        """The dimension a function declares for its value, for a body that
        cannot tell its own (a bare coefficient, say). None when it
        declares none, or one that does not read."""
        unit_name = functions_repo.result_unit(self.ctx.properties(function_id))
        if unit_name is None:
            return None
        try:
            return dimension_of(unit_name)
        except UnitError:
            return None

    def _calculus(self, name: str, node: ast.Call) -> Dim:
        """`integrate(f, a, b)` is f's dimension times its parameter's, and
        `deriv(f, x0)` is f's over it. Arguments are checked as a call's
        are, the bounds against the parameter they bound; a malformed call
        is left to the evaluator to refuse, as a call's is."""
        if not node.args or not isinstance(node.args[0], ast.Name):
            raise FormulaError(f"{name}() takes a function's name first")
        function = node.args[0].id
        function_id, parameters = self._function(function)
        rest = node.args[1:]
        named = bool(rest) and isinstance(rest[0], ast.Constant) and isinstance(rest[0].value, str)
        parameter = rest[0].value if named else (parameters[0] if len(parameters) == 1 else None)
        if parameter not in parameters:
            raise FormulaError(f"{name}({function}, ...) does not name one of its parameters")
        rest = rest[1:] if named else rest
        count = 2 if name == INTEGRATE_CALL else 1
        others = [p for p in parameters if p != parameter]
        if len(rest) != count + len(others):
            raise FormulaError(f"{name}({function}, ...) has the wrong number of arguments")
        own = [self._argument(function, function_id, parameter, a) for a in rest[:count]]
        bindings = {parameter: own[0], **{
            p: self._argument(function, function_id, p, a) for p, a in zip(others, rest[count:])}}
        body, variable = self._body(function_id, bindings), own[0]
        if body is None or variable is None:
            return None
        return body + variable if name == INTEGRATE_CALL else body - variable

    def _function(self, name: str) -> tuple[str, list[str]]:
        """The function mote `name` calls, and its parameters."""
        function_id = function_named(self._evaluator_view(), name)
        if function_id in self.calls:
            raise FormulaError(f"{name}() calls itself")
        try:
            return function_id, functions_repo.parameters_of(self.ctx.properties(function_id))
        except functions_repo.FunctionDefinitionError as e:
            raise FormulaError(str(e)) from e

    def _body(self, function_id: str, bindings: dict[str, Dim]) -> Dim:
        inner = _Checker(dataclasses.replace(self.ctx, mote_id=function_id),
                         bindings, (*self.calls, function_id), parent=self)
        body = functions_repo.body_of(self.ctx.properties(function_id))
        return inner.dimension(parse_expression(body))

    def _argument(self, name: str, function_id: str, parameter: str, node: ast.AST) -> Dim:
        """One argument's dimension as the body sees it. A bare number into
        a parameter with no unit is read as base SI -- legal, and exactly
        the slip (`phi(5)` meaning 5 cm) worth a warning."""
        declared = self._name(function_id, parameter)
        undeclared = parameter not in self.ctx.units(function_id)
        if is_bare_number(node):
            if undeclared:
                self.report(
                    f"{name}({ast.unparse(node)}): {ast.unparse(node)} is passed to "
                    f"{parameter}, which declares no unit, so it is read in base SI units "
                    f"-- declare one on {name} (or [1] for a pure number)"
                )
            return declared if declared is not None else LITERAL
        actual = self.dimension(node)
        if declared is not None and _conflict(actual, declared):
            self.report(
                f"{name}(): {parameter} declares {describe(declared)}, but is passed "
                f"{describe(actual)}"
            )
        return declared if declared is not None else actual

    def _evaluator_view(self):
        """Just enough of a formula Context for formula.function_named, so
        the two walks resolve a call's name the one same way."""
        return _NameScope(self.ctx.named, self.ctx.properties)

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
        argument = self.dimension(node.args[0])
        if (name in _ANGLE_CALLS and argument is not None and not _is_literal(argument)
                and not argument.is_dimensionless):
            hint = (" -- a bare number in it is read in base SI units; give it a unit"
                    if _has_bare_number(node.args[0]) else "")
            self.report(f"{name}() takes an angle (a pure number), but "
                        f"{_written(node.args[0])} is in {describe(argument)}{hint}")
        return DIMENSIONLESS

    def _log(self, name: str, node: ast.Call) -> Dim:
        """log/ln/log10/exp and the hyperbolic calls take a dimensionless
        argument and produce a dimensionless result. Stricter than _trig,
        which lets an angle through whatever it declares: there is no unit
        in which the logarithm (or sinh) of a length means anything, so a
        dimensioned argument is a modelling mistake worth reporting rather
        than a convention worth accommodating."""
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

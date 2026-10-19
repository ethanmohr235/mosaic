import math

import pytest

from core.formula import (
    Context,
    FormulaError,
    IndeterminateError,
    LinearExpr,
    NonlinearError,
    attribute_bases,
    evaluate_equation,
    evaluate_expression,
)

# A tiny fixed garden, expressed as plain dicts -- core/formula.py takes
# callables rather than a connection precisely so it can be tested without
# touching a database.
PROPERTIES = {
    "R1": {"resistance": 100, "label": "a string"},
    "N1": {"t": 999},
    "N2": {},
    "N3": {},
}
UNKNOWNS = {"R1": {"current"}, "N1": {"voltage"}, "N2": {"voltage"}, "N3": {"voltage"}}
RELATIONSHIPS = [("R1", "N1", "a"), ("R1", "N2", "b"), ("R1", "N3", "b")]


def context(mote_id="R1", solved=None, tick=0.0, previous=None, evaluate_model=None):
    kwargs = dict(
        mote_id=mote_id,
        properties=lambda mid: PROPERTIES.get(mid, {}),
        unknown_keys=lambda mid: UNKNOWNS.get(mid, set()),
        targets=lambda mid, t: [to for f, to, ty in RELATIONSHIPS if f == mid and ty == t],
        sources=lambda mid, t: [f for f, to, ty in RELATIONSHIPS if to == mid and ty == t],
        solved=solved or {},
        tick=tick,
        previous=lambda mid: (previous or {}).get(mid, {}),
    )
    # Omitted rather than passed as None, so callers that don't care about
    # property() still exercise Context's real "not wired up" default.
    if evaluate_model is not None:
        kwargs["evaluate_model"] = evaluate_model
    return Context(**kwargs)


# ---- LinearExpr algebra ----

def test_linear_expr_addition_merges_terms():
    a = LinearExpr({("m", "x"): 1.0}, 2.0)
    b = LinearExpr({("m", "x"): 3.0}, 5.0)
    total = a + b
    assert total.terms == {("m", "x"): 4.0}
    assert total.constant == 7.0


def test_cancelling_terms_leaves_a_constant():
    expr = LinearExpr({("m", "x"): 1.0}, 0.0) - LinearExpr({("m", "x"): 1.0}, 0.0)
    assert expr.is_constant


def test_scaling_by_zero_drops_terms():
    expr = LinearExpr({("m", "x"): 4.0}, 3.0) * LinearExpr.number(0)
    assert expr.is_constant
    assert expr.constant == 0.0


def test_multiplying_two_unknowns_is_rejected():
    unknown = LinearExpr({("m", "x"): 1.0}, 0.0)
    with pytest.raises(NonlinearError):
        unknown * unknown


def test_dividing_by_an_unknown_is_rejected():
    with pytest.raises(NonlinearError):
        LinearExpr.number(1) / LinearExpr({("m", "x"): 1.0}, 0.0)


def test_dividing_by_zero_is_rejected():
    with pytest.raises(FormulaError):
        LinearExpr.number(1) / LinearExpr.number(0)


def test_dividing_by_a_tiny_but_nonzero_constant_is_not_treated_as_zero():
    # A neutron mass in kg (~1.6749e-27) is far below the old flat 1e-9
    # "close enough to zero" threshold, but it's a real divisor, not
    # float noise -- v = sqrt(2E/m) must actually divide by it.
    neutron_mass = LinearExpr.number(1.6749e-27)
    energy = LinearExpr.number(3.204e-13)
    result = energy / neutron_mass
    assert result.constant == pytest.approx(3.204e-13 / 1.6749e-27)


def test_scaling_by_a_tiny_but_nonzero_factor_keeps_the_term():
    # Same failure mode as division, in the other primitive that used the
    # flat threshold: multiplying an unknown by a physically tiny
    # constant (an eV-to-joule conversion, say) must not erase the term.
    expr = LinearExpr({("m", "x"): 1.0}, 0.0) * LinearExpr.number(1.602176634e-19)
    assert expr.terms == {("m", "x"): pytest.approx(1.602176634e-19)}


def test_raising_an_unknown_to_a_power_is_rejected():
    with pytest.raises(NonlinearError):
        LinearExpr({("m", "x"): 1.0}, 0.0) ** LinearExpr.number(2)


# Every way float exponentiation can fail, as a FormulaError rather than a
# raw arithmetic error escaping the module. An iterative solver backend
# re-evaluates a rule at each guess point, so these are reachable from a
# perfectly well-formed rule -- `e ** (-k * x)` overflows as soon as a
# Newton step walks x far enough negative.

def test_overflowing_power_raises_a_formula_error():
    with pytest.raises(FormulaError, match="too large to represent"):
        LinearExpr.number(math.e) ** LinearExpr.number(1000.0)


def test_zero_to_a_negative_power_raises_a_formula_error():
    with pytest.raises(FormulaError, match="zero to a negative power"):
        LinearExpr.number(0.0) ** LinearExpr.number(-1.0)


def test_negative_base_to_a_fractional_power_raises_a_formula_error():
    # Python returns a *complex* number here rather than raising, which
    # would otherwise flow on into a solver as a number that isn't one.
    with pytest.raises(FormulaError, match="not a real number"):
        LinearExpr.number(-8.0) ** LinearExpr.number(0.5)


def test_ordinary_powers_still_evaluate():
    assert (LinearExpr.number(2.0) ** LinearExpr.number(10.0)).value() == 1024.0
    assert (LinearExpr.number(-8.0) ** LinearExpr.number(3.0)).value() == -512.0


def test_overflowing_power_through_a_ratio_raises_a_formula_error():
    # Ratio.__pow__ is a separate code path from LinearExpr.__pow__ and
    # was missing the same guard.
    with pytest.raises(FormulaError, match="too large to represent"):
        evaluate_expression("e ** 1000", context())


# ---- evaluation ----

def test_bare_name_reads_own_property():
    assert evaluate_expression("resistance", context()).constant == 100


def test_bare_name_declared_unknown_becomes_a_term():
    expr = evaluate_expression("current", context())
    assert expr.terms == {("R1", "current"): 1.0}


def test_solved_unknown_substitutes_as_a_number():
    expr = evaluate_expression("current", context(solved={("R1", "current"): 7.0}))
    assert expr.is_constant
    assert expr.constant == 7.0


def test_single_target_collapses_to_a_scalar():
    expr = evaluate_expression("targets('a').voltage", context())
    assert expr.terms == {("N1", "voltage"): 1.0}


def test_multiple_targets_require_sum():
    with pytest.raises(FormulaError, match="sum"):
        evaluate_expression("targets('b').voltage", context())


def test_sum_over_multiple_targets():
    expr = evaluate_expression("sum(targets('b').voltage)", context())
    assert expr.terms == {("N2", "voltage"): 1.0, ("N3", "voltage"): 1.0}


def test_sum_of_no_motes_is_zero():
    expr = evaluate_expression("sum(targets('nope').voltage)", context())
    assert expr.is_constant
    assert expr.constant == 0.0


def test_sources_navigates_incoming_relationships():
    expr = evaluate_expression("sum(sources('a').current)", context(mote_id="N1"))
    assert expr.terms == {("R1", "current"): 1.0}


def test_count_counts_motes():
    assert evaluate_expression("count(targets('b'))", context()).constant == 2


def test_arithmetic_with_known_values_is_allowed_to_be_nonlinear():
    expr = evaluate_expression("2 * (100 / 25) ** 0.5", context())
    assert expr.constant == pytest.approx(4.0)


def test_caret_is_accepted_as_exponentiation():
    # `^` reads as bitwise XOR to Python's own grammar, but as power to
    # anyone coming from math notation, spreadsheets, or units.py's own
    # `kg*m/s^2` -- it should mean the same thing here.
    expr = evaluate_expression("2 * (100 / 25) ^ 0.5", context())
    assert expr.constant == pytest.approx(4.0)


def test_equation_becomes_lhs_minus_rhs():
    equation = evaluate_equation("current == 5", context())
    assert equation.terms == {("R1", "current"): 1.0}
    assert equation.constant == -5.0


def test_ohms_law_produces_expected_coefficients():
    relationships = [("R1", "N1", "a"), ("R1", "N2", "b")]
    ctx = Context(
        mote_id="R1",
        properties=lambda mid: PROPERTIES.get(mid, {}),
        unknown_keys=lambda mid: UNKNOWNS.get(mid, set()),
        targets=lambda mid, t: [to for f, to, ty in relationships if f == mid and ty == t],
        sources=lambda mid, t: [f for f, to, ty in relationships if to == mid and ty == t],
        solved={},
    )
    equation = evaluate_equation(
        "targets('a').voltage - targets('b').voltage == current * resistance", ctx
    )
    assert equation.terms == {
        ("N1", "voltage"): 1.0,
        ("N2", "voltage"): -1.0,
        ("R1", "current"): -100.0,
    }
    assert equation.constant == 0.0


# ---- unknowns under a division bar ----

# The engine clears denominators at the relation, so an equation that is
# only nonlinear as *written* is solved as the linear equation it is
# equivalent to -- no hand-rearranging. See core/formula.py's Ratio.

def test_unknown_denominator_is_cross_multiplied():
    # `I / I0 == 0.1` with I0 unknown used to raise "cannot divide by an
    # unknown quantity"; it means the same thing as `I == 0.1 * I0`.
    ctx = Context(
        mote_id="B",
        properties=lambda mid: {"B": {"I": 4.0}}.get(mid, {}),
        unknown_keys=lambda mid: {"B": {"I0"}}.get(mid, set()),
        targets=lambda mid, t: [],
        sources=lambda mid, t: [],
    )
    equation = evaluate_equation("I / I0 == 0.1", ctx)
    # 4 - 0.1 * I0 == 0, i.e. I0 == 40
    assert equation.terms == {("B", "I0"): pytest.approx(-0.1)}
    assert equation.constant == pytest.approx(4.0)


def test_reciprocal_of_an_unknown():
    equation = evaluate_equation("1 / current == 0.25", context())
    assert equation.terms == {("R1", "current"): pytest.approx(-0.25)}
    assert equation.constant == pytest.approx(1.0)


def test_unknown_to_the_minus_one_is_the_same_reciprocal():
    equation = evaluate_equation("current ^ -1 == 0.25", context())
    assert equation.terms == {("R1", "current"): pytest.approx(-0.25)}
    assert equation.constant == pytest.approx(1.0)


def test_matching_denominators_add_without_going_nonlinear():
    # Cross-multiplying these would square the denominator and multiply
    # the numerators by an unknown; they share a denominator, so they
    # add directly instead.
    equation = evaluate_equation(
        "resistance / current + resistance / current == 4", context()
    )
    assert equation.terms == {("R1", "current"): pytest.approx(-4.0)}
    assert equation.constant == pytest.approx(200.0)


def test_denominator_may_be_a_whole_expression():
    equation = evaluate_equation("100 / (2 * current) == 5", context())
    assert equation.terms == {("R1", "current"): pytest.approx(-10.0)}
    assert equation.constant == pytest.approx(100.0)


def test_two_unknowns_multiplied_across_the_bar_is_still_nonlinear():
    # `x / y == z` is a product of unknowns after clearing -- no
    # rearrangement makes it linear, so it still defers to a solver.
    ctx = Context(
        mote_id="B",
        properties=lambda mid: {},
        unknown_keys=lambda mid: {"x", "y", "z"},
        targets=lambda mid, t: [],
        sources=lambda mid, t: [],
    )
    with pytest.raises(NonlinearError):
        evaluate_equation("x / y == z", ctx)


def test_dividing_by_a_literal_zero_is_still_rejected():
    with pytest.raises(FormulaError, match="division by zero"):
        evaluate_equation("resistance / 0 == 1", context())


def test_solved_denominator_divides_through_normally():
    equation = evaluate_equation(
        "resistance / current == 25", context(solved={("R1", "current"): 4.0})
    )
    assert equation.is_equation
    assert equation.terms == {}
    assert equation.constant == pytest.approx(0.0)


def test_constraint_with_an_unknown_denominator_is_deferred_not_guessed():
    # Multiplying `a / x <= b` through by x flips the comparison when x
    # is negative, so this waits for a value rather than picking a sign.
    with pytest.raises(NonlinearError, match="flips <="):
        evaluate_equation("resistance / current <= 25", context())


def test_constraint_with_a_solved_denominator_is_checked():
    equation = evaluate_equation(
        "resistance / current <= 25", context(solved={("R1", "current"): 4.0})
    )
    assert equation.operator == "<="
    assert equation.constant == pytest.approx(0.0)


def test_bare_expression_still_rejects_an_unknown_denominator():
    # Nothing to clear a denominator against outside a relation.
    with pytest.raises(NonlinearError, match="divide by an unknown"):
        evaluate_expression("resistance / current", context())


# ---- logarithms ----

def test_log_is_the_natural_logarithm():
    assert evaluate_expression("log(e)", context()).constant == pytest.approx(1.0)
    assert evaluate_expression("ln(e)", context()).constant == pytest.approx(1.0)


def test_log10_is_base_ten():
    assert evaluate_expression("log10(1000)", context()).constant == pytest.approx(3.0)


def test_exp_inverts_log():
    assert evaluate_expression("exp(log(7))", context()).constant == pytest.approx(7.0)


def test_log_defers_on_an_unknown_argument():
    # Same rule as sin/sqrt: deferred until the substitution loop has a
    # number, not attempted symbolically.
    with pytest.raises(NonlinearError):
        evaluate_expression("log(current)", context())


def test_log_of_a_non_positive_number_is_an_error():
    with pytest.raises(FormulaError, match="must be positive"):
        evaluate_expression("log(0)", context())
    with pytest.raises(FormulaError, match="must be positive"):
        evaluate_expression("log10(-1)", context())


def test_log_of_a_ratio_makes_an_exponential_law_linear():
    # `mxs * x == -ln(I / I0)` -- the exact, linear way to write what
    # `I == I0 * exp(-mxs * x)` says, once I and I0 are known.
    ctx = Context(
        mote_id="B",
        properties=lambda mid: {"B": {"I": 1.0, "I0": 10.0, "mxs": 2.0}}.get(mid, {}),
        unknown_keys=lambda mid: {"B": {"x"}}.get(mid, set()),
        targets=lambda mid, t: [],
        sources=lambda mid, t: [],
    )
    equation = evaluate_equation("mxs * x == -ln(I / I0)", ctx)
    assert equation.terms == {("B", "x"): pytest.approx(2.0)}
    assert equation.constant == pytest.approx(-math.log(10.0))


# ---- time-based simulation: t and prev() ----

def test_t_resolves_to_the_context_tick():
    assert evaluate_expression("t", context(tick=3)).constant == 3.0


def test_t_shadows_a_same_named_property():
    # A mote with a real property literally named "t" still gets the
    # reserved word's value inside a formula, not its own property.
    ctx = Context(
        mote_id="R1",
        properties=lambda mid: {"t": 999},
        unknown_keys=lambda mid: set(),
        targets=lambda mid, t: [],
        sources=lambda mid, t: [],
        tick=7.0,
    )
    assert evaluate_expression("t", ctx).constant == 7.0


def test_t_qualified_by_a_role_reads_the_real_property_not_the_tick():
    # Unlike a bare `t`, `targets('a').t` unambiguously names another
    # mote's real property -- it must not be shadowed by the reserved tick.
    assert evaluate_expression("targets('a').t", context(tick=7.0)).constant == 999.0


# ---- math constants: pi and e ----

def test_pi_and_e_resolve_to_their_values():
    assert evaluate_expression("pi", context()).constant == pytest.approx(math.pi)
    assert evaluate_expression("e", context()).constant == pytest.approx(math.e)


def test_math_constants_participate_in_arithmetic():
    result = evaluate_expression("2 * pi", context())
    assert result.constant == pytest.approx(2 * math.pi)


def test_pi_shadows_a_same_named_property():
    ctx = Context(
        mote_id="R1",
        properties=lambda mid: {"pi": 999},
        unknown_keys=lambda mid: set(),
        targets=lambda mid, t: [],
        sources=lambda mid, t: [],
    )
    assert evaluate_expression("pi", ctx).constant == pytest.approx(math.pi)


def test_e_qualified_by_a_role_reads_the_real_property_not_the_constant():
    # targets('a') reaches only N1, so it collapses to a scalar.
    properties = {**PROPERTIES, "N1": {**PROPERTIES["N1"], "e": 42}}
    ctx = Context(
        mote_id="R1",
        properties=lambda mid: properties.get(mid, {}),
        unknown_keys=lambda mid: UNKNOWNS.get(mid, set()),
        targets=lambda mid, t: [to for f, to, ty in RELATIONSHIPS if f == mid and ty == t],
        sources=lambda mid, t: [f for f, to, ty in RELATIONSHIPS if to == mid and ty == t],
    )
    assert evaluate_expression("targets('a').e", ctx).constant == 42.0


def test_prev_reads_the_previous_value():
    ctx = context(previous={"R1": {"resistance": 50}})
    assert evaluate_expression("prev(resistance)", ctx).constant == 50.0


def test_prev_on_an_unknown_reads_its_prior_solved_value_not_this_ticks():
    ctx = context(previous={"R1": {"current": 2.0}}, solved={})
    # "current" is declared an unknown for R1 -- prev() must still read
    # the snapshot, not go through ctx.solved (which is this step's,
    # still-being-determined value).
    assert evaluate_expression("prev(current)", ctx).constant == 2.0


def test_prev_requires_a_bare_name_argument():
    with pytest.raises(FormulaError, match="bare property name"):
        evaluate_expression("prev('resistance')", context())
    with pytest.raises(FormulaError, match="bare property name"):
        evaluate_expression("prev(resistance + 1)", context())


def test_prev_missing_key_is_reported_clearly():
    with pytest.raises(FormulaError, match="no previous value"):
        evaluate_expression("prev(resistance)", context(previous={}))


def test_prev_non_numeric_previous_value_is_rejected():
    with pytest.raises(FormulaError, match="not a number"):
        evaluate_expression("prev(label)", context(previous={"R1": {"label": "a string"}}))


def test_prev_unavailable_by_default_when_not_wired_up():
    # A caller that doesn't pass `previous` at all (e.g. a one-shot,
    # non-time-based evaluation) gets a clear error, not a silent {}.
    ctx = Context(
        mote_id="R1",
        properties=lambda mid: PROPERTIES.get(mid, {}),
        unknown_keys=lambda mid: UNKNOWNS.get(mid, set()),
        targets=lambda mid, t: [],
        sources=lambda mid, t: [],
    )
    with pytest.raises(FormulaError, match="no previous value"):
        evaluate_expression("prev(resistance)", ctx)


# ---- errors ----

def test_missing_property_names_the_mote_and_key():
    with pytest.raises(FormulaError, match="voltage"):
        evaluate_expression("voltage", context())


def test_non_numeric_property_is_rejected():
    with pytest.raises(FormulaError, match="not a number"):
        evaluate_expression("label", context())


def test_equation_without_comparison_is_rejected():
    with pytest.raises(FormulaError, match="not a relation"):
        evaluate_equation("current * resistance", context())


def test_chained_comparison_is_rejected():
    with pytest.raises(FormulaError, match="exactly one"):
        evaluate_equation("current == resistance == 1", context())


def test_syntax_error_is_reported_clearly():
    with pytest.raises(FormulaError, match="could not parse"):
        evaluate_expression("current +", context())


@pytest.mark.parametrize("source", [
    "__import__('os')",
    "().__class__",
    "open('/etc/passwd')",
    "[x for x in (1, 2)]",
    "(lambda: 1)()",
    "resistance if resistance else 0",
    "{'a': 1}",
    "f'{resistance}'",
])
def test_unsafe_or_unsupported_syntax_is_rejected(source):
    with pytest.raises(FormulaError):
        evaluate_expression(source, context())


def test_subscript_is_rejected():
    with pytest.raises(FormulaError, match="Subscript"):
        evaluate_expression("resistance[0]", context())


def test_attribute_access_on_a_number_is_rejected():
    with pytest.raises(FormulaError, match="targets"):
        evaluate_expression("resistance.real", context())


def test_unknown_function_is_rejected():
    with pytest.raises(FormulaError, match="unknown function"):
        evaluate_expression("frobnicate(4)", context())


def test_navigation_requires_a_quoted_relationship_type():
    with pytest.raises(FormulaError, match="in quotes"):
        evaluate_expression("targets(resistance).voltage", context())


def test_keyword_arguments_are_rejected():
    with pytest.raises(FormulaError, match="keyword"):
        evaluate_expression("targets(type='a')", context())


def test_min_max_reject_unknowns():
    with pytest.raises(NonlinearError):
        evaluate_expression("max(current, 1)", context())


# ---- sin/cos/tan/sqrt ----

def test_trig_functions_take_radians():
    assert evaluate_expression("sin(0)", context()).constant == pytest.approx(0.0)
    assert evaluate_expression(f"cos({math.pi})", context()).constant == pytest.approx(-1.0)
    assert evaluate_expression("tan(0)", context()).constant == pytest.approx(0.0)


def test_sqrt():
    assert evaluate_expression("sqrt(9)", context()).constant == pytest.approx(3.0)


def test_sqrt_of_a_negative_number_is_rejected():
    with pytest.raises(FormulaError, match="negative"):
        evaluate_expression("sqrt(-1)", context())


@pytest.mark.parametrize(
    "name", ["sin", "cos", "tan", "arcsin", "arccos", "arctan", "sinc", "sqrt"])
def test_trig_and_sqrt_defer_on_unknowns(name):
    with pytest.raises(NonlinearError):
        evaluate_expression(f"{name}(current)", context())


def test_arc_trig_functions_invert_their_trig_counterparts():
    assert evaluate_expression("arcsin(1)", context()).constant == pytest.approx(math.pi / 2)
    assert evaluate_expression("arccos(-1)", context()).constant == pytest.approx(math.pi)
    assert evaluate_expression("arctan(1)", context()).constant == pytest.approx(math.pi / 4)


@pytest.mark.parametrize("name", ["arcsin", "arccos"])
def test_arcsin_arccos_reject_arguments_outside_unit_range(name):
    with pytest.raises(FormulaError, match="between -1 and 1"):
        evaluate_expression(f"{name}(2)", context())


def test_sinc_is_the_unnormalized_sin_x_over_x():
    assert evaluate_expression("sinc(0)", context()).constant == 1.0
    assert evaluate_expression("sinc(pi / 2)", context()).constant == pytest.approx(2 / math.pi)
    assert evaluate_expression("sinc(-1)", context()).constant == pytest.approx(math.sin(1))


# ---- 0/0 ----

def test_zero_over_zero_is_indeterminate_not_a_pole():
    with pytest.raises(IndeterminateError, match="sinc"):
        evaluate_expression("sin(0) / 0", context())


def test_a_nonzero_numerator_over_zero_is_a_pole():
    with pytest.raises(FormulaError, match="division by zero") as raised:
        evaluate_expression("1 / (resistance - 100)", context())
    assert not isinstance(raised.value, IndeterminateError)


def test_zero_over_zero_inside_a_relation_is_indeterminate():
    with pytest.raises(IndeterminateError):
        evaluate_equation("current == (resistance - 100) / (resistance - 100)", context())


# ---- property() ----

def test_property_calls_evaluate_model_with_constant_args():
    calls = []

    def fake_evaluate_model(mote_id, relationship_type, operation, args):
        calls.append((mote_id, relationship_type, operation, args))
        return 42.0

    expr = evaluate_expression(
        "property('working_fluid', 'enthalpy', 8000000, 500)",
        context(evaluate_model=fake_evaluate_model),
    )
    assert expr.is_constant
    assert expr.constant == 42.0
    assert calls == [("R1", "working_fluid", "enthalpy", [8000000.0, 500.0])]


def test_property_defers_until_its_arguments_are_known():
    # "current" is R1's declared unknown, so it isn't a constant yet --
    # property() must behave like abs()/min()/max() and defer rather than
    # calling evaluate_model with a symbolic argument.
    ctx = context(evaluate_model=lambda *a: 1.0)
    with pytest.raises(NonlinearError):
        evaluate_expression("property('a', 'op', current)", ctx)


def test_property_requires_at_least_two_arguments():
    with pytest.raises(FormulaError, match="relationship type"):
        evaluate_expression("property('a')", context())


def test_property_requires_a_quoted_relationship_type():
    with pytest.raises(FormulaError, match="relationship type"):
        evaluate_expression("property(resistance, 'op', 1)", context())


def test_property_requires_a_quoted_operation_name():
    with pytest.raises(FormulaError, match="operation name"):
        evaluate_expression("property('a', resistance, 1)", context())


def test_property_unavailable_by_default_when_not_wired_up():
    # Mirrors prev()'s "unavailable unless wired up" default -- a caller
    # that doesn't pass evaluate_model gets a clear error, not an
    # AttributeError.
    with pytest.raises(FormulaError, match="not available"):
        evaluate_expression("property('a', 'op', 1)", context())


def test_property_rejects_a_non_numeric_result():
    ctx = context(evaluate_model=lambda *a: "not a number")
    with pytest.raises(FormulaError, match="not a number"):
        evaluate_expression("property('a', 'op', 1)", ctx)


# ---- connected-name references ----
#
# A dotted identifier that isn't a declared role can still reach a mote
# connected to the anchor -- any relationship type, either direction --
# by that mote's `name` property, without targets()/sources() and
# without a role declaration. See core/formula.py's _connected_by_name.

def _named_context(mote_id, properties, neighbor_map, roles=None, garden=False):
    """`garden=True` turns on the garden-scope fallback, indexing every
    mote in `properties` by its `name` the way core/simulation.py's
    _build_view does. Off by default so the connected-name tests below
    still exercise that scope alone."""
    by_name = {}
    if garden:
        for mid, props in properties.items():
            name = props.get("name")
            if isinstance(name, str) and name:
                by_name.setdefault(name, []).append(mid)
    return Context(
        mote_id=mote_id,
        properties=lambda mid: properties.get(mid, {}),
        unknown_keys=lambda mid: set(),
        targets=lambda mid, t: [],
        sources=lambda mid, t: [],
        neighbors=lambda mid: neighbor_map.get(mid, []),
        named=lambda name: by_name.get(name, []),
        roles=lambda mid: (roles or {}).get(mid, {}),
    )


def test_connected_name_resolves_to_a_wired_mote():
    properties = {"R1": {}, "N1": {"name": "N1", "voltage": 12}}
    ctx = _named_context("R1", properties, {"R1": ["N1"]})
    assert evaluate_expression("N1.voltage", ctx).constant == 12.0


def test_unconnected_name_does_not_resolve():
    # N1 is named "N1" but isn't wired to R1, so this falls through to
    # the ordinary (and here, failing) property lookup -- the same error
    # an unconnected reference always got.
    properties = {"R1": {}, "N1": {"name": "N1", "voltage": 12}}
    ctx = _named_context("R1", properties, {"R1": []})
    with pytest.raises(FormulaError, match="no property"):
        evaluate_expression("N1.voltage", ctx)


def test_ambiguous_connected_name_is_rejected():
    properties = {
        "R1": {},
        "N1": {"name": "N1", "voltage": 1},
        "N1b": {"name": "N1", "voltage": 2},
    }
    ctx = _named_context("R1", properties, {"R1": ["N1", "N1b"]})
    with pytest.raises(FormulaError, match="matches 2 motes"):
        evaluate_expression("N1.voltage", ctx)


# ---- garden-scope names ----
#
# The scope after roles and connected names: a name unique in the whole
# garden resolves with no relationship drawn at all, which is what
# deletes the `related_to`-for-resolution edge (roadmap phase 2). See
# core/formula.py's _named_in_garden.

def test_garden_scope_name_resolves_without_any_relationship():
    properties = {"bd": {"name": "bd", "l": 100}, "rule_anchor": {}}
    ctx = _named_context("rule_anchor", properties, {}, garden=True)
    assert evaluate_expression("bd.l", ctx).constant == 100.0


def test_garden_scope_name_reaches_the_anchor_itself():
    # A rule about `bd` may write `bd.x` for `bd`'s own property, which is
    # what copying a line out of a worksheet means. The anchor is not its
    # own neighbor, so only the garden scope can answer this.
    properties = {"bd": {"name": "bd", "x": 80}}
    ctx = _named_context("bd", properties, {}, garden=True)
    assert evaluate_expression("bd.x", ctx).constant == 80.0


def test_garden_scope_name_collision_names_every_mote():
    properties = {
        "a1b2c3d4-x": {"name": "bd", "l": 1},
        "e5f6g7h8-y": {"name": "bd", "l": 2},
    }
    ctx = _named_context("anchor", properties, {}, garden=True)
    with pytest.raises(FormulaError) as excinfo:
        evaluate_expression("bd.l", ctx)
    message = str(excinfo.value)
    assert "matches 2 motes in the garden" in message
    assert "a1b2c3d4" in message and "e5f6g7h8" in message


def test_a_connected_name_beats_the_garden_scope():
    # Drawing one edge is how a garden holding two `bd`s says which one a
    # rule means -- the connected scope is searched first.
    properties = {
        "anchor": {},
        "bd1": {"name": "bd", "l": 1},
        "bd2": {"name": "bd", "l": 2},
    }
    ctx = _named_context("anchor", properties, {"anchor": ["bd2"]}, garden=True)
    assert evaluate_expression("bd.l", ctx).constant == 2.0


def test_a_role_beats_the_garden_scope():
    from core.roles import Role

    properties = {"anchor": {}, "part": {"name": "other", "l": 7}, "bd": {"name": "bd", "l": 1}}
    ctx = Context(
        mote_id="anchor",
        properties=lambda mid: properties.get(mid, {}),
        unknown_keys=lambda mid: set(),
        targets=lambda mid, t: ["part"] if t == "bd" else [],
        sources=lambda mid, t: [],
        named=lambda name: ["bd"] if name == "bd" else [],
        roles=lambda mid: {"bd": Role("bd")},
    )
    assert evaluate_expression("bd.l", ctx).constant == 7.0


def test_a_name_nobody_has_still_falls_through_to_a_property():
    properties = {"R1": {}}
    ctx = _named_context("R1", properties, {}, garden=True)
    with pytest.raises(FormulaError, match="no property"):
        evaluate_expression("N1.voltage", ctx)


def test_attribute_bases_lists_dotted_identifiers_in_order():
    assert attribute_bases("R1.voltage - R2.voltage == current * resistance") == [
        "R1", "R2",
    ]


def test_attribute_bases_deduplicates():
    assert attribute_bases("R1.x == R1.y + R1.x") == ["R1"]


def test_attribute_bases_ignores_bare_names():
    # "current" and "resistance" are ordinary properties, not dotted --
    # only the left side of a `.` counts.
    assert attribute_bases("current == resistance") == []


def test_attribute_bases_of_unparseable_text_is_empty():
    assert attribute_bases("not an equation") == []


def test_declared_role_takes_precedence_over_a_same_named_neighbor():
    from core.roles import Role

    properties = {"R1": {}, "N1": {"name": "N1", "voltage": 12}}
    ctx = _named_context(
        "R1", properties, {"R1": ["N1"]}, roles={"R1": {"N1": Role("N1")}}
    )
    # The role's own targets() (empty here) governs, not the neighbor
    # search -- so this fails the way an unfulfilled role always does,
    # rather than quietly falling back to the connected-name match.
    with pytest.raises(FormulaError, match="reaches no mote"):
        evaluate_expression("N1.voltage", ctx)

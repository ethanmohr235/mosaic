import pytest

from core.formula import (
    Context,
    FormulaError,
    LinearExpr,
    NonlinearError,
    evaluate_equation,
    evaluate_expression,
)

# A tiny fixed garden, expressed as plain dicts -- core/formula.py takes
# callables rather than a connection precisely so it can be tested without
# touching a database.
PROPERTIES = {
    "R1": {"resistance": 100, "label": "a string"},
    "N1": {},
    "N2": {},
    "N3": {},
}
UNKNOWNS = {"R1": {"current"}, "N1": {"voltage"}, "N2": {"voltage"}, "N3": {"voltage"}}
RELATIONSHIPS = [("R1", "N1", "a"), ("R1", "N2", "b"), ("R1", "N3", "b")]


def context(mote_id="R1", solved=None):
    return Context(
        mote_id=mote_id,
        properties=lambda mid: PROPERTIES.get(mid, {}),
        unknown_keys=lambda mid: UNKNOWNS.get(mid, set()),
        targets=lambda mid, t: [to for f, to, ty in RELATIONSHIPS if f == mid and ty == t],
        sources=lambda mid, t: [f for f, to, ty in RELATIONSHIPS if to == mid and ty == t],
        solved=solved or {},
    )


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


def test_raising_an_unknown_to_a_power_is_rejected():
    with pytest.raises(NonlinearError):
        LinearExpr({("m", "x"): 1.0}, 0.0) ** LinearExpr.number(2)


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


# ---- errors ----

def test_missing_property_names_the_mote_and_key():
    with pytest.raises(FormulaError, match="voltage"):
        evaluate_expression("voltage", context())


def test_non_numeric_property_is_rejected():
    with pytest.raises(FormulaError, match="not a number"):
        evaluate_expression("label", context())


def test_equation_without_comparison_is_rejected():
    with pytest.raises(FormulaError, match="not an equation"):
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
        evaluate_expression("sqrt(4)", context())


def test_navigation_requires_a_quoted_relationship_type():
    with pytest.raises(FormulaError, match="in quotes"):
        evaluate_expression("targets(resistance).voltage", context())


def test_keyword_arguments_are_rejected():
    with pytest.raises(FormulaError, match="keyword"):
        evaluate_expression("targets(type='a')", context())


def test_min_max_reject_unknowns():
    with pytest.raises(NonlinearError):
        evaluate_expression("max(current, 1)", context())

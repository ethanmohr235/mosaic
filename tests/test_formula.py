import pytest

from core.formula import (
    Context,
    FormulaError,
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
    "N1": {},
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

def _named_context(mote_id, properties, neighbor_map, roles=None):
    return Context(
        mote_id=mote_id,
        properties=lambda mid: properties.get(mid, {}),
        unknown_keys=lambda mid: set(),
        targets=lambda mid, t: [],
        sources=lambda mid, t: [],
        neighbors=lambda mid: neighbor_map.get(mid, []),
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

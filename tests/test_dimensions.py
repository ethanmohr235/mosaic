import ast
import inspect

import pytest

from core import formula
from core.dimensions import DimensionContext, check_equation


def context(mote_id="r1", units=None, targets=None, capability_units=None,
            neighbors=None, names=None):
    """A checker context over plain dicts -- the same testing shape
    core/formula.py's Context is built for."""
    units = units or {}
    targets = targets or {}
    neighbors = neighbors or {}
    names = names or {}
    return DimensionContext(
        mote_id=mote_id,
        units=lambda mid: units.get(mid, {}),
        targets=lambda mid, rel: targets.get((mid, rel), []),
        sources=lambda mid, rel: [],
        capability_units=capability_units or (lambda *a: None),
        neighbors=lambda mid: neighbors.get(mid, []),
        mote_name=lambda mid: names.get(mid),
    )


# ---- the opt-in guarantee ----

def test_an_unannotated_equation_reports_nothing():
    # The property that makes units opt-in: a garden that never declares
    # any produces zero warnings, forever.
    assert check_equation("voltage == current * resistance", context()) == []


def test_a_partly_annotated_equation_reports_nothing():
    # Only `voltage` is declared; the right-hand side is unknowable, so
    # there is nothing to disagree with.
    units = {"r1": {"voltage": "V"}}
    assert check_equation("voltage == current * resistance", context(units=units)) == []


def test_an_undeclared_factor_does_not_poison_a_product():
    # `current` is A, `resistance` undeclared -- the product must come out
    # unspecified rather than pretending to be A and clashing with V.
    units = {"r1": {"voltage": "V", "current": "A"}}
    assert check_equation("voltage == current * resistance", context(units=units)) == []


# ---- consistency ----

def test_ohms_law_checks_out():
    units = {"r1": {"voltage": "V", "current": "A", "resistance": "ohm"}}
    assert check_equation("voltage == current * resistance", context(units=units)) == []


def test_a_mismatched_equation_is_flagged():
    units = {"r1": {"voltage": "V", "current": "A", "resistance": "ohm"}}
    problems = check_equation("voltage == current * current", context(units=units))
    assert len(problems) == 1
    assert "left side is V" in problems[0]


def test_adding_different_dimensions_is_flagged():
    units = {"r1": {"voltage": "V", "current": "A", "resistance": "ohm"}}
    problems = check_equation("voltage == current + resistance", context(units=units))
    assert any("cannot be added to" in p for p in problems)


def test_subtracting_different_dimensions_is_flagged():
    units = {"r1": {"length": "m", "duration": "s"}}
    problems = check_equation("length == length - duration", context(units=units))
    assert any("subtracted from" in p for p in problems)


def test_compatible_units_of_the_same_dimension_agree():
    # kohm and ohm are different units but the same dimension -- comparing
    # dimensions, not unit strings, is the whole point.
    units = {"r1": {"voltage": "mV", "current": "A", "resistance": "kohm"}}
    assert check_equation("voltage == current * resistance", context(units=units)) == []


def test_division_subtracts_dimensions():
    units = {"r1": {"voltage": "V", "current": "A", "resistance": "ohm"}}
    assert check_equation("current == voltage / resistance", context(units=units)) == []


def test_a_literal_is_dimensionless_and_scales_cleanly():
    units = {"r1": {"voltage": "V", "current": "A", "resistance": "ohm"}}
    assert check_equation("voltage == 2 * current * resistance", context(units=units)) == []


def test_a_literal_compared_against_a_dimensioned_quantity_is_allowed():
    # `resistance == 100` is the ordinary way to state a known value; the
    # literal is taken to be in that quantity's own base unit.
    units = {"r1": {"resistance": "ohm"}}
    assert check_equation("resistance == 100", context(units=units)) == []


def test_a_ratio_is_dimensionless_and_clashes_with_a_real_dimension():
    units = {"r1": {"a": "m", "b": "m", "voltage": "V"}}
    problems = check_equation("voltage == a / b", context(units=units))
    assert any("dimensionless" in p for p in problems)


def test_powers_scale_dimensions():
    units = {"r1": {"area": "m^2", "side": "m"}}
    assert check_equation("area == side ** 2", context(units=units)) == []


def test_a_wrong_power_is_flagged():
    units = {"r1": {"area": "m^2", "side": "m"}}
    problems = check_equation("area == side ** 3", context(units=units))
    assert len(problems) == 1


def test_a_negative_power_scales_dimensions():
    units = {"r1": {"frequency": "Hz", "period": "s"}}
    assert check_equation("frequency == period ** -1", context(units=units)) == []


# ---- sin/cos/tan/sqrt ----

def test_sqrt_halves_dimensions():
    units = {"r1": {"area": "m^2", "side": "m"}}
    assert check_equation("side == sqrt(area)", context(units=units)) == []


def test_a_wrong_sqrt_is_flagged():
    units = {"r1": {"volume": "m^3", "side": "m"}}
    problems = check_equation("side == sqrt(volume)", context(units=units))
    assert len(problems) == 1


def test_trig_functions_are_always_dimensionless():
    # Regardless of what the argument declares -- unlike abs/min/max,
    # sin/cos/tan don't relay their input's dimension to the output.
    units = {"r1": {"angle": "deg", "ratio": "ohm"}}
    problems = check_equation("ratio == sin(angle)", context(units=units))
    assert any("left side is" in p for p in problems)


def test_trig_functions_still_surface_problems_inside_their_argument():
    units = {"r1": {"bad": "not-a-real-unit"}}
    problems = check_equation("y == sin(bad)", context(units=units))
    assert any("not recognized" in p for p in problems)


# ---- navigation and aggregation ----

def test_dimensions_are_read_from_the_mote_actually_navigated_to():
    units = {"r1": {"voltage": "V"}, "n1": {"potential": "V"}}
    targets = {("r1", "a"): ["n1"]}
    assert check_equation(
        "voltage == targets('a').potential", context(units=units, targets=targets)
    ) == []


def test_a_mismatch_across_a_relationship_is_flagged():
    units = {"r1": {"voltage": "V"}, "n1": {"potential": "A"}}
    targets = {("r1", "a"): ["n1"]}
    problems = check_equation(
        "voltage == targets('a').potential", context(units=units, targets=targets)
    )
    assert len(problems) == 1


def test_summing_motes_that_measure_different_things_is_flagged():
    units = {"r1": {"total": "A"}, "n1": {"current": "A"}, "n2": {"current": "V"}}
    targets = {("r1", "in"): ["n1", "n2"]}
    problems = check_equation(
        "total == sum(targets('in').current)", context(units=units, targets=targets)
    )
    assert any("combines" in p for p in problems)


def test_count_is_dimensionless():
    units = {"r1": {"n": "m"}}
    problems = check_equation("n == count(targets('in'))", context(units=units))
    assert any("dimensionless" in p for p in problems)


def test_min_max_and_abs_keep_their_operands_dimension():
    units = {"r1": {"voltage": "V", "floor": "V", "current": "A"}}
    assert check_equation("voltage == max(floor, 0)", context(units=units)) == []
    problems = check_equation("voltage == abs(current)", context(units=units))
    assert len(problems) == 1


def test_prev_has_the_same_dimension_as_the_property_itself():
    units = {"r1": {"balance": "J", "rate": "Hz"}}
    assert check_equation("balance == prev(balance) * 1.05", context(units=units)) == []
    problems = check_equation("balance == prev(rate)", context(units=units))
    assert len(problems) == 1


def test_the_tick_never_conflicts():
    # `t` is a step index, but rules routinely read it as elapsed time
    # (docs/simulation.md's own `height == 10 - 0.5 * t`). Ambiguous, so
    # it constrains nothing rather than inventing a false positive.
    units = {"r1": {"height": "m", "speed": "m/s"}}
    assert check_equation("height == 10 - speed * t", context(units=units)) == []


def test_a_role_qualified_t_keeps_its_real_dimension():
    # Unlike a bare `t`, `targets('a').t` names another mote's own
    # property -- it must not go unspecified the way the tick does.
    units = {"r1": {"rate": "m/s"}, "n1": {"t": "s"}}
    targets = {("r1", "a"): ["n1"]}
    problems = check_equation(
        "rate == targets('a').t", context(units=units, targets=targets)
    )
    assert len(problems) == 1


def test_math_constants_are_treated_as_literals():
    # pi/e are bare numbers, like a literal `3.14159`, so they stand for
    # whatever quantity they're compared against rather than reporting a
    # dimension mismatch.
    units = {"r1": {"circumference": "m", "radius": "m"}}
    assert check_equation("circumference == 2 * pi * radius", context(units=units)) == []


def test_a_role_qualified_e_keeps_its_real_dimension():
    units = {"r1": {"voltage": "V"}, "n1": {"e": "s"}}
    targets = {("r1", "a"): ["n1"]}
    problems = check_equation(
        "voltage == targets('a').e", context(units=units, targets=targets)
    )
    assert len(problems) == 1


# ---- property models ----

def test_a_property_call_takes_its_dimension_from_the_capability():
    units = {"r1": {"p": "Pa"}}
    problems = check_equation(
        "p == property('gas', 'pressure', density, temperature)",
        context(units=units, capability_units=lambda *a: "Pa"),
    )
    assert problems == []


def test_a_property_call_with_the_wrong_dimension_is_flagged():
    units = {"r1": {"p": "Pa"}}
    problems = check_equation(
        "p == property('gas', 'density', density, temperature)",
        context(units=units, capability_units=lambda *a: "kg/m^3"),
    )
    assert len(problems) == 1


def test_a_capability_with_no_declared_unit_is_simply_unspecified():
    units = {"r1": {"p": "Pa"}}
    assert check_equation(
        "p == property('gas', 'pressure', density, temperature)", context(units=units)
    ) == []


# ---- bad input ----

def test_an_unrecognized_unit_name_is_reported_once():
    units = {"r1": {"voltage": "volts", "current": "volts"}}
    problems = check_equation("voltage == current", context(units=units))
    assert len(problems) == 1
    assert "not recognized" in problems[0]


def test_an_unparseable_equation_reports_nothing():
    # core/formula.py reports syntax errors, with a better message, when
    # the run evaluates the rule. This checker's job is units only.
    assert check_equation("voltage = current", context()) == []
    assert check_equation("!!!", context()) == []


def test_a_rejected_construct_reports_nothing():
    assert check_equation("voltage == some_list[0]", context()) == []


# ---- drift ----

def test_the_checker_handles_exactly_the_formula_call_whitelist():
    """core/formula.py's call whitelist is the security boundary, and this
    module walks the same grammar. If a builtin is added there without
    being handled here, the checker would silently stop understanding
    equations that use it -- so fail loudly instead."""
    tree = ast.parse(inspect.getsource(formula._call))
    referenced = {
        node.id for node in ast.walk(tree)
        if isinstance(node, ast.Name)
    }
    dispatch_constants = {
        "NAVIGATION_CALLS", "AGGREGATE_CALLS", "NUMERIC_CALLS", "TRIG_CALLS",
        "LOG_CALLS", "SQRT_CALL", "PREVIOUS_CALL", "PROPERTY_CALL",
    }
    # Every group formula._call() dispatches on must be one this module
    # imported and handles.
    assert dispatch_constants <= referenced, (
        "core/formula.py: _call() no longer dispatches through the shared "
        "whitelist constants that core/dimensions.py relies on"
    )
    assert formula.ALLOWED_CALLS == (
        formula.NAVIGATION_CALLS
        | formula.AGGREGATE_CALLS
        | formula.NUMERIC_CALLS
        | formula.TRIG_CALLS
        | formula.LOG_CALLS
        | {formula.SQRT_CALL, formula.PREVIOUS_CALL, formula.PROPERTY_CALL}
    )


@pytest.mark.parametrize("name", sorted(formula.ALLOWED_CALLS))
def test_every_whitelisted_builtin_is_understood_by_the_checker(name):
    """Each allowed call name must walk without hitting the checker's
    'unknown function' branch."""
    calls = {
        "targets": "targets('a').x",
        "sources": "sources('a').x",
        "sum": "sum(targets('a').x)",
        "count": "count(targets('a'))",
        "abs": "abs(x)",
        "min": "min(x, 1)",
        "max": "max(x, 1)",
        "sin": "sin(x)",
        "cos": "cos(x)",
        "tan": "tan(x)",
        "arcsin": "arcsin(x)",
        "arccos": "arccos(x)",
        "arctan": "arctan(x)",
        "sqrt": "sqrt(x)",
        "log": "log(x)",
        "ln": "ln(x)",
        "log10": "log10(x)",
        "exp": "exp(x)",
        "prev": "prev(x)",
        "property": "property('a', 'op', 1)",
    }
    assert name in calls, f"{name}() is whitelisted but has no checker coverage here"
    # No assertion on the result -- the point is that it doesn't raise
    # 'unknown function', which check_equation() would surface.
    check_equation(f"y == {calls[name]}", context(targets={("r1", "a"): ["n1"]}))


# ---- connected-name references ----

def test_connected_name_dimension_is_checked_like_a_role():
    units = {"r1": {"x": "V"}, "n1": {"voltage": "A"}}
    problems = check_equation(
        "x == n1.voltage",
        context(units=units, neighbors={"r1": ["n1"]}, names={"n1": "n1"}),
    )
    assert len(problems) == 1
    assert "different dimensions" in problems[0]


def test_ambiguous_connected_name_reports_nothing_here():
    # An ambiguous name is a resolution problem, not a units problem --
    # core/formula.py raises the real error at solve time; the checker
    # only swallows it here, the same way it swallows any other
    # FormulaError the real evaluator would also reject (see
    # check_equation's docstring).
    problems = check_equation(
        "y == n1.voltage",
        context(neighbors={"r1": ["n1", "n1b"]}, names={"n1": "n1", "n1b": "n1"}),
    )
    assert problems == []

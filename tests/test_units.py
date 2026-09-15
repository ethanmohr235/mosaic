import math

import pytest

from core.units import (
    DIMENSIONLESS,
    Dimension,
    IncompatibleUnits,
    UnitError,
    UnknownUnit,
    convert,
    describe,
    describe_expression,
    dimension_of,
    format_unit,
    from_base,
    is_unit,
    list_units,
    parse_units_property,
    register_unit,
    to_base,
)


# ---- dimensions ----

def test_multiplying_quantities_adds_dimensions():
    # Ohm's law: V = A * ohm, the check that makes equation verification
    # possible at all.
    assert dimension_of("A") + dimension_of("ohm") == dimension_of("V")


def test_dividing_quantities_subtracts_dimensions():
    assert dimension_of("V") - dimension_of("A") == dimension_of("ohm")


def test_raising_to_a_power_scales_dimensions():
    area = dimension_of("m") * 2
    assert area == Dimension(length=2)


def test_a_quantity_divided_by_its_own_kind_is_dimensionless():
    assert (dimension_of("Pa") - dimension_of("bar")).is_dimensionless


def test_exponents_are_rounded_so_float_error_cannot_split_a_dimension():
    accumulated = DIMENSIONLESS
    for _ in range(30):
        accumulated = accumulated + dimension_of("m") * 0.1
    assert accumulated == Dimension(length=3)


def test_dimension_symbol_is_readable():
    assert dimension_of("V").symbol == "m²·kg·s⁻³·A⁻¹"
    assert DIMENSIONLESS.symbol == "dimensionless"


def test_describe_prefers_a_familiar_unit_name():
    assert describe(dimension_of("V")) == "V"
    assert describe(dimension_of("W")) == "W"
    # No named unit for this one, so it falls back to the base product.
    assert describe(Dimension(length=1, time=-1)) == "m·s⁻¹"


def test_describe_expression_gives_caret_notation():
    # A familiar unit's name doesn't change...
    assert describe_expression(dimension_of("V")) == "V"
    # ...but a base-product fallback is caret notation, not superscripts,
    # so an automatically inferred unit is typed the same way a person
    # typing a unit into a `units` property by hand would type it.
    assert describe_expression(Dimension(length=1, time=-1)) == "m*s^-1"
    assert is_unit(describe_expression(Dimension(length=1, time=-1)))


def test_format_unit_renders_caret_notation_as_superscript():
    assert format_unit("cm^-3") == "cm⁻³"
    assert format_unit("m*s^-1") == "m·s⁻¹"
    assert format_unit("kohm") == "kohm"
    assert format_unit("J/(kg*K)") == "J/(kg·K)"


# ---- conversion ----

def test_convert_between_prefixed_units():
    assert convert(1, "kohm", "ohm") == pytest.approx(1000.0)
    assert convert(2500, "mV", "V") == pytest.approx(2.5)


def test_kilogram_arrives_through_the_gram_prefix():
    # The SI base unit of mass is the kilogram, but the prefixable unit is
    # the gram -- kg has to come out right anyway.
    assert convert(1, "kg", "g") == pytest.approx(1000.0)
    assert to_base(1, "kg") == pytest.approx(1.0)


def test_convert_between_non_si_units():
    assert convert(1, "hr", "min") == pytest.approx(60.0)
    assert convert(12, "in", "ft") == pytest.approx(1.0)
    assert convert(1, "bar", "Pa") == pytest.approx(1e5)


def test_nuclear_and_radiological_units():
    assert convert(1, "MeV", "eV") == pytest.approx(1e6)
    assert to_base(1, "eV") == pytest.approx(1.602176634e-19)
    assert convert(1, "Ci", "Bq") == pytest.approx(3.7e10)
    assert convert(1, "GBq", "Bq") == pytest.approx(1e9)
    assert convert(1, "rem", "Sv") == pytest.approx(0.01)
    assert convert(1000, "mSv", "Sv") == pytest.approx(1.0)
    assert dimension_of("Gy") == dimension_of("Sv")
    assert to_base(1, "barn") == pytest.approx(1e-28)
    assert to_base(1, "u") == pytest.approx(1.66053906660e-27)


def test_degree_converts_to_dimensionless_radians():
    assert to_base(180, "deg") == pytest.approx(math.pi)
    assert to_base(90, "deg") == pytest.approx(math.pi / 2)
    assert dimension_of("deg").is_dimensionless


def test_affine_temperature_conversion():
    # The case that justifies Unit.offset existing at all: treating degC
    # as a plain multiple of K would put freezing at 0 K.
    assert convert(0, "degC", "K") == pytest.approx(273.15)
    assert convert(0, "degC", "degF") == pytest.approx(32.0)
    assert convert(100, "degC", "degF") == pytest.approx(212.0)
    assert convert(-40, "degF", "degC") == pytest.approx(-40.0)


def test_to_base_and_from_base_round_trip():
    for name, value in [("kohm", 4.7), ("degF", 98.6), ("psi", 14.7), ("mi", 3.0)]:
        assert from_base(to_base(value, name), name) == pytest.approx(value)


def test_converting_between_different_dimensions_is_refused():
    with pytest.raises(IncompatibleUnits, match="different dimensions"):
        convert(1, "ohm", "s")


def test_unknown_unit_is_reported_clearly():
    with pytest.raises(UnknownUnit, match="furlong"):
        dimension_of("furlong")


# ---- compound unit expressions ----

def test_compound_units_parse():
    assert dimension_of("m/s") == Dimension(length=1, time=-1)
    assert dimension_of("kg/m^3") == Dimension(mass=1, length=-3)
    assert dimension_of("N*m") == dimension_of("J")


def test_compound_units_accept_the_forms_capabilities_already_declare():
    # These exact strings are declared by the property providers already
    # in this codebase (core/ideal_gas_provider.py, coolprop_provider.py),
    # so property() verification depends on them parsing.
    assert dimension_of("J/(kg*K)") == Dimension(length=2, time=-2, temperature=-1)
    assert dimension_of("J/(kg K)") == dimension_of("J/(kg*K)")  # juxtaposition
    assert dimension_of("J/kg") == Dimension(length=2, time=-2)


def test_superscript_exponents_parse():
    assert dimension_of("kg/m³") == dimension_of("kg/m^3")
    assert dimension_of("s⁻¹") == dimension_of("Hz")


def test_equivalent_compounds_share_a_dimension():
    assert dimension_of("J/kg") == dimension_of("m^2/s^2")


def test_number_density_is_a_compound_not_a_registered_unit():
    # 1/cm^3 (neutron/particle density) is never registered up front --
    # the algebraic grammar is what makes it work at all.
    assert dimension_of("1/cm^3") == Dimension(length=-3)
    assert dimension_of("cm^-3") == dimension_of("1/cm^3")
    assert convert(1, "1/cm^3", "1/m^3") == pytest.approx(1e6)


def test_compound_scales_compose():
    assert convert(1, "kJ/kg", "J/kg") == pytest.approx(1000.0)
    assert convert(36, "km/hr", "m/s") == pytest.approx(10.0)


def test_offset_units_cannot_be_combined_into_a_compound():
    # Dropping the offset would make degC/s silently mean K/s -- numbers
    # that look plausible and are wrong.
    with pytest.raises(UnitError, match="offset unit"):
        dimension_of("degC/s")


def test_unknown_unit_inside_a_compound_names_the_whole_expression():
    with pytest.raises(UnknownUnit, match="furlong"):
        dimension_of("furlong/s")


@pytest.mark.parametrize("text", ["m/", "((m)", "m^", "m @ s", ""])
def test_malformed_compounds_are_rejected(text):
    with pytest.raises(UnitError):
        dimension_of(text)


# ---- registry ----

def test_registering_a_duplicate_name_is_refused():
    # Guards the prefix-generation loop: a generated name landing on one
    # that already means something else should fail at import, loudly.
    with pytest.raises(UnitError, match="already registered"):
        register_unit("ohm", Dimension())


def test_minute_was_not_shadowed_by_a_generated_prefix():
    # 'min' would be milli-inch if inches were prefixed. They deliberately
    # aren't, so 'min' still means a minute.
    assert to_base(1, "min") == pytest.approx(60.0)


def test_list_units_and_is_unit_agree():
    names = list_units()
    assert "kohm" in names and "degC" in names
    assert all(is_unit(n) for n in names)
    assert not is_unit("furlong")


# ---- the `units` property ----

def test_parse_units_property_reads_a_plain_map():
    assert parse_units_property({"resistance": "kohm", "power": "W"}) == {
        "resistance": "kohm", "power": "W",
    }


def test_parse_units_property_ignores_malformed_values():
    # A half-typed `units` property should leave a mote un-annotated
    # rather than breaking every simulation that touches it.
    assert parse_units_property({"a": "", "b": "  ", "c": 5, "d": None, "e": " V "}) == {"e": "V"}


def test_parse_units_property_ignores_a_non_dict():
    assert parse_units_property("ohm") == {}
    assert parse_units_property(None) == {}

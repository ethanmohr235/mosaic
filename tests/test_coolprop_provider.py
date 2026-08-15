import pytest

from core.coolprop_provider import CoolPropProvider
from core.providers import (
    CapabilityNotSupported,
    InvalidModelConfiguration,
    OutsideDomain,
    ProviderExecutionError,
)

# Reference values from published steam tables / CoolProp itself at
# unambiguous single-phase states (superheated steam at 8 MPa/500 C, and
# subcooled liquid water at 1 atm/20 C), so evaluate() can be checked
# against real physics rather than just round-tripping through itself.
_STEAM_PRESSURE = 8e6       # Pa
_STEAM_TEMPERATURE = 773.15  # K (500 C)
_STEAM_ENTHALPY = pytest.approx(3399491.65, rel=1e-3)   # ~3399.5 kJ/kg
_STEAM_ENTROPY = pytest.approx(6726.59, rel=1e-3)       # ~6.7266 kJ/(kg*K)

_LIQUID_PRESSURE = 101325.0  # Pa
_LIQUID_TEMPERATURE = 293.15  # K (20 C)
_LIQUID_DENSITY = pytest.approx(998.2, rel=1e-3)        # kg/m^3


def test_requires_fluid():
    with pytest.raises(InvalidModelConfiguration, match="fluid"):
        CoolPropProvider({})


def test_rejects_non_string_fluid():
    with pytest.raises(InvalidModelConfiguration, match="fluid"):
        CoolPropProvider({"fluid": 123})


def test_rejects_empty_fluid():
    with pytest.raises(InvalidModelConfiguration, match="fluid"):
        CoolPropProvider({"fluid": "   "})


def test_rejects_non_string_backend():
    with pytest.raises(InvalidModelConfiguration, match="backend"):
        CoolPropProvider({"fluid": "Water", "backend": 7})


def test_backend_defaults_to_heos():
    provider = CoolPropProvider({"fluid": "Water"})
    assert provider.fluid_spec == "HEOS::Water"


def test_capabilities_lists_all_eleven_operations():
    provider = CoolPropProvider({"fluid": "Water"})
    operations = {c.operation for c in provider.capabilities()}
    assert operations == {
        "enthalpy_pt", "entropy_pt", "density_pt", "temperature_ph", "enthalpy_ps",
        "quality_ph", "quality_ps", "enthalpy_pq", "entropy_pq", "temperature_pq", "density_pq",
    }


def test_rejects_unknown_operation():
    provider = CoolPropProvider({"fluid": "Water"})
    with pytest.raises(CapabilityNotSupported):
        provider.evaluate("pressure_ts", {})


def test_enthalpy_pt_matches_steam_tables():
    provider = CoolPropProvider({"fluid": "Water"})
    h = provider.evaluate(
        "enthalpy_pt", {"pressure": _STEAM_PRESSURE, "temperature": _STEAM_TEMPERATURE}
    )
    assert h == _STEAM_ENTHALPY


def test_entropy_pt_matches_steam_tables():
    provider = CoolPropProvider({"fluid": "Water"})
    s = provider.evaluate(
        "entropy_pt", {"pressure": _STEAM_PRESSURE, "temperature": _STEAM_TEMPERATURE}
    )
    assert s == _STEAM_ENTROPY


def test_density_pt_matches_known_liquid_water_density():
    provider = CoolPropProvider({"fluid": "Water"})
    d = provider.evaluate(
        "density_pt", {"pressure": _LIQUID_PRESSURE, "temperature": _LIQUID_TEMPERATURE}
    )
    assert d == _LIQUID_DENSITY


def test_temperature_ph_round_trips_with_enthalpy_pt():
    provider = CoolPropProvider({"fluid": "Water"})
    h = provider.evaluate(
        "enthalpy_pt", {"pressure": _STEAM_PRESSURE, "temperature": _STEAM_TEMPERATURE}
    )
    t = provider.evaluate("temperature_ph", {"pressure": _STEAM_PRESSURE, "enthalpy": h})
    assert t == pytest.approx(_STEAM_TEMPERATURE, rel=1e-6)


def test_enthalpy_ps_isentropic_expansion_drops_enthalpy():
    # Expanding steam isentropically to a much lower pressure (a turbine's
    # ideal expansion) must reduce its enthalpy -- this is the exact lookup
    # scripts/example_steam_turbine.py uses for h_out.
    provider = CoolPropProvider({"fluid": "Water"})
    s = provider.evaluate(
        "entropy_pt", {"pressure": _STEAM_PRESSURE, "temperature": _STEAM_TEMPERATURE}
    )
    h_out = provider.evaluate("enthalpy_ps", {"pressure": 10e3, "entropy": s})
    assert h_out == pytest.approx(2130228.13, rel=1e-3)


def test_enthalpy_pq_and_quality_ph_round_trip():
    # A turbine exit at 10 kPa lands in the two-phase dome (see
    # test_enthalpy_ps_isentropic_expansion_drops_enthalpy), which is
    # exactly where a quality lookup is needed to check exit wetness.
    provider = CoolPropProvider({"fluid": "Water"})
    h = provider.evaluate("enthalpy_pq", {"pressure": 10e3, "quality": 0.8})
    q = provider.evaluate("quality_ph", {"pressure": 10e3, "enthalpy": h})
    assert q == pytest.approx(0.8, rel=1e-6)


def test_entropy_pq_and_quality_ps_round_trip():
    provider = CoolPropProvider({"fluid": "Water"})
    s = provider.evaluate("entropy_pq", {"pressure": 10e3, "quality": 0.8})
    q = provider.evaluate("quality_ps", {"pressure": 10e3, "entropy": s})
    assert q == pytest.approx(0.8, rel=1e-6)


def test_temperature_pq_saturation_temperature_is_independent_of_quality():
    # Saturation temperature depends only on pressure along the dome, not
    # on quality -- liquid and vapor at the same pressure boil at the
    # same temperature.
    provider = CoolPropProvider({"fluid": "Water"})
    t_liquid = provider.evaluate("temperature_pq", {"pressure": 10e3, "quality": 0.0})
    t_vapor = provider.evaluate("temperature_pq", {"pressure": 10e3, "quality": 1.0})
    assert t_liquid == pytest.approx(t_vapor, rel=1e-9)


def test_density_pq_liquid_denser_than_vapor():
    provider = CoolPropProvider({"fluid": "Water"})
    d_liquid = provider.evaluate("density_pq", {"pressure": 10e3, "quality": 0.0})
    d_vapor = provider.evaluate("density_pq", {"pressure": 10e3, "quality": 1.0})
    assert d_liquid > d_vapor * 100  # liquid water vs. low-pressure steam


def test_quality_ph_rejects_a_state_outside_the_two_phase_dome():
    # Superheated steam has no quality -- CoolProp silently returns -1
    # rather than raising, so evaluate() has to catch that itself and
    # surface it as OutsideDomain instead of a bogus quality value.
    provider = CoolPropProvider({"fluid": "Water"})
    with pytest.raises(OutsideDomain, match="quality_ph"):
        provider.evaluate(
            "quality_ph", {"pressure": _STEAM_PRESSURE, "enthalpy": 3399491.65}
        )


def test_invalid_state_wraps_coolprop_error():
    provider = CoolPropProvider({"fluid": "Water"})
    with pytest.raises(ProviderExecutionError, match="enthalpy_pt"):
        provider.evaluate("enthalpy_pt", {"pressure": -1.0, "temperature": 300.0})


def test_unknown_fluid_wraps_coolprop_error_on_evaluate():
    provider = CoolPropProvider({"fluid": "NotAFluid"})
    with pytest.raises(ProviderExecutionError):
        provider.evaluate("enthalpy_pt", {"pressure": 101325.0, "temperature": 300.0})

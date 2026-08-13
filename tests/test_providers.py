import pytest

from core.capability import Capability
from core.ideal_gas_provider import IdealGasProvider
from core.providers import (
    CapabilityNotSupported,
    InvalidModelConfiguration,
    ProviderNotFound,
    PropertyProvider,
    list_providers,
    register_provider,
    resolve_provider,
)


class _EchoProvider(PropertyProvider):
    """A trivial provider for exercising the registry and the shared
    capability() lookup without pulling in real physics."""

    def capabilities(self):
        return [Capability(operation="echo", inputs=("x",), output="x", units=None)]

    def evaluate(self, operation, state):
        return state["x"]


# ---- registry ----

def test_register_and_resolve_provider():
    register_provider("_echo_test", _EchoProvider)
    assert resolve_provider("_echo_test") is _EchoProvider


def test_list_providers_includes_registered_names():
    register_provider("_echo_test_2", _EchoProvider)
    assert "_echo_test_2" in list_providers()


def test_resolve_unregistered_provider_is_reported_clearly():
    with pytest.raises(ProviderNotFound, match="nope"):
        resolve_provider("nope")


# ---- PropertyProvider.capability() ----

def test_capability_finds_matching_operation():
    provider = _EchoProvider({})
    cap = provider.capability("echo")
    assert cap.operation == "echo"
    assert cap.inputs == ("x",)


def test_capability_lists_available_operations_on_failure():
    provider = _EchoProvider({})
    with pytest.raises(CapabilityNotSupported, match="echo"):
        provider.capability("nonexistent")


def test_echo_provider_evaluates():
    provider = _EchoProvider({})
    assert provider.evaluate("echo", {"x": 5.0}) == 5.0


# ---- IdealGasProvider ----

def test_ideal_gas_pressure():
    provider = IdealGasProvider({"gas_constant": 287.05})
    # P = rho * R * T
    pressure = provider.evaluate("pressure", {"density": 1.2, "temperature": 293.15})
    assert pressure == pytest.approx(1.2 * 287.05 * 293.15)


def test_ideal_gas_density():
    provider = IdealGasProvider({"gas_constant": 287.05})
    density = provider.evaluate("density", {"pressure": 101325.0, "temperature": 288.15})
    assert density == pytest.approx(101325.0 / (287.05 * 288.15))


def test_ideal_gas_temperature():
    provider = IdealGasProvider({"gas_constant": 287.05})
    temperature = provider.evaluate("temperature", {"pressure": 101325.0, "density": 1.225})
    assert temperature == pytest.approx(101325.0 / (287.05 * 1.225))


def test_ideal_gas_round_trips_with_pressure():
    provider = IdealGasProvider({"gas_constant": 287.05})
    pressure = provider.evaluate("pressure", {"density": 1.0, "temperature": 300.0})
    density = provider.evaluate("density", {"pressure": pressure, "temperature": 300.0})
    assert density == pytest.approx(1.0)


def test_ideal_gas_requires_gas_constant():
    with pytest.raises(InvalidModelConfiguration, match="gas_constant"):
        IdealGasProvider({})


def test_ideal_gas_rejects_non_numeric_gas_constant():
    with pytest.raises(InvalidModelConfiguration, match="gas_constant"):
        IdealGasProvider({"gas_constant": "a lot"})


def test_ideal_gas_capabilities_lists_all_three_operations():
    provider = IdealGasProvider({"gas_constant": 287.05})
    operations = {c.operation for c in provider.capabilities()}
    assert operations == {"pressure", "density", "temperature"}


def test_ideal_gas_rejects_unknown_operation():
    provider = IdealGasProvider({"gas_constant": 287.05})
    with pytest.raises(CapabilityNotSupported):
        provider.evaluate("enthalpy", {})

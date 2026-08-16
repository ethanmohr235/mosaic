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


# ---- describe_providers: the reference, derived rather than copied ----

def test_describe_providers_reports_every_registered_provider():
    from core import model_resolver  # noqa: F401  (registers the built-ins)
    from core.providers import describe_providers

    described = describe_providers()
    # A superset, not an equality: the registry is process-global and
    # other tests in this file register fakes into it. That is
    # describe_providers() being honest -- it reports what is registered.
    assert {"ideal_gas", "coolprop"} <= set(described)

    ideal_gas = described["ideal_gas"]
    assert ideal_gas["description"]
    assert ideal_gas["configuration"] == {
        "gas_constant": "number, J/(kg*K), e.g. 287.05 for dry air"
    }
    assert {c["operation"] for c in ideal_gas["capabilities"]} == {
        "pressure", "density", "temperature"
    }
    pressure = next(c for c in ideal_gas["capabilities"] if c["operation"] == "pressure")
    assert pressure == {
        "operation": "pressure",
        "inputs": ["density", "temperature"],
        "output": "pressure",
        "units": "Pa",
    }


def test_reserved_provider_table_is_the_derived_one():
    """core/reserved.py used to hand-copy this, so it could drift from
    the providers it described. Now there is one source.

    The constant is a snapshot taken when core.reserved is imported,
    which is complete because every built-in registers on import of
    core.model_resolver -- so this compares the built-ins rather than
    whatever fakes other tests have since registered."""
    from core.providers import describe_providers
    from core.reserved import PROPERTY_MODEL_PROVIDERS

    live = describe_providers()
    for name in ("ideal_gas", "coolprop"):
        assert PROPERTY_MODEL_PROVIDERS[name] == live[name]


def test_a_providers_declared_capabilities_match_what_an_instance_reports():
    """The class declaration is what describe_providers() reads and the
    instance method is what the solver calls -- if those two disagreed,
    the reference would describe operations that don't run."""
    from core.coolprop_provider import CoolPropProvider
    from core.ideal_gas_provider import IdealGasProvider

    ideal_gas = IdealGasProvider({"gas_constant": 287.05})
    assert ideal_gas.capabilities() == list(IdealGasProvider.CAPABILITIES)

    coolprop = CoolPropProvider({"fluid": "Water"})
    assert coolprop.capabilities() == list(CoolPropProvider.CAPABILITIES)


def test_a_provider_may_still_override_capabilities():
    """Nothing assumes capabilities are static -- a provider whose
    operations depend on its configuration can override the method, and
    the base class only supplies the default."""
    from core.capability import Capability
    from core.providers import PropertyProvider

    class Dynamic(PropertyProvider):
        CAPABILITIES = (Capability(operation="declared", inputs=("x",), output="y"),)

        def capabilities(self):
            return [Capability(operation=self.configuration["op"], inputs=("x",), output="y")]

        def evaluate(self, operation, state):
            return 1.0

    provider = Dynamic({"op": "computed"})
    assert [c.operation for c in provider.capabilities()] == ["computed"]
    assert provider.capability("computed").output == "y"

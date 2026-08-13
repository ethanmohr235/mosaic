# core/ideal_gas_provider.py
"""The first concrete property provider: the ideal gas law, P = rho * R *
T (the intensive form of PV = nRT, using a specific gas constant R in
J/(kg*K) rather than the universal gas constant). Needs no external
dependency, which is why it -- not CoolProp -- is what proves the
property-model architecture end-to-end first. See
docs/physical_property_proposal.

R is generic configuration, not hardcoded to any one gas: dry air is
287.05 J/(kg*K), but the same provider works for any gas given its own
R via `configuration = {"gas_constant": ...}`.
"""
from core.capability import Capability
from core.providers import InvalidModelConfiguration, PropertyProvider

_CAPABILITIES = [
    Capability(
        operation="pressure",
        inputs=("density", "temperature"),
        output="pressure",
        units="Pa",
    ),
    Capability(
        operation="density",
        inputs=("pressure", "temperature"),
        output="density",
        units="kg/m^3",
    ),
    Capability(
        operation="temperature",
        inputs=("pressure", "density"),
        output="temperature",
        units="K",
    ),
]


class IdealGasProvider(PropertyProvider):
    def __init__(self, configuration: dict):
        super().__init__(configuration)
        gas_constant = configuration.get("gas_constant")
        if not isinstance(gas_constant, (int, float)) or isinstance(gas_constant, bool):
            raise InvalidModelConfiguration(
                "ideal_gas provider requires a numeric 'gas_constant' "
                "(the specific gas constant, J/(kg*K)) in its configuration"
            )
        self.gas_constant = float(gas_constant)

    def capabilities(self) -> list[Capability]:
        return _CAPABILITIES

    def evaluate(self, operation: str, state: dict) -> float:
        self.capability(operation)  # raises CapabilityNotSupported if unknown
        if operation == "pressure":
            return state["density"] * self.gas_constant * state["temperature"]
        if operation == "density":
            return state["pressure"] / (self.gas_constant * state["temperature"])
        if operation == "temperature":
            return state["pressure"] / (self.gas_constant * state["density"])
        raise AssertionError(f"unreachable: {operation!r} passed capability() check")

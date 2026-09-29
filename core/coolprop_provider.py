# core/coolprop_provider.py
"""The first provider backed by a real external library (core/ideal_gas_
provider.py needs none, which is why it -- not this -- proved the property-
model architecture first). Wraps CoolProp's high-level PropsSI interface for
water/steam and anything else CoolProp knows how to compute.

CoolProp's own SI unit system already matches MonOSS's convention (Sec 12:
Pa, K, J/kg, J/(kg*K), kg/m^3) exactly, so evaluate() does no unit
conversion -- just translates operation/argument names to PropsSI's
single-letter parameter codes.

Capability naming: each operation is named `<output>_<input-initials>`
(e.g. `enthalpy_pt`, `enthalpy_ps`) rather than bare `enthalpy` for every
input combination. PropertyProvider.capability() (core/providers.py) looks
up a capability purely by matching `operation` name -- two capabilities
sharing a name (e.g. one taking pressure+temperature, another taking
pressure+entropy, both length 2) would collide, with the first-registered
one always winning regardless of which inputs were actually supplied. That
would silently hand a P,s call's entropy argument to CoolProp as if it were
a temperature. Suffixing the inputs keeps every operation name unique.
"""
from CoolProp.CoolProp import PropsSI

from core.capability import Capability
from core.providers import (
    InvalidModelConfiguration,
    OutsideDomain,
    PropertyProvider,
    ProviderExecutionError,
)

_CAPABILITIES = (
    Capability(
        operation="enthalpy_pt",
        inputs=("pressure", "temperature"),
        output="enthalpy",
        units="J/kg",
    ),
    Capability(
        operation="entropy_pt",
        inputs=("pressure", "temperature"),
        output="entropy",
        units="J/(kg*K)",
    ),
    Capability(
        operation="density_pt",
        inputs=("pressure", "temperature"),
        output="density",
        units="kg/m^3",
    ),
    Capability(
        operation="temperature_ph",
        inputs=("pressure", "enthalpy"),
        output="temperature",
        units="K",
    ),
    Capability(
        operation="enthalpy_ps",
        inputs=("pressure", "entropy"),
        output="enthalpy",
        units="J/kg",
    ),
    # Quality (vapor mass fraction, 0-1) only means anything inside the
    # two-phase dome -- CoolProp raises for a state PropsSI can't place
    # there, which evaluate() already wraps as ProviderExecutionError the
    # same way it does any other out-of-domain state, so no new error
    # handling is needed for these. quality_ph/quality_ps are what a
    # turbine rule uses to check exit wetness after an isentropic
    # expansion (core/coolprop_provider.py's own enthalpy_ps lookup).
    Capability(
        operation="quality_ph",
        inputs=("pressure", "enthalpy"),
        output="quality",
        units="dimensionless",
    ),
    Capability(
        operation="quality_ps",
        inputs=("pressure", "entropy"),
        output="quality",
        units="dimensionless",
    ),
    Capability(
        operation="enthalpy_pq",
        inputs=("pressure", "quality"),
        output="enthalpy",
        units="J/kg",
    ),
    Capability(
        operation="entropy_pq",
        inputs=("pressure", "quality"),
        output="entropy",
        units="J/(kg*K)",
    ),
    Capability(
        operation="temperature_pq",
        inputs=("pressure", "quality"),
        output="temperature",
        units="K",
    ),
    Capability(
        operation="density_pq",
        inputs=("pressure", "quality"),
        output="density",
        units="kg/m^3",
    ),
)

# MonOSS input/output name -> PropsSI parameter code.
_PROPSI_KEY = {
    "pressure": "P",
    "temperature": "T",
    "enthalpy": "H",
    "entropy": "S",
    "density": "D",
    "quality": "Q",
}


class CoolPropProvider(PropertyProvider):
    DESCRIPTION = (
        "Real-fluid properties (water/steam and anything else CoolProp knows) "
        "via the CoolProp library. Operations are named <output>_<input-initials> "
        "since one output is often reachable from more than one input pair."
    )
    CONFIGURATION = {
        "fluid": "CoolProp fluid name, e.g. 'Water'",
        "backend": "optional, defaults to 'HEOS'",
    }
    CAPABILITIES = _CAPABILITIES

    def __init__(self, configuration: dict):
        super().__init__(configuration)
        fluid = configuration.get("fluid")
        if not isinstance(fluid, str) or not fluid.strip():
            raise InvalidModelConfiguration(
                "coolprop provider requires a 'fluid' name (e.g. 'Water') in its configuration"
            )
        backend = configuration.get("backend", "HEOS")
        if not isinstance(backend, str) or not backend.strip():
            raise InvalidModelConfiguration(
                "coolprop provider's 'backend' must be a non-empty string"
            )
        self.fluid_spec = f"{backend}::{fluid}"

    def evaluate(self, operation: str, state: dict) -> float:
        capability = self.capability(operation)  # raises CapabilityNotSupported if unknown
        output_key = _PROPSI_KEY[capability.output]
        (name1, value1), (name2, value2) = (
            (_PROPSI_KEY[key], state[key]) for key in capability.inputs
        )
        try:
            result = float(PropsSI(output_key, name1, value1, name2, value2, self.fluid_spec))
        except Exception as e:
            raise ProviderExecutionError(
                f"CoolProp could not evaluate {operation}({', '.join(capability.inputs)}) "
                f"for {self.fluid_spec!r}: {e}"
            ) from e
        # CoolProp doesn't raise for a quality query outside the two-phase
        # dome (a subcooled liquid or superheated vapor state) -- it
        # silently returns -1, which would otherwise flow into a
        # simulation looking like a real (nonsensical) 0-1 quality value.
        if capability.output == "quality" and result < 0:
            raise OutsideDomain(
                f"{operation}({', '.join(capability.inputs)}) for {self.fluid_spec!r} "
                "has no quality -- the state is outside the two-phase dome "
                "(single-phase liquid or vapor)"
            )
        return result

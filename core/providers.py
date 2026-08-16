# core/providers.py
"""The provider contract: the compatibility layer between MonOSS and an
actual computational implementation (an external library, a lookup
table, a closed-form equation). A simulation rule never talks to a
provider directly -- see the `property()` builtin in core/formula.py and
the resolver in core/model_resolver.py -- so nothing here knows about
motes, relationships, or the database.

Providers are registered explicitly (register_provider), never resolved
via dynamic import from a database string, so a garden can't cause
arbitrary code to load just by naming a provider.
"""
from abc import ABC, abstractmethod

from core.capability import Capability
from core.formula import FormulaError


class PropertyModelError(FormulaError):
    """Base for every physical-property-model error. Subclasses
    FormulaError so these flow through core/simulation.py's existing
    `except FormulaError` handling with no changes there -- a rule that
    hits one of these fails the simulation with a clear message, exactly
    like any other formula error."""


class ProviderNotFound(PropertyModelError):
    """No provider is registered under the requested identifier."""


class InvalidModelConfiguration(PropertyModelError):
    """A property-model mote's configuration is missing or malformed for
    the provider it names."""


class CapabilityNotSupported(PropertyModelError):
    """The provider doesn't support the requested operation."""


class InvalidState(PropertyModelError):
    """The state passed to evaluate() is missing or malformed inputs."""


class OutsideDomain(PropertyModelError):
    """The requested state is outside the provider's valid domain."""


class ProviderExecutionError(PropertyModelError):
    """The underlying implementation raised while evaluating -- wraps the
    original exception as debugging context rather than letting an
    obscure provider-specific exception leak into the simulation layer."""


class PropertyProvider(ABC):
    """Base class for every property provider. A provider is unaware of
    the broader mote graph -- it only knows its own configuration and how
    to evaluate its capabilities.

    DESCRIPTION, CONFIGURATION and CAPABILITIES are declared on the class
    rather than per instance so they can be read without constructing
    one, which needs provider-specific configuration a caller asking
    "what providers exist?" doesn't have. That is what lets
    describe_providers() below derive the reference core/reserved.py used
    to keep as a hand-copied second table.
    """

    #: One line on what this provider is and what it needs.
    DESCRIPTION: str = ""
    #: Configuration key -> what that key means, for someone writing a
    #: property-model mote by hand.
    CONFIGURATION: dict[str, str] = {}
    #: Every operation this provider supports.
    CAPABILITIES: tuple[Capability, ...] = ()

    def __init__(self, configuration: dict):
        self.configuration = configuration

    def capabilities(self) -> list[Capability]:
        """Every operation this provider supports. Reads the class
        declaration by default; a provider whose operations genuinely
        depend on its configuration may override this, and nothing else
        here assumes it doesn't."""
        return list(type(self).CAPABILITIES)

    @abstractmethod
    def evaluate(self, operation: str, state: dict) -> float:
        """Evaluate `operation` given a state dict keyed by that
        capability's declared `inputs`. Raise InvalidState, OutsideDomain,
        or ProviderExecutionError as appropriate."""

    def capability(self, operation: str) -> Capability:
        """Look up a declared capability by operation name -- shared by
        every provider rather than reimplemented per subclass."""
        for cap in self.capabilities():
            if cap.operation == operation:
                return cap
        available = ", ".join(c.operation for c in self.capabilities())
        raise CapabilityNotSupported(
            f"provider {type(self).__name__!r} does not support {operation!r}. "
            f"Available operations: {available or '(none)'}"
        )


_PROVIDERS: dict[str, type[PropertyProvider]] = {}


def register_provider(name: str, cls: type[PropertyProvider]) -> None:
    _PROVIDERS[name] = cls


def resolve_provider(name: str) -> type[PropertyProvider]:
    try:
        return _PROVIDERS[name]
    except KeyError:
        available = ", ".join(sorted(_PROVIDERS)) or "(none registered)"
        raise ProviderNotFound(
            f"no provider registered as {name!r}. Available providers: {available}"
        ) from None


def list_providers() -> list[str]:
    return sorted(_PROVIDERS)


def describe_providers() -> dict[str, dict]:
    """Every registered provider, as the reference an LLM or a person
    setting up a property-model mote needs: what it is, what
    configuration it takes, and every operation with its inputs, output
    and units.

    Derived from the providers themselves. core/reserved.py used to carry
    this as a hand-written table because capabilities() needed a
    constructed instance -- three places (the provider, that table, and
    docs/simulation.md's prose) claiming the same facts, two of them able
    to go quietly stale."""
    return {
        name: {
            "description": cls.DESCRIPTION,
            "configuration": dict(cls.CONFIGURATION),
            "capabilities": [
                {
                    "operation": cap.operation,
                    "inputs": list(cap.inputs),
                    "output": cap.output,
                    "units": cap.units,
                }
                for cap in cls.CAPABILITIES
            ],
        }
        for name, cls in sorted(_PROVIDERS.items())
    }

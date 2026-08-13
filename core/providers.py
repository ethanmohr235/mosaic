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
    to evaluate its capabilities."""

    def __init__(self, configuration: dict):
        self.configuration = configuration

    @abstractmethod
    def capabilities(self) -> list[Capability]:
        """Every operation this provider supports."""

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

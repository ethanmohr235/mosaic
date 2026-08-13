# core/model_resolver.py
"""Turns a mote's `uses_model` relationship into an executable
PropertyProvider. Pure and DB-free, like core/formula.py's Context --
takes the same plain-callable shapes (`properties`, `targets`) rather
than a connection, so it stays trivially testable with dicts and never
gets re-resolved from the database mid-formula-evaluation. See
docs/physical_property_proposal.

Importing this module registers every built-in provider (currently just
Ideal Gas) -- the single explicit registration point. Anything that
resolves a model imports this module first, so registration always
happens before resolution; providers are never resolved via dynamic
import from a database string.
"""
from collections.abc import Callable

from core.ideal_gas_provider import IdealGasProvider
from core.providers import InvalidModelConfiguration, PropertyModelError, PropertyProvider, register_provider, resolve_provider

USES_MODEL = "uses_model"
PROVIDER_KEY = "provider"
CONFIGURATION_KEY = "configuration"

register_provider("ideal_gas", IdealGasProvider)


class ModelNotFound(PropertyModelError):
    """A mote has no usable `uses_model` relationship, or the mote it
    points at doesn't exist."""


def resolve(
    mote_id: str,
    properties: Callable[[str], dict],
    targets: Callable[[str, str], list[str]],
) -> PropertyProvider:
    """Follow mote_id's `uses_model` relationship to a property-model
    mote, and construct the provider it names. Only the first target is
    used -- a mote with several `uses_model` relationships is a
    modelling mistake, not a feature, the same precedent
    core/species.py's template_for() sets for `template_of`."""
    model_ids = targets(mote_id, USES_MODEL)
    if not model_ids:
        raise ModelNotFound(
            f"mote {mote_id[:8]} has no {USES_MODEL!r} relationship to a property model"
        )
    model_id = model_ids[0]
    model_properties = properties(model_id)

    provider_name = model_properties.get(PROVIDER_KEY)
    if not isinstance(provider_name, str) or not provider_name.strip():
        raise InvalidModelConfiguration(
            f"property model {model_id[:8]} has no {PROVIDER_KEY!r} property"
        )

    configuration = model_properties.get(CONFIGURATION_KEY, {})
    if not isinstance(configuration, dict):
        raise InvalidModelConfiguration(
            f"property model {model_id[:8]}'s {CONFIGURATION_KEY!r} must be an object, "
            f"got {configuration!r}"
        )

    provider_cls = resolve_provider(provider_name)
    return provider_cls(configuration)

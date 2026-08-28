# core/model_resolver.py
"""Turns a mote's `uses_model` relationship into an executable
PropertyProvider. Pure and DB-free, like core/formula.py's Context --
takes the same plain-callable shapes (`properties`, `targets`) rather
than a connection, so it stays trivially testable with dicts and never
gets re-resolved from the database mid-formula-evaluation. See
docs/physical_property_proposal.

Importing this module registers every built-in provider (currently Ideal
Gas and CoolProp) -- the single explicit registration point. Anything that
resolves a model imports this module first, so registration always
happens before resolution; providers are never resolved via dynamic
import from a database string.
"""
from collections.abc import Callable, Iterable

from core.coolprop_provider import CoolPropProvider
from core.ideal_gas_provider import IdealGasProvider
from core.entities import Relationship
from core.providers import InvalidModelConfiguration, PropertyModelError, PropertyProvider, register_provider, resolve_provider

USES_MODEL = "uses_model"
PROVIDER_KEY = "provider"
CONFIGURATION_KEY = "configuration"

register_provider("ideal_gas", IdealGasProvider)
register_provider("coolprop", CoolPropProvider)


def model_ids_in(relationships: Iterable[Relationship]) -> set[str]:
    """Which motes a set of relationships makes into property models --
    the target of at least one uses_model. The same shape as
    core/species.py::species_ids_in(), so "which motes play role X" has
    one answer per role instead of one per caller (the CLI used to work
    this out for itself, twice).

    Pure, like everything else in this module: it takes relationships
    rather than a connection, which is also what lets the caller decide
    whether to scan the whole garden or just a subgraph."""
    return {r.to_id for r in relationships if r.type == USES_MODEL}


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
    return construct(model_id, properties(model_id))


def construct(model_id: str, model_properties: dict) -> PropertyProvider:
    """Build the provider a property-model mote names, from its own
    properties -- split out of resolve() so a caller that already has the
    model mote in hand (e.g. the CLI's `model show`/`model capabilities`,
    inspecting the model mote directly rather than following `uses_model`
    to it) doesn't have to duplicate this validation."""
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

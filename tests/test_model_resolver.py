import pytest

from core.model_resolver import ModelNotFound, construct, resolve
from core.providers import InvalidModelConfiguration, ProviderNotFound

# Dict-based, no DB -- core/model_resolver.py takes the same plain-callable
# shapes core/formula.py's Context does, precisely so it can be tested
# without touching a database.
PROPERTIES = {
    "air": {"name": "Air"},
    "ideal-air": {"provider": "ideal_gas", "configuration": {"gas_constant": 287.05}},
    "no-provider-model": {"configuration": {"gas_constant": 1.0}},
    "bad-config-model": {"provider": "ideal_gas", "configuration": "not a dict"},
    "unregistered-model": {"provider": "nonexistent_provider"},
    "no-config-model": {"provider": "ideal_gas"},
}
RELATIONSHIPS = [
    ("air", "ideal-air", "uses_model"),
    ("no-provider", "no-provider-model", "uses_model"),
    ("bad-config", "bad-config-model", "uses_model"),
    ("unregistered", "unregistered-model", "uses_model"),
    ("no-config", "no-config-model", "uses_model"),
]


def properties(mote_id):
    return PROPERTIES.get(mote_id, {})


def targets(mote_id, relationship_type):
    return [to for frm, to, ty in RELATIONSHIPS if frm == mote_id and ty == relationship_type]


def test_resolve_returns_a_working_provider():
    provider = resolve("air", properties, targets)
    assert provider.evaluate("pressure", {"density": 1.0, "temperature": 300.0}) == pytest.approx(
        1.0 * 287.05 * 300.0
    )


def test_resolve_raises_model_not_found_without_uses_model():
    with pytest.raises(ModelNotFound, match="uses_model"):
        resolve("nothing-here", properties, targets)


def test_resolve_raises_invalid_configuration_without_provider_key():
    with pytest.raises(InvalidModelConfiguration, match="provider"):
        resolve("no-provider", properties, targets)


def test_resolve_raises_invalid_configuration_for_non_dict_configuration():
    with pytest.raises(InvalidModelConfiguration, match="configuration"):
        resolve("bad-config", properties, targets)


def test_resolve_raises_provider_not_found_for_unregistered_provider():
    with pytest.raises(ProviderNotFound, match="nonexistent_provider"):
        resolve("unregistered", properties, targets)


def test_resolve_defaults_missing_configuration_to_empty_dict():
    # ideal_gas itself requires gas_constant, so this should surface as
    # that provider's own InvalidModelConfiguration, not a resolver error.
    with pytest.raises(InvalidModelConfiguration, match="gas_constant"):
        resolve("no-config", properties, targets)


def test_construct_builds_the_same_provider_resolve_uses():
    # resolve() is just "follow uses_model, then construct()" -- this
    # confirms the split didn't change resolve()'s behavior.
    provider = construct("ideal-air", properties("ideal-air"))
    assert provider.evaluate("pressure", {"density": 1.0, "temperature": 300.0}) == pytest.approx(
        1.0 * 287.05 * 300.0
    )


def test_construct_raises_invalid_configuration_without_provider_key():
    with pytest.raises(InvalidModelConfiguration, match="provider"):
        construct("no-provider-model", properties("no-provider-model"))

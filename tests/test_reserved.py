from core.reserved import (
    EQUATION_LANGUAGE,
    PROPERTY_MEANINGS,
    PROPERTY_MODEL_PROVIDERS,
    RELATIONSHIP_TYPE_MEANINGS,
    RESERVED_PROPERTIES,
    RESERVED_RELATIONSHIP_TYPES,
)


def test_reserved_properties_match_the_documented_list():
    # Kept in sync by hand with docs/architecture.md's reserved-name list.
    assert RESERVED_PROPERTIES == {
        "equation", "unknowns", "url", "date", "duration", "recurrence", "gcal_event_id",
        "provider", "configuration", "portal_garden", "portal_mote",
    }


def test_reserved_relationship_types_match_the_documented_list():
    assert RESERVED_RELATIONSHIP_TYPES == {
        "instance_of", "template_of", "applies_to", "uses_model", "in_folder",
    }


def test_every_reserved_property_has_a_meaning():
    # PROPERTY_MEANINGS is what an MCP client actually reads (via
    # reserved_names()) to learn what a reserved key does -- a key
    # missing here is silently invisible to it.
    assert PROPERTY_MEANINGS.keys() == RESERVED_PROPERTIES


def test_every_reserved_relationship_type_has_a_meaning():
    assert RELATIONSHIP_TYPE_MEANINGS.keys() == RESERVED_RELATIONSHIP_TYPES


def test_equation_language_entries_have_syntax_and_meaning():
    assert EQUATION_LANGUAGE
    for entry in EQUATION_LANGUAGE:
        assert entry["syntax"]
        assert entry["meaning"]


def test_property_model_providers_match_the_registry():
    from core import model_resolver  # noqa: F401 -- registers ideal_gas/coolprop
    from core.providers import list_providers

    # Subset, not equality: core/providers.py's registry is a shared
    # global that other tests (test_providers.py) register throwaway
    # providers into, so it can hold more than just the two built-ins.
    assert set(PROPERTY_MODEL_PROVIDERS) <= set(list_providers())
    assert set(PROPERTY_MODEL_PROVIDERS) == {"ideal_gas", "coolprop"}
    for provider in PROPERTY_MODEL_PROVIDERS.values():
        assert provider["capabilities"]
        for cap in provider["capabilities"]:
            assert cap["operation"] and cap["inputs"] and cap["output"]

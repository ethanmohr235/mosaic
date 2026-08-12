from core.reserved import RESERVED_PROPERTIES, RESERVED_RELATIONSHIP_TYPES


def test_reserved_properties_match_the_documented_list():
    # Kept in sync by hand with docs/architecture.md's reserved-name list.
    assert RESERVED_PROPERTIES == {
        "equation", "unknowns", "url", "date", "duration", "recurrence", "gcal_event_id",
    }


def test_reserved_relationship_types_match_the_documented_list():
    assert RESERVED_RELATIONSHIP_TYPES == {"instance_of", "template_of", "applies_to"}

# core/reserved.py
"""Central list of reserved names -- property keys and relationship types
recognized by convention rather than enforced by schema. See
docs/architecture.md's reserved-name list, which this must be kept in
sync with. Exposed to the frontend via Api.reserved_names() so the
detail panel can flag them instead of leaving them looking like
ordinary user data."""
from core.google_calendar import RESERVED_KEYS as _GCAL_RESERVED_PROPERTIES
from core.model_resolver import CONFIGURATION_KEY, PROVIDER_KEY, USES_MODEL
from core.roles import ROLES_KEY
from core.scenarios import OVERRIDES, VARIES
from core.rules import APPLIES_TO, EQUATION_KEY, FREEDOMS_KEY
from core.species import INSTANCE_OF, TEMPLATE_OF
from core.units import UNITS_KEY

RESERVED_PROPERTIES = _GCAL_RESERVED_PROPERTIES | {
    EQUATION_KEY, FREEDOMS_KEY, PROVIDER_KEY, CONFIGURATION_KEY, UNITS_KEY,
    ROLES_KEY,
}
RESERVED_RELATIONSHIP_TYPES = {
    INSTANCE_OF, TEMPLATE_OF, APPLIES_TO, USES_MODEL, OVERRIDES, VARIES,
}

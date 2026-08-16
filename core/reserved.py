# core/reserved.py
"""Central list of reserved names -- property keys and relationship types
recognized by convention rather than enforced by schema. See
docs/architecture.md's reserved-name list, which this must be kept in
sync with. Exposed to the frontend via Api.reserved_names() so the
detail panel can flag them instead of leaving them looking like
ordinary user data, and to MCP clients via mcp_server.server.reserved_
names() -- which is also why PROPERTY_MEANINGS, EQUATION_LANGUAGE and
PROPERTY_MODEL_PROVIDERS live here rather than as inline strings in the
MCP layer: an LLM driving MonOSS only through MCP tools has no other way
to reach this, since docs/simulation.md itself isn't on the MCP surface.
Hand-synced with docs/simulation.md's own tables -- there is no dynamic
introspection here (e.g. of core/providers.py's registry) because a
provider's capabilities() requires a constructed instance, which needs
provider-specific configuration this module has no business knowing."""
from core.google_calendar import RESERVED_KEYS as _GCAL_RESERVED_PROPERTIES
from core.model_resolver import CONFIGURATION_KEY, PROVIDER_KEY, USES_MODEL
from core.portals import PORTAL_GARDEN_KEY, PORTAL_MOTE_KEY
from core.rules import APPLIES_TO, EQUATION_KEY, UNKNOWNS_KEY
from core.species import INSTANCE_OF, TEMPLATE_OF

RESERVED_PROPERTIES = _GCAL_RESERVED_PROPERTIES | {
    EQUATION_KEY, UNKNOWNS_KEY, PROVIDER_KEY, CONFIGURATION_KEY,
    PORTAL_GARDEN_KEY, PORTAL_MOTE_KEY,
}
RESERVED_RELATIONSHIP_TYPES = {INSTANCE_OF, TEMPLATE_OF, APPLIES_TO, USES_MODEL}

# One line per reserved property key -- kept in the same declaration order
# as the imports above, not sorted, so a diff adding a key is obvious.
PROPERTY_MEANINGS: dict[str, str] = {
    EQUATION_KEY: "A rule mote's formula, `lhs == rhs`, in the expression "
        "language returned alongside this as `equationLanguage`. Paired with an "
        "`applies_to` relationship pointing at the species the rule runs "
        "against, once per instance.",
    UNKNOWNS_KEY: "On a species mote: which of its instances' properties "
        "the solver may determine. A comma-separated string "
        "('voltage, current') or a JSON list (['voltage']).",
    PROVIDER_KEY: "On a property-model mote: the registered PropertyProvider "
        "name it's constructed from -- see `propertyModelProviders` "
        "returned alongside this.",
    CONFIGURATION_KEY: "On a property-model mote: the JSON object the named "
        "`provider` needs to construct itself, e.g. {\"gas_constant\": "
        "287.05} for ideal_gas.",
    PORTAL_GARDEN_KEY: "Together with portal_mote, marks a mote as a portal "
        "-- a one-way navigational reference to a mote in a different "
        "garden. Set by create_portal(); read by resolve_portal(). Names "
        "the target garden.",
    PORTAL_MOTE_KEY: "Together with portal_garden, marks a mote as a portal. "
        "Names the target mote's id within that garden.",
    "date": "An ISO date ('2026-08-15') or datetime ('2026-08-15T14:00:00') "
        "marking a mote as exportable to Google Calendar (paired with "
        "duration/recurrence). Export itself is a UI-only action, not an "
        "MCP tool.",
    "duration": "Minutes for a timed `date` (one with a 'T'), or days for an "
        "all-day `date`. Defaults to 60 or 1 if absent.",
    "recurrence": "An RFC 5545 RRULE ('FREQ=WEEKLY;COUNT=5'), with or "
        "without a leading 'RRULE:', for a recurring calendar event.",
    "gcal_event_id": "Auto-set by the UI's calendar export after pushing "
        "this mote as an event, so the next export updates it instead of "
        "creating a duplicate. Not meant to be hand-authored.",
    "url": "A web address or local file path. core/links.py classifies it "
        "as a URL (has a scheme, or looks like a bare domain) or an "
        "explicit local path ('/', '~', './', '../', or a drive letter) "
        "for the UI's 'open' action.",
}

RELATIONSHIP_TYPE_MEANINGS: dict[str, str] = {
    INSTANCE_OF: "mote --instance_of--> species. Declares species "
        "membership; a mote may have more than one. Set via set_species() "
        "or create_instance().",
    TEMPLATE_OF: "mote --template_of--> species. Marks the mote whose "
        "properties new instances of that species start from. Set via "
        "set_template(); the template itself is never an instance.",
    APPLIES_TO: "rule mote --applies_to--> species. The rule's `equation` "
        "is evaluated once per instance of that species.",
    USES_MODEL: "mote --uses_model--> property-model mote. Lets a rule's "
        "`property('rel', 'operation', ...)` call resolve: it navigates "
        "'rel' from the rule's own mote first, then follows that target's "
        "uses_model to find the provider/configuration to evaluate "
        "'operation' against.",
}

# Mirrors docs/simulation.md's "Expression language" table. Order matches
# that table, which roughly goes simple -> compound -> time -> models.
EQUATION_LANGUAGE: list[dict[str, str]] = [
    {"syntax": "name", "meaning":
        "A property of the mote the rule is running against. An unknown "
        "declared by its species becomes solvable; otherwise it must "
        "already hold a number."},
    {"syntax": "targets('type')", "meaning":
        "The motes this one points at via a relationship of type 'type'."},
    {"syntax": "sources('type')", "meaning":
        "The motes pointing *at* this one via a relationship of type "
        "'type'."},
    {"syntax": "targets('type').prop / sources('type').prop", "meaning":
        "That property on those motes, resolved live against the same "
        "solve -- not a snapshot. Collapses to a single value when exactly "
        "one mote is reached; otherwise must be wrapped in sum()/count()."},
    {"syntax": "sum(...), count(...)", "meaning":
        "Combine several motes' values into one. Required whenever "
        "targets()/sources() reaches more than one mote."},
    {"syntax": "+ - * / **", "meaning": "Arithmetic. Parentheses work as usual."},
    {"syntax": "abs(x), min(...), max(...)", "meaning":
        "Only on values that are already known by the time they're "
        "evaluated -- deferred otherwise, same as property()."},
    {"syntax": "t", "meaning":
        "The current step's index within a time-based run (0 for the "
        "first step, and always 0 in a plain non-time-based run). Shadows "
        "a property literally named 't' inside a formula only."},
    {"syntax": "prev(x)", "meaning":
        "The value x had entering the current step, for a recurrence "
        "('balance == prev(balance) * 1.05'). x must be a bare "
        "property/unknown name on the current mote. Only valid in a "
        "time-based run (see preview_simulation)."},
    {"syntax": "property('rel', 'operation', arg, ...)", "meaning":
        "Evaluates 'operation' through the property model of the single "
        "mote reached by relationship type 'rel', e.g. property"
        "('working_fluid', 'enthalpy_pt', pressure, temperature). 'rel' "
        "and 'operation' must be quoted string literals; the remaining "
        "arguments must already be known numbers. See "
        "`propertyModelProviders` (returned alongside this) for what "
        "operations exist."},
]

# Mirrors docs/simulation.md's "Physical property models" section. Each
# provider is registered in core/model_resolver.py; a mote uses one by
# pointing `uses_model` at a mote carrying `provider`=<name here> and a
# matching `configuration`.
PROPERTY_MODEL_PROVIDERS: dict[str, dict] = {
    "ideal_gas": {
        "description": "P = rho * R * T. No external dependency.",
        "configuration": {"gas_constant": "number, J/(kg*K), e.g. 287.05 for dry air"},
        "capabilities": [
            {"operation": "pressure", "inputs": ["density", "temperature"], "output": "pressure", "units": "Pa"},
            {"operation": "density", "inputs": ["pressure", "temperature"], "output": "density", "units": "kg/m^3"},
            {"operation": "temperature", "inputs": ["pressure", "density"], "output": "temperature", "units": "K"},
        ],
    },
    "coolprop": {
        "description": "Real-fluid properties (water/steam and anything "
            "else CoolProp knows) via the CoolProp library. Operations are "
            "named <output>_<input-initials> since one output is often "
            "reachable from more than one input pair.",
        "configuration": {
            "fluid": "CoolProp fluid name, e.g. 'Water'",
            "backend": "optional, defaults to 'HEOS'",
        },
        "capabilities": [
            {"operation": "enthalpy_pt", "inputs": ["pressure", "temperature"], "output": "enthalpy", "units": "J/kg"},
            {"operation": "entropy_pt", "inputs": ["pressure", "temperature"], "output": "entropy", "units": "J/(kg*K)"},
            {"operation": "density_pt", "inputs": ["pressure", "temperature"], "output": "density", "units": "kg/m^3"},
            {"operation": "temperature_ph", "inputs": ["pressure", "enthalpy"], "output": "temperature", "units": "K"},
            {"operation": "enthalpy_ps", "inputs": ["pressure", "entropy"], "output": "enthalpy", "units": "J/kg"},
            {"operation": "quality_ph", "inputs": ["pressure", "enthalpy"], "output": "quality", "units": "dimensionless"},
            {"operation": "quality_ps", "inputs": ["pressure", "entropy"], "output": "quality", "units": "dimensionless"},
            {"operation": "enthalpy_pq", "inputs": ["pressure", "quality"], "output": "enthalpy", "units": "J/kg"},
            {"operation": "entropy_pq", "inputs": ["pressure", "quality"], "output": "entropy", "units": "J/(kg*K)"},
            {"operation": "temperature_pq", "inputs": ["pressure", "quality"], "output": "temperature", "units": "K"},
            {"operation": "density_pq", "inputs": ["pressure", "quality"], "output": "density", "units": "kg/m^3"},
        ],
    },
}

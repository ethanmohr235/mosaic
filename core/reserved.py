# core/reserved.py
"""Central list of reserved names -- property keys and relationship types
recognized by convention rather than enforced by schema. See
docs/architecture.md's reserved-name list, which this must be kept in
sync with. Exposed to the frontend via Api.reserved_names() so the
detail panel can flag them instead of leaving them looking like
ordinary user data, and to MCP clients via mcp_server.server.reserved_
names() -- which is also why PROPERTY_MEANINGS and EQUATION_LANGUAGE live
here rather than as inline strings in the MCP layer: an LLM driving
MonOSS only through MCP tools has no other way to reach this, since
docs/simulation.md itself isn't on the MCP surface. Those two are
hand-synced with docs/simulation.md's own tables.

PROPERTY_MODEL_PROVIDERS is *not* written here -- it is derived from the
registered providers themselves. It used to be a hand-copy, because
capabilities() needed a constructed instance and constructing one needs
provider-specific configuration this module has no business knowing;
declaring DESCRIPTION/CONFIGURATION/CAPABILITIES on the provider class
removed that reason, and with it the third place the same facts were
written down."""
from core.google_calendar import RESERVED_KEYS as _GCAL_RESERVED_PROPERTIES
from core.folders import IN_FOLDER
from core.model_resolver import CONFIGURATION_KEY, PROVIDER_KEY, USES_MODEL
from core.modoc import ABOUT, MODOC_KEY
from core.portals import PORTAL_GARDEN_KEY, PORTAL_MOTE_KEY
from core.providers import describe_providers
from core.roles import ROLES_KEY
from core.rules import APPLIES_TO, EQUATION_KEY, FREEDOMS_KEY
from core.scenarios import OVERRIDES, VARIES
from core.species import INSTANCE_OF, TEMPLATE_OF
from core.units import UNITS_KEY

RESERVED_PROPERTIES = _GCAL_RESERVED_PROPERTIES | {
    EQUATION_KEY, FREEDOMS_KEY, PROVIDER_KEY, CONFIGURATION_KEY, UNITS_KEY,
    ROLES_KEY, PORTAL_GARDEN_KEY, PORTAL_MOTE_KEY, MODOC_KEY,
}
RESERVED_RELATIONSHIP_TYPES = {
    INSTANCE_OF, TEMPLATE_OF, APPLIES_TO, USES_MODEL, IN_FOLDER, ABOUT,
    OVERRIDES, VARIES,
}

# One line per reserved property key -- kept in the same declaration order
# as the imports above, not sorted, so a diff adding a key is obvious.
PROPERTY_MEANINGS: dict[str, str] = {
    EQUATION_KEY: "A rule mote's formula, `lhs == rhs`, in the expression "
        "language returned alongside this as `equationLanguage`. Paired with an "
        "`applies_to` relationship pointing at the species the rule runs "
        "against, once per instance.",
    FREEDOMS_KEY: "Which of a mote's properties the solver may determine. "
        "A comma-separated string ('voltage, current') or a JSON list "
        "(['voltage']). On a mote acting as a species it declares what its "
        "instances may solve for; on any other mote it declares that mote's "
        "own, and *replaces* rather than adds to what its species declare -- "
        "which is how two instances of one species can pose opposite "
        "questions (sizing vs rating).",
    ROLES_KEY: "On a mote acting as a species: the named parts its instances "
        "are wired to, as {'turbine': {'arity': 'one'}}. A role is an "
        "ordinary relationship whose *type* is the role's name, so a rule can "
        "address a part by name (`turbine.work`). On any other mote it "
        "declares that mote's own parts. Checked by `roles check`, not by "
        "every run. Distinct from core/kinds.py, which is what a mote *is*.",
    UNITS_KEY: "A mote's per-property unit map, {'resistance': 'kohm'}. "
        "Opt-in per property -- a key with no entry is unitless. Units are "
        "registered at import time, never parsed from the database.",
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
    MODOC_KEY: "A .modoc note about this mote -- a small LaTeX subset whose "
        "\\mote/\\val/\\graph macros resolve live against the garden. The "
        "property is the canonical copy; .modoc files are import/export only. "
        "Written and read in Mora (run_mora.py); see docs/modoc.md.",
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
    IN_FOLDER: "mote --in_folder--> folder. Purely organizational annotation -- "
        "a folder is an ordinary mote with no other structural meaning, "
        "never picked up by simulation unless separately declared an "
        "instance_of a species. Set via set_folder().",
    OVERRIDES: "override mote --overrides--> target. The override holds "
        "replacement properties for that one mote, and is excluded from every "
        "run unless a scenario selects it -- the same exclusion a template "
        "gets. Mirrors template_of one arrow over.",
    VARIES: "scenario mote --varies--> override. Gathers the overrides one "
        "scenario applies. A scenario run never writes to the graph: it "
        "answers what would happen, so run_time_series(commit=True, "
        "scenario=...) refuses outright.",
    ABOUT: "note mote --about--> subject. Optional, and only meaningful on a "
        "mote carrying a `modoc` note: it moves the mote that note's "
        "\\val{key} and \\graph read from, so a note can live on its own "
        "mote while still speaking about another. A note with no `about` "
        "reads from itself; one with several is ambiguous and falls back "
        "to itself.",
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

# Derived, not declared: every registered provider describes itself
# (core/providers.py::describe_providers, reading the DESCRIPTION /
# CONFIGURATION / CAPABILITIES each provider class carries). A provider
# added, or an operation changed, without this file being touched is
# correct here automatically -- which is the point. Registration happens
# on import of core.model_resolver, above, so every provider is present
# by the time this runs.
PROPERTY_MODEL_PROVIDERS: dict[str, dict] = describe_providers()

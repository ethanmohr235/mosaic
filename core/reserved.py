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
from core.links import URL_PROPERTY
from core.model_resolver import CONFIGURATION_KEY, PROVIDER_KEY, USES_MODEL
from core.modoc import ABOUT, MODOC_KEY
from core.providers import describe_providers
from core.query import QUERY_KEY
from core.roles import ROLES_KEY
from core.rules import APPLIES_TO, EQUATION_KEY, FREEDOMS_KEY
from core.scenarios import OVERRIDES, VARIES
from core.species import INSTANCE_OF, TEMPLATE_OF
from core.units import UNITS_KEY

RESERVED_PROPERTIES = {
    URL_PROPERTY,
    EQUATION_KEY, FREEDOMS_KEY, PROVIDER_KEY, CONFIGURATION_KEY, UNITS_KEY,
    ROLES_KEY, MODOC_KEY, QUERY_KEY,
}
RESERVED_RELATIONSHIP_TYPES = {
    INSTANCE_OF, TEMPLATE_OF, APPLIES_TO, USES_MODEL, ABOUT,
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
    MODOC_KEY: "A .modoc note about this mote -- a small LaTeX subset whose "
        "\\mote/\\val/\\graph macros resolve live against the garden. The "
        "property is the canonical copy; .modoc files are import/export only. "
        "Written and read in Mora (run_mora.py); see docs/modoc.md.",
    QUERY_KEY: "A saved view: the structured query (filter, sort, grouping "
        "and columns) a table of the garden is drawn from, as JSON -- see "
        "core/query.py for the shape. A field in it names a property key, a "
        "builtin (name/id/species/degree/updated), a relationship type in one "
        "direction, or the graph itself (instance_of/kind/related), which is "
        "how a query asks about shape and not only about properties. Written "
        "and read in Monolith (run_monolith.py).",
    "url": "A web address or local file path. core/links.py classifies it "
        "as a URL (has a scheme, or looks like a bare domain) or an "
        "explicit local path ('/', '~', './', '../', or a drive letter) "
        "for the UI's 'open' action.",
}

# Meaning carried by a value's *shape* rather than by its key's name.
#
# This is the other half of the reference above, and the more important
# half to hand an LLM: there is no property name to look up for "when
# something happens" any more. Anything shaped like an ISO 8601 interval
# is one, under whatever key the garden likes, so a client writing a date
# needs to know the notation rather than the name.
VALUE_SHAPES: dict[str, str] = {
    "interval": "When something happens, as one self-describing ISO 8601 "
        "string, under ANY property key you like -- `date`, `when`, `due`, "
        "`starts`, whatever the garden already says. There is no reserved "
        "name for this and no separate duration or recurrence property; "
        "the value carries its own span and rule.\n"
        "  '2026-09-01'                        an all-day event, one day\n"
        "  '2026-09-01/2026-09-03'             all-day, Sep 1 and 2 (the end is EXCLUSIVE)\n"
        "  '2026-09-01T09:00'                  a timed event, default 60 minutes\n"
        "  '2026-09-01T09:00/PT1H30M'          timed, 90 minutes\n"
        "  '2026-09-01T09:00/2026-09-01T10:30' timed, explicit end\n"
        "A repetition goes on the lines beneath, as RFC 5545:\n"
        "  '2026-09-01T09:00/PT1H\\nRRULE:FREQ=WEEKLY;BYDAY=TU'\n"
        "and an 'EXDATE:20260908T090000' line beneath that excepts one "
        "occurrence out of the series. Durations use only days/hours/"
        "minutes/seconds (P2D, PT1H30M) -- years and months have no fixed "
        "length and are refused. A mote may carry several of these under "
        "different keys; each places it separately.",
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
    {"syntax": "+ - * / **", "meaning":
        "Arithmetic. Parentheses work as usual. An unknown may sit in a "
        "denominator -- 'I / I0 == 0.1' is solved as the equivalent "
        "linear equation, so there's no need to rearrange it by hand."},
    {"syntax": "pi, e", "meaning":
        "The math constants (3.14159..., 2.71828...). Reserved words -- "
        "shadow a property literally named 'pi' or 'e' inside a formula "
        "only, the same way 't' does."},
    {"syntax": "log(x), ln(x), log10(x), exp(x)", "meaning":
        "Logarithms and the exponential. log() and ln() are the natural "
        "logarithm; log10() is base 10. Only on values that are already "
        "known -- but writing an exponential law as its logarithm ("
        "'mxs * x == -ln(I / I0)' rather than 'I == I0 * exp(-mxs * x)') "
        "makes it linear in x, so the default solver gets it exactly."},
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

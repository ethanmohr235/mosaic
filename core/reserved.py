# core/reserved.py
"""Central list of reserved names -- property keys and relationship types
recognized by convention rather than enforced by schema, plus the
console's verbs (RESERVED_VERBS, at the bottom). See
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
from core.calc import SOURCE_KEY
from core.functions import FUNCTION_KEY, PARAMETERS_KEY
from core.images import IMAGE_KEY
from core.links import URL_PROPERTY
from core.projects import ARCHIVED_KEY, GARDEN_KEY, SUBMISSION_KEY
from core.uncertainty import UNCERTAINTY_KEY
from core.model_resolver import CONFIGURATION_KEY, PROVIDER_KEY, USES_MODEL
from core.modoc import ABOUT, MODOC_KEY
from core.providers import describe_providers
from core.query import QUERY_KEY
from core.roles import ROLES_KEY
from core.rules import APPLIES_TO, CHECK_KEY, EQUATION_KEY, FREEDOMS_KEY, TOLERANCE_KEY
from core.scenarios import OVERRIDES, VARIES
from core.species import INSTANCE_OF, TEMPLATE_OF
from core.sweep import PLOT_KEY
from core.units import UNITS_KEY

RESERVED_PROPERTIES = {
    URL_PROPERTY,
    EQUATION_KEY, FREEDOMS_KEY, FUNCTION_KEY, PARAMETERS_KEY, PROVIDER_KEY, CONFIGURATION_KEY, UNITS_KEY,
    ROLES_KEY, MODOC_KEY, QUERY_KEY, PLOT_KEY, SOURCE_KEY, GARDEN_KEY,
    CHECK_KEY, TOLERANCE_KEY, UNCERTAINTY_KEY, ARCHIVED_KEY, IMAGE_KEY, SUBMISSION_KEY,
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
    FUNCTION_KEY: "A function mote's body: an expression of its `parameters` "
        "(and of its own properties, as constants), callable by the mote's "
        "`name` from any formula -- `phi(0)`, `phi(reactor.r)`. Never solved.",
    PARAMETERS_KEY: "A function mote's parameter names, in call order: a "
        "comma-separated string ('r, z') or a JSON list. A parameter's entry "
        "in `units` is what a bare number passed to it means.",
    CHECK_KEY: "On a rule mote: `true` makes the rule a check -- evaluated "
        "against the answer after solving and shown green or red, but never "
        "used to determine a value, even when written with `==` (`sum(Fy) == "
        "0` verifying equilibrium). A comparison (`<=`, `>=`, `<`, `>`) is "
        "always a check and needs no marking.",
    TOLERANCE_KEY: "On a check: how close an equality must come, as a fraction "
        "of the largest magnitude the relation involves. Defaults to 0.001 "
        "(0.1%). Comparisons ignore it.",
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
        "Written and read by the `edit` and `read` panes; see docs/modoc.md.",
    QUERY_KEY: "A saved view: the structured query (filter, sort, grouping "
        "and columns) a table of the garden is drawn from, as JSON -- see "
        "core/query.py for the shape. A field in it names a property key, a "
        "builtin (name/id/species/degree/updated), a relationship type in one "
        "direction, or the graph itself (instance_of/kind/related), which is "
        "how a query asks about shape and not only about properties. Read by "
        "the console's table as `find --view <name>` (core/findtext.py).",
    PLOT_KEY: "A saved plot: the specification a graph of the garden is "
        "drawn from, as JSON -- which property is swept and over what range, "
        "which properties are plotted against it, which scenarios and which "
        "transient overrides. It holds no computed points, only what to "
        "compute, so reopening it re-solves the garden as it stands rather "
        "than showing what it looked like when saved. Written and read in "
        "the console by `plot save` / `plot ‹name›` (core/plotcmd.py) and drawn "
        "in notes by `\\graph{name}`; the solving itself is core/sweep.py.",
    SOURCE_KEY: "Where each of a mote's values came from, as a per-property "
        "map in exactly the shape `units` has: {'x': 'assumed', 'sigma': "
        "'external'}. An entry may be the bare word or {'kind': ..., 'note': "
        "...} where the value deserves a sentence. `assumed` is what a "
        "document's \\assumptions section lists; `external` marks a value "
        "that came from outside the garden (an FEA probe, a measurement), "
        "which is what a verification table compares against a hand calc; "
        "`measured` is what a CSV import writes and `fitted` what `fit … "
        "--apply` writes (its note is the fit line). "
        "Opt-in per property, and unread by the solver -- a value's "
        "provenance changes nothing about how it is used.",
    GARDEN_KEY: "The name of a garden this mote owns, making the mote that "
        "project's cover. Nesting is recursion: a mote inside the owned garden "
        "can own one of its own. A garden has at most one owner -- two claims, "
        "a claim on a garden that doesn't exist, or a loop are flagged by "
        "`project_tree`'s problems, never enforced. Written by `start_project`, "
        "which creates the garden in the same step.",
    ARCHIVED_KEY: "`true` archives a project tree: on a mote that owns a "
        "garden (`garden`), it archives that garden and everything beneath it; "
        "on a mote claiming no garden, it archives the garden the mote lives "
        "in (how a loose garden is archived). An archived garden stays "
        "searchable and readable; every write tool refuses it. Set or clear it "
        "with `archive_garden` rather than by hand.",
    IMAGE_KEY: "A stored picture, as {'mime': 'image/png' | 'image/svg+xml', "
        "'data': <base64>, 'alt'?, 'width'?, 'height'?}. A mote carrying it is an "
        "image; a note shows it with `\\image{name}`. Add one with `monoss "
        "image add` or `add_image` rather than by hand -- those sniff the "
        "format, cap the size and clean an SVG.",
    SUBMISSION_KEY: "A frozen revision of a project's document, as {'revision': "
        "'r2', 'date', 'note', 'forced', 'values': {name: text}, 'files': {path: "
        "<base64>}} -- the .tex, the PDF and any figures. Written by `submit` "
        "(`monoss submit`, `submit_project`), never by hand; the project's "
        "owner mote carries the matching `revision` and `status: submitted`.",
    UNCERTAINTY_KEY: "A given's ±, as a per-property map in exactly the "
        "shape `units` has: {'x': 0.5} is ± 0.5 in x's own unit, {'x': '2%'} "
        "is ± 2% of its value. The value stays a plain number. A result's ± is "
        "never written here -- it is propagated on read (first order, "
        "root-sum-square, givens independent) by `propagate_uncertainty`, and "
        "an entry on a freedom is ignored.",
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
        "scenario applies. A scenario solve never writes to the graph: it "
        "answers what would happen, so it reports the changes it would "
        "make and leaves the motes and their provenance alone.",
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
    {"syntax": "sin, cos, tan, arcsin, arccos, arctan, sinc, sqrt", "meaning":
        "Only on values that are already known. Radians in (a value "
        "declaring unit 'deg' arrives converted) and out. sinc(x) is the "
        "unnormalized sin(x)/x with sinc(0) == 1 -- not numpy's sin(pi x)/"
        "(pi x)."},
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
    {"syntax": "phi(x, ...)", "meaning":
        "A call to the function mote named 'phi' (one carrying a `function` "
        "body): the body evaluated with each parameter bound to its "
        "argument. An unknown may be an argument. A bare number is in the "
        "unit the parameter declares in the function mote's `units`."},
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

# The console's verbs (docs/console.md -> Command line and grammar): the
# first word of a line typed at the `$` prompt. Reserved in a
# different sense from the sets above -- a verb names no property and no
# relationship type, but it *does* shadow a mote of the same name as the
# first token of a line, because the console reads verb-first. A mote
# named `log` still works; it is reached as `"log"` (quoted) or `#<id>`,
# and core/commands.py warns whoever creates one. Each console step adds
# its own verbs here (`find` in U2, ...). Ordered as the completion popup
# lists them, and kept in sync by hand with docs/architecture.md.
RESERVED_VERBS: dict[str, dict[str, str]] = {
    "find": {"usage": "find ‹key:value …› [--sort k] [--group k] [--cols a,b]",
             "describe": "table of motes"},
    "set": {"usage": "set [--scenario ‹s›] @sel|#id|‹name› ‹key› ‹value›|--clear  (or ‹m.k› ‹value›)",
            "describe": "set a property on one mote or the whole selection"},
    "open": {"usage": "open @sel|#id|‹name›",
             "describe": "a mote's properties and links (follows the selection with @sel)"},
    "rename": {"usage": "rename @sel|#id|‹name› ‹old key› ‹new key›",
               "describe": "rename a property, keeping its value and unit"},
    "unlink": {"usage": "unlink @sel|#id|‹name› ‹type› ‹other›|#‹relationship id›",
               "describe": "remove a relationship"},
    "template": {"usage": "template #id|‹name› ‹species›",
                 "describe": "make a mote its species' template"},
    "plot": {"usage": "plot ‹y…› vs ‹x›|t [lo:hi] [by ‹y› lo:hi] [--across] [--steps N] "
                      "[--set ‹m.k›=‹v› [unit]] [--keep] [--scenario ‹s›] [--log] | plot ‹saved› | "
                      "plot save ‹name› | plot export csv|png|svg|pdf|tex [path] | ... --data ‹ykey› vs ‹xkey› [--from ‹import›]",
             "describe": "plot a value against another, re-solving the garden (nothing is written)"},
    "fit": {"usage": "fit ‹m.y› vs ‹m.x› --data ‹ykey› vs ‹xkey› [--from ‹import›] --free ‹m.k›… "
                     "[--scenario ‹s›] [--set ‹m.k›=‹v› [unit]] [--apply|--keep]",
            "describe": "fit inputs to measured points by least squares (--apply writes them)"},
    "sim": {"usage": "sim [--ticks N] [--scenario ‹s›] [--vs ‹s›|garden] [--solver ‹name›] "
                     "[--set ‹m.k›=‹v› [unit]] [--keep] [‹find conditions›]",
            "describe": "run the garden tick by tick and watch its values (nothing is written)"},
    "rule": {"usage": "rule @sel|#id|‹rule›",
             "describe": "a rule's equation and the values it binds (follows the selection with @sel)"},
    "scenarios": {"usage": "scenarios", "describe": "the garden's scenarios and what each overrides"},
    "solve": {"usage": "solve [--live on|off]",
              "describe": "solve the garden once and write it (--live: after every edit)"},
    "why": {"usage": "why ‹m.k›", "describe": "how the garden as it stands determines a value"},
    "step": {"usage": "step +N|-N|‹tick›", "describe": "move the playhead"},
    "play": {"usage": "play", "describe": "play the run from the playhead"},
    "pause": {"usage": "pause", "describe": "stop the playhead"},
    "edit": {"usage": "edit @sel|#id|‹name›",
             "describe": "edit a note's source (a new name makes the note on its first save)"},
    "read": {"usage": "read @sel|#id|‹name›", "describe": "a note, rendered (follows unsaved typing)"},
    "notes": {"usage": "notes [‹text›]", "describe": "every note, or those containing the text"},
    "worksheet": {"usage": "worksheet", "describe": "the garden's calculation as lines of text"},
    "agenda": {"usage": "agenda [‹from›] [‹to›] [--keys a,b] [--type ‹species›] [‹conditions›]",
               "describe": "dated motes, a row per occurrence, by day"},
    "week": {"usage": "week [‹date›] [--keys a,b] [--type ‹species›] [‹conditions›]",
             "describe": "the week as an hour grid"},
    "month": {"usage": "month [‹date›] [--keys a,b] [--type ‹species›] [‹conditions›]",
              "describe": "the month as a six-week grid"},
    "detach": {"usage": "detach #id ‹key› ‹occurrence› ‹start› ‹end›",
               "describe": "move one occurrence out of its series"},
    "graph": {"usage": "graph [@sel|#id|‹name›] [--depth N] [--hide-rel a,b] [--view ‹saved›] [--flow [--set ‹m.k›=‹v› [unit]] [--keep]] [‹conditions›]",
              "describe": "a mote's neighbourhood, or the whole garden, as a graph"},
    "image": {"usage": "image add ‹path› [‹name›] | image rename ‹old› ‹new›",
              "describe": "store a PNG or SVG for notes (\\image{name}), or rename one everywhere it is used"},
    "plan": {"usage": "plan [--days N] [--hours 9-17] [--block 2h] [--from ‹date›] [--apply]",
             "describe": "place work blocks for open motes with an estimate and a due (earliest deadline first); --apply writes them"},
    "worked": {"usage": "worked ‹mote› ‹duration› [‹date›]",
               "describe": "log time actually spent on a mote (90m, 1.5h), as an actual work block"},
    "budget": {"usage": "budget [‹mote›]",
               "describe": "estimate vs. done vs. planned hours, per mote"},
    "export": {"usage": "export tex|pdf [‹path›] [--note ‹name›]",
               "describe": "write the project's document as .tex (with its figure files) or a PDF -- a draft, nothing is frozen"},
    "submit": {"usage": "submit [--force] [--note ‹name›]",
               "describe": "freeze the document as the next revision (PDF, .tex, values); the owner becomes submitted"},
    "revisions": {"usage": "revisions | revisions diff ‹a› ‹b› | revisions export ‹rN› [‹path›] [--tex]",
                  "describe": "frozen submissions: list them, diff their values, write one out"},
    "gardens": {"usage": "gardens [--archived]", "describe": "every garden: the ownership tree, loose and archived"},
    "garden": {"usage": "garden [‹name›] | garden new|rename|rm|restore|archive|unarchive|export|import …",
               "describe": "a garden's details, or manage gardens"},
    "sync": {"usage": "sync | sync now|devices | sync merge|local|share ‹garden› | sync rename ‹garden› ‹new› | sync revoke ‹device›",
             "describe": "every garden's sync state; sync now, answer a name clash, keep a garden local"},
    "project": {"usage": "project start @sel|#id|‹name› [‹garden›] | project stop @sel|#id|‹name›",
                "describe": "give a mote its own garden, or drop its claim"},
    "cd": {"usage": "cd ‹garden›", "describe": "switch this workspace's garden"},
    "layout": {"usage": "layout ‹name› | layout save|load|list|rm ‹name›",
               "describe": "open, save or restore pane layouts (calc, docs, plan, graph, gardens built in)"},
    "refresh": {"usage": "refresh",
                "describe": "re-read the garden (picks up MCP/CLI/sync edits)"},
    "undo": {"usage": "undo", "describe": "undo the last edit"},
    "redo": {"usage": "redo", "describe": "redo"},
    "log": {"usage": "log", "describe": "command and error log"},
}


def reserved_words() -> set[str]:
    """Every word the console reads as its own when it opens a line: the
    verbs above."""
    return set(RESERVED_VERBS)

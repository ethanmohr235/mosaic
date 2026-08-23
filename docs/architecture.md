MonOSS should not be a very complicated software fundamentally.

Store motes and relationships in SQLite database(s). Use Python commands to update those databases.

The user should be able to use a simple UI to view and interact with the graph. The user should also be able to interact with the graph through a CLI.

# Motes

Store motes by immutable ID. Properties of motes should be an extremely flexible datatype that isn’t stored directly in the database.

# Relationships

Store relationships by immutable ID. 

Relationships should have the ability to modify the properties of motes programmatically. This is an advanced feature that should be finished later in development. Consult the human for more advice when implementing the simulation capabilities.

# Gardens

The user should be able to create multiple object graphs that are completely separate from each other. These are called gardens. The one deliberate exception is a **portal**: a mote may carry a reference into another garden so the graph can be navigated there, but nothing about the two gardens' data, simulation, or history otherwise mixes -- see Portals below. A second, different exception is the **library**: a reserved garden holding standalone copies of motes worth reusing elsewhere -- see Reusable motes below.

# Species

A mote can act as a "species" -- a shared type that other motes are declared instances of. A species is still just a mote; nothing marks it as special structurally. Membership is a relationship: a mote is an instance of a species by having an `instance_of` relationship pointing at it. `instance_of` is a reserved relationship type recognized by the engine and UI, the same way any type system needs some minimal way to express "is-a."

Species are flat for now -- a species being an instance of another species is not interpreted as inheritance. A mote may be an instance of more than one species. In v1, species only matter as a way to target rules at every instance of a type at once (e.g. "this rule applies to every instance of Resistor"); enforcing required properties per species (e.g. every instance of Person must have `age` and `phone_number`) is a deferred extension, not built yet.

A species may have a **template**: a mote pointing at it via a `template_of` relationship, whose properties new instances start from. `template_of` is deliberately a different relationship type from `instance_of` -- a template holds placeholder values and belongs to no simulation, so it must not appear among a species' instances.

# Folders

A mote can act as a **folder** -- a purely organizational container that other motes are grouped into, for annotation only. Like a species, a folder is still just a mote; nothing marks it as special structurally. Membership is a relationship: a mote is grouped into a folder by having an `in_folder` relationship pointing at it (`core/folders.py`), the same `member --in_folder--> folder` shape as `instance --instance_of--> species`. Like every other reserved type, it reads as a sentence in the direction it points; it was briefly called `groups`, which read backwards, and was renamed while no garden had used one yet. A mote may belong to more than one folder, and folders are usable in any garden, not just the library (see Reusable motes below).

Folders have no effect on simulation or storage beyond the relationship itself: a folder mote is never touched by `core/simulation.py` unless it's separately declared an `instance_of` a species, and nothing about Calendar export or garden export/import treats a folder or an `in_folder` relationship differently from ordinary data. The one concrete distinction is visual -- the UI renders a folder mote with its own shape (a square, distinct from a species' diamond and an ordinary mote's circle) and offers a "Folders" legend section with the same hide/show behavior species get. The detail panel treats folders as the parallel of species throughout: a "Folders" section listing the folders a mote belongs to, and, on a folder itself, a member count line matching the instance count a species gets.

# Roles

Species, template, folder, property model, rule, portal and note are all the same idea: an ordinary mote that plays a **role** because of what points at it (or, for a portal, because of the reserved properties it carries). Nothing is stored on a mote to say so -- that is what "everything is a mote" costs, and the price is that "what is this?" has to be answered by looking.

`core/roles.py` is where it is answered, once. It is an index, not an implementation: each role's detection stays in the module that owns the concept -- `species_ids_in()`, `template_ids_in()`, `folder_ids_in()`, `model_ids_in()`, `rule_ids_in()` + `has_equation()`, `portal_target()`, `has_modoc()` -- and the `ROLES` table just says which is which, plus the human label every surface shows. A declarative table rather than a providers-style registry: there is no plugin story for roles, and `docs/style.md` rules out the global mutable state a `register_role()` would need.

| role | recognized by |
| --- | --- |
| species | target of `instance_of` or `template_of` |
| template | source of `template_of` |
| folder | target of `in_folder` |
| property model | target of `uses_model` |
| rule | source of `applies_to` **and** carries an `equation` |
| portal | carries `portal_garden` + `portal_mote` |
| note | carries `modoc` |

Two forms, one answer: `roles_in(motes, relationships)` for a caller holding an already-fetched subgraph (each role's id set computed once, then intersected), and each module's own `list_*()` for the same question asked as a query. Roles are not exclusive -- a species can also be a folder, and a `template_of` target counts as a species so that a species you've set up but not yet instantiated still registers as one.

`ui/api.py::focus()` and `library_snapshot()` return this as one `roles` map (mote id -> role names; motes with no role are omitted rather than carrying an empty list). That replaced the `species_ids`/`folder_ids` arrays, which were two of the six roles hardcoded into the payload's shape -- the frontend had no way to hear about the other five, which is why a portal's detail panel said "Mote" while the canvas drew it with a portal ring. Adding a role is now a change to the table, not to a payload.

Adapters must not answer "is this a species" for themselves. That is how a subtlety ends up true in one surface and false in another.

# Simulation

This is the "relationships modify mote properties programmatically" capability described above.

A rule is a mote with an `equation` property and an `applies_to` relationship pointing at a species. The engine runs each rule against every instance of its species. A species declares which of its instances' properties the solver may determine, via an `unknowns` property.

`instance_of`, `template_of`, `applies_to`, `uses_model`, `in_folder` and `about` are reserved relationship types; `equation`, `unknowns`, `url`, `date`, `duration`, `recurrence`, `gcal_event_id`, `provider`, `configuration`, `portal_garden`, `portal_mote` and `modoc` are reserved property names. Everything else is ordinary user data. Reserved property names are still just ordinary, editable/deletable properties -- nothing in the schema enforces them, they're only recognized by convention (the UI/engine looks for that exact key).

# Physical property models

A mote can reference a **property model** -- another mote describing how
one of its physical properties (e.g. a fluid's enthalpy) is determined --
via a `uses_model` relationship. The property-model mote itself carries a
`provider` property (which registered provider implements it, e.g.
`ideal_gas`) and a `configuration` property (whatever that provider needs
to instantiate, e.g. a gas constant). `core/model_resolver.py` turns that
relationship into an executable `PropertyProvider` (`core/providers.py`);
simulation rules never see the provider directly, only the `property()`
builtin described in `docs/simulation.md`. See
`docs/physical_property_proposal` for the full design.

A provider declares `DESCRIPTION`, `CONFIGURATION` and `CAPABILITIES` on
the class, not per instance, so `core/providers.py::describe_providers()`
can read them without constructing one -- which would need the very
provider-specific configuration a caller asking "what providers exist?"
doesn't have. That is what `core/reserved.py`'s provider reference is
built from, and it is why adding a provider or an operation now updates
the reference the UI and MCP clients see without anyone remembering to.
It was a hand-copied table before, one of three places the same facts
were written down.

Equations are written in a small expression language parsed against a strict whitelist -- never `eval`. Evaluating one produces a weighted sum of unknowns plus a constant, so an expression that happens to contain no unknowns is simply a number. That is why there is only one kind of rule: an assignment and a coupled equation are the same thing to the solver.

Solving happens in two stages. A substitution loop repeatedly resolves any equation that has come down to a single unknown and feeds the answer back, which allows nonlinear formulas as long as the nonlinear part involves values already known. Whatever remains must be linear and goes to a single least-squares solve.

`core.simulation.run_time_series()` is the only solve-and-write path there is: `run_simulation()` is that function called with one step and `commit=True`, unpacked into a single result. The two used to be parallel implementations of the same thing, which is how "a steady-state run is a one-step time series" could be true in the documentation and only approximately true in the code.

A run can be time-based: `run_time_series()` solves a requested number of discrete steps, each one starting from the *previous* step's solved values rather than the database, so a rule can use the reserved `t` (the current step index) or `prev(x)` (the value `x` had entering the step) to express time-dependent behavior or a recurrence. This is computed entirely in memory and nondestructive by default -- nothing is written, and nothing is logged -- until a specific step is explicitly committed, which re-solves up to that step (deterministic, given the same garden and rules) and writes only the cumulative result in one shot. A single steady-state `run_simulation()` call is equivalent to committing a 1-step time series.

Every logged run (failures included) is recorded in `simulation_runs` and `simulation_changes` -- a discarded preview leaves no trace, but a *saved* run (kept for review without being written to the graph -- see `docs/simulation.md`) is logged just like a committed one. The per-garden run counter (`run_number`) is derived from that log, and is a different thing from a time-based run's own step numbering (which resets to 0 every run and is never persisted on its own -- each of a run's own steps/ticks is logged individually now, so it can be scrubbed back through later). See `docs/simulation.md` for the language reference and what is deferred.

# MCP server

`mcp_server/` is a third adapter over `core/`, alongside `cli/` and
`ui/`: it exposes the same repository functions as tools an MCP client
can call (`run_mcp.py`), over stdio by default or streamable HTTP with
`--http`. It adds no logic of its own -- every tool is a thin
translation between an MCP call and the function the CLI or UI already
calls.

By default nothing here authenticates. Over stdio that is fine -- the
client starts the process and talks to it over a pipe -- and the HTTP
transport binds loopback for the same reason, meant to be reached over a
private network (see `docs/mcp.md`), not published. DNS-rebinding
protection is on so a browser page can't drive the loopback port.

`run_mcp.py --http --public-url <url>` turns on OAuth (`mcp_server/auth.py`)
for clients that require it protocol-level rather than accepting a plain
connection -- Claude Desktop's remote-connector UI does, Claude Code's
`mcp add --transport http` doesn't. `authorize()` grants every request
immediately, with no login or consent step: there's no second party on
this server to protect the resource from, so the token exists to satisfy
the client's requirement, not as an access boundary beyond the one the
network already provides (Tailscale, `--allowed-host`). Every client
registration and token lives in memory and is forgotten on restart.

The package is deliberately not named `mcp/`: `run_mcp.py` puts the
project root on `sys.path`, so a top-level package by that name would
shadow the installed `mcp` SDK.

Where `ui/api.py` holds one long-lived connection for the open garden,
every tool here opens and closes its own, and takes the garden as an
optional argument (defaulting to `default`, like the CLI). The server is
a separate process from the GUI, so holding a connection open for its
whole life would mean sitting on a SQLite lock while the user works in
the app. Those connections set `busy_timeout`, so a write that collides
with the GUI's write transaction waits it out instead of failing --
that pragma is set in the adapter only, leaving the CLI and UI
untouched. The GUI does not observe external changes until it refetches.

The surface is reads plus graph edits: motes, relationships, species,
templates and instances, folders, the reusable-motes library,
neighborhood queries, and *previews* of simulation runs. Applying a
simulation to the graph, deleting or restoring gardens, calendar export,
and opening links are deliberately left out -- they either write results
the user should approve in the app, or need a live window. See
`docs/mcp.md` for setup.

# What belongs on which surface

`core/` is the whole system. `cli/`, `ui/`, `mora/` and `mcp_server/`
are four adapters over it, and each one's job is a different question, so none of
them is expected to expose everything. What is *not* acceptable is a gap
with no reason behind it -- which is what the surfaces had accumulated:
folders and the library existed only in the app, property models only in
the CLI, templates only in MCP, not by decision but by whichever surface
happened to be open when each was built.

The charter:

* **core** -- complete. Every capability lives here, and an adapter that
  holds logic of its own is a bug (see Calendar export, moved out of
  `ui/api.py` for exactly this reason).
* **CLI** -- everything scriptable. If it can be done without a window
  and without a human approving a result, it has a command.
* **UI** -- everything interactive. Anything a person would reach for
  while looking at a graph, including the things only a live window can
  do: opening links, exporting to Calendar, approving a simulation.
* **Mora** -- the same charter as the UI, for one thing: writing and
  reading .modoc notes (see Notes below). A second window rather than a
  fourth kind of adapter -- it is the UI's charter applied to a surface
  a graph canvas has no room for.
* **MCP** -- reads plus graph edits (above). Deliberately not: writing a
  simulation to the graph, deleting or restoring gardens, Calendar
  export, opening links.

A capability missing from a surface its charter covers is a gap to
close, not a decision. A capability missing from a surface its charter
excludes should say so, here, in one line.

# Links and external export

`url` (`core/links.py`) makes a mote openable -- a web address or a local
file path, launched via the OS's default handler. See `docs/ui-setup.md`.

`date` and `duration` (`core/google_calendar.py`) mark a mote as something
with a time: `date` is ISO 8601 (a bare date for an all-day event, a full
datetime for a timed one), `duration` is a plain number (minutes for a
timed event, days for an all-day one). `recurrence` is a raw RFC 5545
RRULE string (e.g. `FREQ=WEEKLY;BYDAY=TU,TH`), passed straight through to
Google rather than parsed or validated locally -- one Calendar event with
a rule represents the whole series, so this needed no other change.
Removing the property is itself exported: the next export explicitly
clears recurrence on the existing event rather than leaving the old rule
in place.
`gcal_event_id` gets written back onto a mote the first time it's
exported to Google Calendar, so re-running the export updates that event
(the whole series, if recurring) instead of duplicating it. Every export
also deletes any previously-created event whose mote is gone or no longer
dated -- found by querying Calendar itself for everything tagged as
MonOSS-managed (`extendedProperties.private` on each event), not a local
record, so this stays correct even if events were edited or deleted by
hand. See `docs/google-calendar.md` for setup and details.

The export loop itself is `core/google_calendar.py::export_events()`, not
the UI's: an adapter holding business logic is an adapter no other
surface can reuse and no test can reach without a GUI. What `ui/api.py`
still supplies is the two things only the app knows -- `record_event_id`
(how to write a new event id back, through its connection lock) and
`still_exporting` (whether the user has switched gardens while a network
call was in flight, which must never write an id into the wrong garden).
Exposing the export over CLI or MCP is now a question of whether it
should be, rather than of rewriting it.

# Portals

`portal_garden` and `portal_mote` (`core/portals.py`) turn an ordinary
mote into a one-way link into a specific mote in a different garden.
Both are flat reserved properties, not a relationship --
`relationships.to_id` has a foreign key into the *same* connection's
`motes` table (`core/db.py`), so a cross-garden reference can't be one.
Creating a portal does not modify anything in the target garden or
create a reciprocal link back; following a portal is UI/MCP navigation,
not a live coupling -- the portal mote's own `name` (if any) is a
one-time snapshot of the target's label at creation time, not kept in
sync afterward.

Portals are navigation-only for v1: `targets()`/`sources()` and the
`property()` builtin never see through one, and a rule's `equation`
cannot read a value across a portal. There is currently no cross-garden
mechanism anywhere in `core/simulation.py`; extending the solver to
resolve properties across gardens would be a substantially larger,
separate feature.

# Reusable motes

`core/library.py` lets a mote worth reusing (a person, a recurring
meeting shape, a standard task) be saved once and dropped into any
garden later. Unlike a portal, this is not a live reference: saving a
mote copies its properties into a reserved garden, `_library`, exactly
like any other garden underneath (same `gardens/<name>.db` file, same
motes/relationships schema). Reusing a library entry copies its
properties into a brand-new mote in the current garden -- a fresh,
independent snapshot, the same way `core/species.py::create_instance()`
seeds a new instance from a template. Nothing links the two afterward;
editing either side never affects the other.

Saving and reusing are both `core/motes.py::copy_mote()` -- a mote's
properties into a new, independent mote, in the same garden or a
different one. That primitive is also what `create_instance()` does from
a species' template, so seeding a new mote from an existing one is one
function rather than a read-then-create written out wherever it's needed.
What `core/library.py` keeps is what actually belongs to the library:
which garden it is, and bringing it into being lazily.

Only a mote's properties are saved -- its relationships are never
captured or restored. Saving a whole subgraph (a mote plus its
relationships to other library entries) is a deferred, larger feature:
it needs a way to decide where a subgraph's boundary is, and what to do
with a relationship pointing at a mote outside it.

`_library` never shows up as one of your everyday workspaces -- it is a
**reserved garden** (`core/db.py::is_reserved_garden`: any name starting
with an underscore), which `core/gardens.py` refuses to create by hand
and leaves out of `list_gardens()` unless explicitly asked with
`include_reserved=True`. That rule lives in core rather than in one
adapter, so the UI's garden switcher, the portal picker's dropdown, the
CLI's `garden list` and the MCP `list_gardens` tool all show the same
set; previously only the UI filtered it, and the other two offered the
library as an ordinary garden you could switch into or collide with.
Reserved governs discovery and creation, not access: it's still a real,
fully editable garden underneath, reachable directly via the garden
menu's "Open library" action or by naming it explicitly. That
makes it worth organizing like any other garden: creating folder motes
inside it and grouping related library entries together (see Folders
above) is the intended way to keep a large library navigable, and the
"Reuse from library…" picker filters by that same folder/species
grouping alongside free-text search.

This first pass covers `core/` and the UI (`ui/api.py`,
`ui/frontend/`) only; CLI and MCP exposure are deferred to a follow-up.

# Notes

`modoc` (`core/modoc.py`) holds a **.modoc note**: prose written *about*
the graph, in a small LaTeX subset whose three MonOSS macros --
`\mote{...}`, `\val{...}`, `\graph{...}` -- resolve against the garden
every time the note is read. A due date or a task count in a note is a
lookup, not a copy, so a note cannot go stale the way a typed-in number
does. See `docs/modoc.md` for the language.

Storage is one reserved property on an ordinary mote, and that is the
whole design. A note is not a new object type; a mote carrying one plays
the `note` role (Roles above) exactly as a mote carrying an `equation`
plays `rule`. The consequence worth stating is what nobody had to build:
notes are inside `mote_history`, garden export/import, the UI's undo, and
MCP's `update_mote` already, because they are properties. `.modoc` files
exist only for import and export, and a file is exactly the property's
string -- no header naming the garden it came from, which was the first
design and would have cost the round trip its byte-for-byte identity for
a line meaningful to one program.

A note reads from its own mote by default. `about` (a reserved
relationship type, `note --about--> subject`) moves that anchor, so a
note can live on its own mote while speaking about another. Exactly one
`about` moves it; several is ambiguous and falls back to the note itself
with a diagnostic, rather than picking one.

`\val` is deliberately not a second expression language. It hands its
argument to `core/formula.py` -- the one rules use -- so `targets()`,
`sum()`, `prev()` and `t` are all available in a note without being
implemented twice. The one thing in front of it is a plain-property fast
path: `core/formula.py` is numeric by construction, so `\val{due}` has to
be read as a property before an evaluator that can only return numbers
sees it. `core/simulation.py::expression_context()` builds the context,
clearing the view's declared unknowns first -- a note *reads state*, and
a free variable with no solver behind it is useless rather than correct.

Stepping a note forward in time is `run_time_series(commit=False)`, the
preview that already existed: `t+0` is the garden as stored, `t+N`
resolves every macro against a projection N steps out, and nothing is
written or logged either way. Turning a projection into fact stays a
separate, explicit act (Mora's `Apply t+N…`, which asks first). Saving a
note and applying a run are two buttons on purpose: a save must never be
a way to write simulation results.

Nothing in the note path raises. A note with a typo still renders with
the broken part visibly broken, and every failure -- unknown command,
unresolved reference, unevaluable expression -- becomes a `Diagnostic`.
What counts as a problem is answered once, in
`core/modoc_bind.py::problems()`, for the same reason `core/roles.py`
exists: it was briefly the CLI's own, and Mora promptly reported a note
with a dangling `\mote{...}` in it as having no problems at all.

`mora/` is the editor (`run_mora.py`) -- a second pywebview window with
its own garden connection, source/rendered/data panes, and the `t+N`
stepper. It parses nothing itself: the rendered pane is built from the
tree `core/` produced and `mora/payload.py` shaped, which is why the
window and `monoss modoc render` cannot come to disagree about what a
note means. Only the source pane's syntax highlighting runs in
JavaScript, colour being presentation rather than meaning.

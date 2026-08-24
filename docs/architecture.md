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

# Kinds

Species, template, folder, property model, rule and portal are all the same idea: an ordinary mote that *is* one of these -- a **kind** -- because of what points at it (or, for a portal, because of the reserved properties it carries). Nothing is stored on a mote to say so -- that is what "everything is a mote" costs, and the price is that "what is this?" has to be answered by looking.

Called kinds and not roles, which is what this was: `core/roles.py` answers a different question -- what an assembly's parts are *called* (see Roles below). **A kind is what a mote is; a role is what something else calls it.** A kind is derived and never stored; a role is a declared property. The frontend was already using this word (`kindLabel`, `kindOf`), so the rename made existing names correct rather than inventing one.

`core/kinds.py` is where it is answered, once. It is an index, not an implementation: each kind's detection stays in the module that owns the concept -- `species_ids_in()`, `template_ids_in()`, `folder_ids_in()`, `model_ids_in()`, `rule_ids_in()` + `has_equation()`, `portal_target()` -- and the `KINDS` table just says which is which, plus the human label every surface shows. A declarative table rather than a providers-style registry: there is no plugin story for kinds, and `docs/style.md` rules out the global mutable state a `register_kind()` would need.

| kind | recognized by |
| --- | --- |
| species | target of `instance_of` or `template_of` |
| template | source of `template_of` |
| folder | target of `in_folder` |
| property model | target of `uses_model` |
| rule | source of `applies_to` **and** carries an `equation` |
| portal | carries `portal_garden` + `portal_mote` |

Two forms, one answer: `kinds_in(motes, relationships)` for a caller holding an already-fetched subgraph (each kind's id set computed once, then intersected), and each module's own `list_*()` for the same question asked as a query. Kinds are not exclusive -- a species can also be a folder, and a `template_of` target counts as a species so that a species you've set up but not yet instantiated still registers as one.

`ui/api.py::focus()` and `library_snapshot()` return this as one `kinds` map (mote id -> kind names; motes of no particular kind are omitted rather than carrying an empty list). That replaced the `species_ids`/`folder_ids` arrays, which were two of the six kinds hardcoded into the payload's shape -- the frontend had no way to hear about the other four, which is why a portal's detail panel said "Mote" while the canvas drew it with a portal ring. Adding a kind is now a change to the table, not to a payload.

Adapters must not answer "is this a species" for themselves. That is how a subtlety ends up true in one surface and false in another.

# Simulation

This is the "relationships modify mote properties programmatically" capability described above.

A rule is a mote with an `equation` property and an `applies_to` relationship. What that relationship points at decides the rule's reach, inferred rather than declared: a species means once per instance, any other mote means once about that mote (see "Roles" below).

Which of a mote's properties the solver may determine -- its **freedoms** -- comes from a `freedoms` property. On a mote acting as a species it declares what every instance is free to have determined; on any other mote it declares what that mote itself is free to have determined, and a mote's own declaration replaces its species' rather than adding to it. That is what lets two instances of one species be asked opposite questions -- sizing and rating from the same equation -- without editing what the species *is*.

`freedoms` was called `unknowns` while it could only live on a species; gardens are migrated on open. `freedoms` is the modelling word, and the solver's own vocabulary is deliberately left alone: `core/formula.py`'s `Unknown` and both solver backends still say unknown, because at that layer it is correct. `core/simulation.py`'s `_context()` is the single translation point.

`instance_of`, `template_of`, `applies_to`, `uses_model`, `overrides`, `varies`, `in_folder` and `about` are reserved relationship types; `equation`, `freedoms`, `roles`, `url`, `date`, `duration`, `recurrence`, `gcal_event_id`, `provider`, `configuration`, `units`, `portal_garden`, `portal_mote` and `modoc` are reserved property names. Everything else is ordinary user data. Reserved property names are still just ordinary, editable/deletable properties -- nothing in the schema enforces them, they're only recognized by convention (the UI/engine looks for that exact key).

# Units

A numerical property can optionally declare what it measures, via a
mote's `units` property -- a map from property key to unit name
(`{"resistance": "kohm"}`). `core/units.py` holds an explicitly
registered catalog of units, each carrying an SI dimension vector, a
scale, and (for `degC`/`degF`) an offset; compound names like `m/s` and
`J/(kg*K)` are parsed by a small dedicated tokenizer rather than
evaluated, the same discipline equations follow.

Units are opt-in per property and declared per mote, not per species --
two instances of one species may legitimately track the same quantity in
different units. That is what the solver reconciles: a declared value is
converted to base SI on the way into a formula (`core/formula.py`'s
`_lookup`) and back to the mote's own declared unit on the way out
(`core/simulation.py`'s write-back), so mixed units combine correctly
and solving never silently changes the unit a property is recorded in. A
garden that declares no units takes exactly the path it always has.

`core/dimensions.py` walks the same expression grammar to check an
equation's dimensional consistency, reusing `core/formula.py`'s
call-name whitelist rather than duplicating it -- that whitelist is the
security boundary, and two copies could drift. Mismatches are advisory
by default (`SimulationResult.dimension_warnings`); `strict_units=True`
promotes them to a failure. See `docs/simulation.md` for the reference
and `docs/units_proposal` for the design.

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

Solving happens in two stages. A substitution loop repeatedly resolves any equation that has come down to a single unknown and feeds the answer back, which allows nonlinear formulas as long as the nonlinear part involves values already known. Whatever remains -- a still-linear system of several unknowns, or an equation genuinely nonlinear in its unknowns -- goes to a solver backend chosen per run via `solver`/`solver_configuration` (`core/solvers.py`), the same pluggable-by-name pattern physical property models use for `provider`/`configuration`. The default, `linear`, is an exact single least-squares solve and fails on genuine nonlinearity; `newton` solves that iteratively instead. See `docs/simulation.md` for the full solver reference.

`core.simulation.run_time_series()` is the only solve-and-write path there is: `run_simulation()` is that function called with one step and `commit=True`, unpacked into a single result. The two used to be parallel implementations of the same thing, which is how "a steady-state run is a one-step time series" could be true in the documentation and only approximately true in the code.

A run can be time-based: `run_time_series()` solves a requested number of discrete steps, each one starting from the *previous* step's solved values rather than the database, so a rule can use the reserved `t` (the current step index) or `prev(x)` (the value `x` had entering the step) to express time-dependent behavior or a recurrence. This is computed entirely in memory and nondestructive by default -- nothing is written, and nothing is logged -- until a specific step is explicitly committed, which re-solves up to that step (deterministic, given the same garden and rules) and writes only the cumulative result in one shot. A single steady-state `run_simulation()` call is equivalent to committing a 1-step time series.

Every logged run (failures included) is recorded in `simulation_runs` and `simulation_changes` -- a discarded preview leaves no trace, but a *saved* run (kept for review without being written to the graph -- see `docs/simulation.md`) is logged just like a committed one. The per-garden run counter (`run_number`) is derived from that log, and is a different thing from a time-based run's own step numbering (which resets to 0 every run and is never persisted on its own -- each of a run's own steps/ticks is logged individually now, so it can be scrubbed back through later). See `docs/simulation.md` for the language reference and what is deferred.

# Comparison relations

A rule may use `<=`, `>=`, `<` or `>` instead of `==`, which makes it a
**constraint**: checked against the answer rather than solved for. This
cost one change in `core/formula.py`'s parser and no second expression
language, because every relation already evaluated to `lhs - rhs <op> 0`
from one walk of one grammar -- the operator only decides what is done
with the result.

`core/simulation.py` sorts rules into equations and constraints before
solving, so no solver backend ever receives a constraint. That is the
structural form of "an inequality cannot determine a value", and it is
also why `core/incidence.py` never lists a constraint among a value's
producers.

Violations are reported on the result as `constraint_violations`, kept
separate from `dimension_warnings`: one is a statement about the answer,
the other about the model. Advisory by default -- an answer that breaks a
limit is often what the run was for -- with `strict_constraints`
promoting them to a failure, mirroring `strict_units`. A constraint
depending on something the run could not determine is reported as *not
checked*, never as passing.

# Scenarios

`core/scenarios.py` lets one garden hold more than one answer. An
**override** mote holds replacement properties for one specific mote
(`overrides`), and a **scenario** mote gathers overrides (`varies`). The
shape deliberately mirrors templates one arrow over -- `template_of :
species :: overrides : mote` -- and an override is excluded from runs for
the same reason a template is excluded from a species' instances: it
holds replacement values, not state.

That exclusion applies to **every** run, not only scenario runs. An
override declaring `freedoms` would otherwise become solvable in its own
right, and its values would be read as some mote's real data.

`apply()` is pure, and that is why scenarios cost so little: the run
machinery was already driven from a properties dict rather than from the
database (`_build_view` has been pure since units), so a scenario is one
pure function inserted ahead of another.

A scenario run **never writes to the graph**. `run_simulation` with a
scenario logs an uncommitted run recording its `scenario_id` and reports
what would change; `run_time_series` refuses `commit=True` outright,
since committing is an explicit request to make something the garden's
state. The answer to a hypothetical is not state, and there is no good
answer to where a scenario run's results would be written.

An override may carry `freedoms`, which changes the question rather than
the inputs -- sizing becomes rating. That is what made moving freedoms
off the species (see "Simulation" above) a prerequisite rather than a
convenience: a scenario perturbs motes, so anything it varies must be
declarable on a mote.

See `docs/simulation.md` for the reference.

# Roles

An assembly's parts are named by `core/roles.py`. A role is **not a new
kind of object**: it is an ordinary relationship whose *type* is the
role's name, so `Rankine Cycle #1 --turbine--> HP Turbine` needs no
binding table and nests for free. Rules anchored on an assembly have
always been able to reach and solve values on other motes -- the voltage
divider does exactly that -- and a role adds the *declaration*: a
`roles` property naming what an instance should be wired to.

That buys three things the bare graph cannot give: a formula can address
a part by name (`turbine.h_in == boiler.h_out`), arity is a stated
intention rather than an accident of how many edges happen to exist
(`many` never collapses to a scalar, so `sum(...)` is required from the
first part rather than from the second), and `check_roles` catches a
mis-wired assembly before it is solved.

`roles` on a mote acting as a species declares what its *instances* have;
on any other mote it declares what that mote itself has -- which is what
lets a one-off assembly name its parts without inventing a species with
one instance. `freedoms` is read exactly the same way, so a mote never
has to work out which of its declaration properties are about it and
which are about its instances: one rule covers both.

Roles resolve only in the attribute position, so a bare name is still
always a property and nothing changed meaning: reading an attribute off
anything but `targets()`/`sources()` was already an error.
`core/dimensions.py` resolves them identically to `core/formula.py`,
through the shared rules in `core/roles.py` -- the two walks must agree
about roles exactly as they already agree about the call whitelist.

What `applies_to` points at decides a rule's reach, inferred rather than
declared: a species means once per instance, any other mote means once
about that mote. `Rule.scope` carries the resolved answer and
`check_roles` lists every `once` rule, because making the first instance
of a mote silently converts every rule asserted about it.

See `docs/simulation.md` for the reference and
`scripts/example_rankine_cycle.py` for a worked cycle.

# Provenance

Every logged run also records *why* each value came out as it did:
which equations, reading which already-known values, determined it.
`core/provenance.py` collects this during the solve and `core/derivations.py`
persists and replays it, backing the `why` command in both CLI surfaces
and the MCP server.

The recording is pure and DB-free, and -- importantly -- it does not touch
`core/formula.py`. Reads are captured by handing the evaluator a dict
that records what comes out of it, the technique `core/simulation.py`'s
`_RecordingKnown` already used for a different purpose. The evaluator's
node whitelist is the security boundary and stays a flat dispatch with
nothing woven through it.

A derivation is a **block**: the equations solved together, the values
they jointly determined, and the inputs that went in. That one shape
covers both stages of a solve, because the substitution loop is simply
the case where a block holds one equation and one value. What a solver
backend returns cannot be attributed any more finely than "these
equations determined these values together", and the record says so
rather than implying a tree. Recorded values are in base SI units --
what the solver worked in -- unlike the change log, which converts back
into each mote's declared unit.

A discarded preview records nothing (committing re-solves, and that
solve is recorded); a saved run records derivations without touching the
graph; a failed run records neither, exactly as it already recorded no
changes.

# Dependencies

`core/incidence.py` holds the bipartite relation between equations and
the values they touch, backing `impact`, `requires`, `producers` and
`consumers` in both CLI surfaces and the MCP server.

It is **derived from the rules on every query, never stored.** The
equations are the single source of truth about what depends on what, and
a persisted index would be a second account of them that could drift --
the same call `core/dimensions.py` made in reusing `core/formula.py`'s
whitelist rather than copying it.

The relation is bipartite because MonOSS solves constraint systems. An
equation has no inputs and outputs: `V == I * R` determines V in one
garden and I in another, from the same text, according to which values a
species declares free. So `producers` returns every equation that could
determine a value, and an empty result means nothing can -- the most
common modelling mistake, and one that is otherwise silent.

`core/incidence.py` is graph math only: it imports no parser and walks no
expression. The touch sets it works on are produced by
`core/simulation.py`'s `_probe`, using the same evaluator that solves,
which is what makes "one interpretation of the model" structural rather
than a promise. `tests/test_incidence.py` asserts both halves of that.

The probe reads permissively -- a properties dict that answers for keys
it does not have -- so that a *missing* property, which `core/formula.py`
raises on before any read is recorded, still shows up as the thing
`requires` most needs to report. That is safe only because nothing there
is solving; while solving, a missing property is an error, and answering
for it would produce a confident wrong number instead of a failure.

See `docs/simulation.md` for the reference and `docs/modeling_proposal`
for the design and what follows it.

# The Observatory

`ui/` is the desktop app, and like `cli/` and `mcp_server/` it is an
adapter over `core/` -- `ui/api.py` adds shaping for the JS bridge and no
logic of its own. Everything the engine gained is reachable from it:
`why` and the dependency queries as a per-property panel in the
inspector, a scenario picker beside the solver picker, unit and
constraint advisories on the sim bar, and a roles check in Tools.

Two shaping decisions worth knowing. The three dependency queries are one
`dependencies()` call, because the index is rebuilt per call by design
and the panel shows them together. And a derivation's numbers reach the
GUI in base SI units while the property rows above them show each mote's
declared unit -- the panel says which it is showing, since this is the
only place the two representations are visible at once.

Roles, freedoms and scenarios needed no bespoke editors: they are
ordinary properties and relationships, so the existing property editor
and the reserved-name badge already cover them. That is the monism paying
for itself -- every abstraction added in `docs/modeling_proposal` arrived
in the GUI as data the app already knew how to display.

See `docs/ui-setup.md` for the interaction reference.

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
whole design. A note is not a new object type, and carrying one is
deliberately **not** a kind (Kinds above): every entry in that table is a
job a mote does in the graph's machinery -- a rule solves, a portal
navigates -- and `modoc` changes nothing about how a mote is treated. It
is data, exactly like `url` and `date`, neither of which is a kind
either. This was briefly got wrong, and the cost was immediate and
visible: because a kind displaces the species in the detail panel's type
label, a project that gained a note stopped reading as a project. The
consequence worth stating is what nobody had to build:
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
`core/modoc_bind.py::problems()`, for the same reason `core/kinds.py`
exists: it was briefly the CLI's own, and Mora promptly reported a note
with a dangling `\mote{...}` in it as having no problems at all.

`mora/` is the editor (`run_mora.py`) -- a second pywebview window with
its own garden connection, source/rendered/data panes, and the `t+N`
stepper. It parses nothing itself: the rendered pane is built from the
tree `core/` produced and `mora/payload.py` shaped, which is why the
window and `monoss modoc render` cannot come to disagree about what a
note means. Only the source pane's syntax highlighting runs in
JavaScript, colour being presentation rather than meaning.

The graph window reaches it through `ui/api.py::open_mora`, which spawns
`run_mora.py` detached under `sys.executable` -- a sibling process, not a
child window, so neither app's lifetime is tied to the other's. That
makes two processes on one garden file the normal case rather than the
exception, which is why `mora/api.py` sets `busy_timeout` on its
connection for the same reason `mcp_server/server.py` does. `core/db.py`
is deliberately left alone, so the CLI and the graph window keep the
behavior they have.

Two apps at once also means the two must not want the same HTTP port or
the same WebKit storage directory, and the port is the one with teeth:
pywebview pins its server to 42001 unless told otherwise, and the
process that loses that race does not fail visibly -- it points its
window at the winner's address and loads the other app's page against
its own bridge. `mora/app.py::choose_port` asks for 42002 and falls back
to an ephemeral port; `.webview_data_mora/` is its own storage. See
`docs/ui-setup.md`.

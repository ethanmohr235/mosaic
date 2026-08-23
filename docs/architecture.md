MonOSS should not be a very complicated software fundamentally.

Store motes and relationships in SQLite database(s). Use Python commands to update those databases.

The user should be able to use a simple UI to view and interact with the graph. The user should also be able to interact with the graph through a CLI.

# Motes

Store motes by immutable ID. Properties of motes should be an extremely flexible datatype that isn’t stored directly in the database.

# Relationships

Store relationships by immutable ID. 

Relationships should have the ability to modify the properties of motes programmatically. This is an advanced feature that should be finished later in development. Consult the human for more advice when implementing the simulation capabilities.

# Gardens

The user should be able to create multiple object graphs that are completely separate from each other. These are called gardens.

# Species

A mote can act as a "species" -- a shared type that other motes are declared instances of. A species is still just a mote; nothing marks it as special structurally. Membership is a relationship: a mote is an instance of a species by having an `instance_of` relationship pointing at it. `instance_of` is a reserved relationship type recognized by the engine and UI, the same way any type system needs some minimal way to express "is-a."

Species are flat for now -- a species being an instance of another species is not interpreted as inheritance. A mote may be an instance of more than one species. In v1, species only matter as a way to target rules at every instance of a type at once (e.g. "this rule applies to every instance of Resistor"); enforcing required properties per species (e.g. every instance of Person must have `age` and `phone_number`) is a deferred extension, not built yet.

A species may have a **template**: a mote pointing at it via a `template_of` relationship, whose properties new instances start from. `template_of` is deliberately a different relationship type from `instance_of` -- a template holds placeholder values and belongs to no simulation, so it must not appear among a species' instances.

# Simulation

This is the "relationships modify mote properties programmatically" capability described above.

A rule is a mote with an `equation` property and an `applies_to` relationship. What that relationship points at decides the rule's reach, inferred rather than declared: a species means once per instance, any other mote means once about that mote (see "Roles" below).

Which of a mote's properties the solver may determine -- its **freedoms** -- comes from a `freedoms` property. On a mote acting as a species it declares what every instance is free to have determined; on any other mote it declares what that mote itself is free to have determined, and a mote's own declaration replaces its species' rather than adding to it. That is what lets two instances of one species be asked opposite questions -- sizing and rating from the same equation -- without editing what the species *is*.

`freedoms` was called `unknowns` while it could only live on a species; gardens are migrated on open. `freedoms` is the modelling word, and the solver's own vocabulary is deliberately left alone: `core/formula.py`'s `Unknown` and both solver backends still say unknown, because at that layer it is correct. `core/simulation.py`'s `_context()` is the single translation point.

`instance_of`, `template_of`, `applies_to`, `uses_model`, `overrides` and `varies` are reserved relationship types; `equation`, `freedoms`, `roles`, `url`, `date`, `duration`, `recurrence`, `gcal_event_id`, `provider`, `configuration` and `units` are reserved property names. Everything else is ordinary user data. Reserved property names are still just ordinary, editable/deletable properties -- nothing in the schema enforces them, they're only recognized by convention (the UI/engine looks for that exact key).

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

Equations are written in a small expression language parsed against a strict whitelist -- never `eval`. Evaluating one produces a weighted sum of unknowns plus a constant, so an expression that happens to contain no unknowns is simply a number. That is why there is only one kind of rule: an assignment and a coupled equation are the same thing to the solver.

Solving happens in two stages. A substitution loop repeatedly resolves any equation that has come down to a single unknown and feeds the answer back, which allows nonlinear formulas as long as the nonlinear part involves values already known. Whatever remains -- a still-linear system of several unknowns, or an equation genuinely nonlinear in its unknowns -- goes to a solver backend chosen per run via `solver`/`solver_configuration` (`core/solvers.py`), the same pluggable-by-name pattern physical property models use for `provider`/`configuration`. The default, `linear`, is an exact single least-squares solve and fails on genuine nonlinearity; `newton` solves that iteratively instead. See `docs/simulation.md` for the full solver reference.

A run can be time-based: `core.simulation.run_time_series()` solves a requested number of discrete steps, each one starting from the *previous* step's solved values rather than the database, so a rule can use the reserved `t` (the current step index) or `prev(x)` (the value `x` had entering the step) to express time-dependent behavior or a recurrence. This is computed entirely in memory and nondestructive by default -- nothing is written, and nothing is logged -- until a specific step is explicitly committed, which re-solves up to that step (deterministic, given the same garden and rules) and writes only the cumulative result in one shot. A single steady-state `run_simulation()` call is equivalent to committing a 1-step time series.

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

Nothing here authenticates. Over stdio that is fine -- the client starts
the process and talks to it over a pipe -- but the HTTP transport binds
loopback for that reason, and is meant to be reached over a private
network (see `docs/mcp.md`), not published. DNS-rebinding protection is
on so a browser page can't drive the loopback port.

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
templates and instances, neighborhood queries, and *previews* of
simulation runs. Applying a simulation to the graph, deleting or
restoring gardens, calendar export, and opening links are deliberately
left out -- they either write results the user should approve in the
app, or need a live window. See `docs/mcp.md` for setup.

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
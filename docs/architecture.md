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

# Kinds

Species, template, property model and rule are all the same idea: an ordinary mote that *is* one of these -- a **kind** -- because of what points at it. Nothing is stored on a mote to say so -- that is what "everything is a mote" costs, and the price is that "what is this?" has to be answered by looking.

Called kinds and not roles, which is what this was: `core/roles.py` answers a different question -- what an assembly's parts are *called* (see Roles below). **A kind is what a mote is; a role is what something else calls it.** A kind is derived and never stored; a role is a declared property. The frontend was already using this word (`kindLabel`, `kindOf`), so the rename made existing names correct rather than inventing one.

`core/kinds.py` is where it is answered, once. It is an index, not an implementation: each kind's detection stays in the module that owns the concept -- `species_ids_in()`, `template_ids_in()`, `model_ids_in()`, `rule_ids_in()` + `has_equation()` -- and the `KINDS` table just says which is which, plus the human label every surface shows. A declarative table rather than a providers-style registry: there is no plugin story for kinds, and `docs/style.md` rules out the global mutable state a `register_kind()` would need.

| kind | recognized by |
| --- | --- |
| species | target of `instance_of` or `template_of` |
| template | source of `template_of` |
| property model | target of `uses_model` |
| rule | source of `applies_to` **and** carries an `equation` |

Two forms, one answer: `kinds_in(motes, relationships)` for a caller holding an already-fetched subgraph (each kind's id set computed once, then intersected), and each module's own `list_*()` for the same question asked as a query. Kinds are not exclusive -- a `template_of` target counts as a species so that a species you've set up but not yet instantiated still registers as one.

`monarch/api.py::focus()` returns this as one `kinds` map (mote id -> kind names; motes of no particular kind are omitted rather than carrying an empty list). That replaced the `species_ids` array, one kind hardcoded into the payload's shape -- the frontend had no way to hear about the others. Adding a kind is now a change to the table, not to a payload.

Adapters must not answer "is this a species" for themselves. That is how a subtlety ends up true in one surface and false in another.

# Simulation

This is the "relationships modify mote properties programmatically" capability described above.

A rule is a mote with an `equation` property and an `applies_to` relationship. What that relationship points at decides the rule's reach, inferred rather than declared: a species means once per instance, any other mote means once about that mote (see "Roles" below).

Which of a mote's properties the solver may determine -- its **freedoms** -- comes from a `freedoms` property. On a mote acting as a species it declares what every instance is free to have determined; on any other mote it declares what that mote itself is free to have determined, and a mote's own declaration replaces its species' rather than adding to it. That is what lets two instances of one species be asked opposite questions -- sizing and rating from the same equation -- without editing what the species *is*.

`freedoms` was called `unknowns` while it could only live on a species; gardens are migrated on open. `freedoms` is the modelling word, and the solver's own vocabulary is deliberately left alone: `core/formula.py`'s `Unknown` and both solver backends still say unknown, because at that layer it is correct. `core/simulation.py`'s `_context()` is the single translation point.

`instance_of`, `template_of`, `applies_to`, `uses_model`, `overrides`, `varies` and `about` are reserved relationship types; `equation`, `freedoms`, `roles`, `url`, `provider`, `configuration`, `units`, `modoc`, `query` and `plot` are reserved property names. Everything else is ordinary user data. Reserved property names are still just ordinary, editable/deletable properties -- nothing in the schema enforces them, they're only recognized by convention (the UI/engine looks for that exact key).

A reserved name is a **last resort**, not the normal way to give a property meaning. Where a value has a shape of its own, the engine recognizes the shape and the key is yours to name: when something happens is an ISO 8601 interval under any key at all (see Time below), and a task's status or estimate is likewise discovered from what the value looks like (`core/task_candidates.py`). The names above survive because their values genuinely cannot be told apart by looking -- `roles`, `units` and `configuration` are all plain dicts, and an `equation`-shaped false positive would silently join the solver.

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

Each side of a relation evaluates to a ratio of two linear expressions, and the denominator is cleared once against the relation as a whole, so an equation that is only nonlinear as *written* (`I / I0 == 0.1`) is solved as the linear equation it is equivalent to rather than having to be rearranged by hand. Solving then happens in two stages. A substitution loop repeatedly resolves any equation that has come down to a single unknown and feeds the answer back, which allows nonlinear formulas as long as the nonlinear part involves values already known. Whatever remains -- a still-linear system of several unknowns, or an equation genuinely nonlinear in its unknowns -- goes to a solver backend chosen per run via `solver`/`solver_configuration` (`core/solvers.py`), the same pluggable-by-name pattern physical property models use for `provider`/`configuration`. The default, `linear`, is an exact single least-squares solve and fails on genuine nonlinearity; `newton` solves that iteratively instead. See `docs/simulation.md` for the full solver reference.

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

# Sweeping

A scenario answers a second question about a garden. `core/sweep.py`
answers a range of them: solve the whole garden once per step of a
property's range, and report another property at every step. The answer
is a curve rather than a state, which is why it is a module beside
`core/simulation.py` rather than an option on `run_simulation()`, and
why it has its own window (**Moxie**, `run_moxie.py`).

Sweeping cost nothing structurally, for the same reason scenarios did.
The run machinery was already driven from a properties dict, so a sweep
is that dict perturbed N times. What the change *did* require was
lifting the solve out of its two callers: `run_simulation()` and
`run_time_series()` had run the same five steps -- split the relations,
substitute, hand the rest to the backend, check constraints, report the
unsolved -- differing only in bookkeeping. That body is now
`solve_view()`, and `solve_properties()` is its properties-dict front
door. Both existing functions call it; neither changed behaviour.

`solve_properties()` takes properties and relationships rather than a
connection, and that is the structural version of the rule that matters
here: **a sweep writes nothing and logs nothing**. There is no database
in reach to write to. No write-back, no run-log row, no scenario motes
created and destroyed around the edges — which is what makes it safe to
run on every frame of a dragged slider, and is the same refusal a
scenario run makes for the same reason.

Two decisions inside it are worth naming:

**Cases, not series.** A caller asks for *distinct what-ifs* rather than
distinct curves. Eight curves reading eight properties off one solve are
one case, so the cost is `cases × steps` rather than `series × steps`.
Keeping the frontend's notion (a series) out of the engine is what makes
that collapse expressible at all.

**A failed point is a point.** A solve that fails at one x may succeed at
the next, so the message is recorded on that point and the sweep
continues. Constraints ride along the same way: every solve checks them
anyway, so each point carries the ones its answer broke. A plot can then
shade where the model actually broke its own rule, rather than reading a
fixed bound off a constraint and drawing a line that would be wrong
wherever the bound itself moves.

`read_garden()` returns the whole read as a `Ground`, which every entry
point accepts in place of a connection -- so a caller can read under
whatever lock guards its connection and solve outside it. That is not an
optimization: a sweep that held the connection for its whole run would
be holding it against the very keystroke meant to cancel it.

`plot` is a reserved property, and a mote carrying one is a saved plot --
the specification, never the points, so reopening it re-solves the
garden as it stands. It is `query` (Monolith's saved view) one subject
over, down to being asked of the property rather than made a kind.

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

`monarch/` is the desktop app, and like `cli/` and `mcp_server/` it is an
adapter over `core/` -- `monarch/api.py` adds shaping for the JS bridge and no
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
`monarch/`: it exposes the same repository functions as tools an MCP client
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

Where `monarch/api.py` holds one long-lived connection for the open garden,
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
simulation runs. Applying a
simulation to the graph, deleting or restoring gardens, calendar export,
and opening links are deliberately left out -- they either write results
the user should approve in the app, or need a live window. See
`docs/mcp.md` for setup.

# What belongs on which surface

`core/` is the whole system. `cli/`, `monarch/`, `mora/`, `monastery/`,
`momentum/`, `monolith/`, `motion/`, `gardens_app/` and `mcp_server/`
are nine adapters over it, and each one's job is a
different question, so none of
them is expected to expose everything. What is *not* acceptable is a gap
with no reason behind it -- which is what the surfaces had accumulated:
property models existed only in the CLI, templates only in MCP, not by
decision but by whichever surface happened to be open when each was
built.

The charter:

* **core** -- complete. Every capability lives here, and an adapter that
  holds logic of its own is a bug (see Calendar export, moved out of
  `monarch/api.py` for exactly this reason).
* **CLI** -- everything scriptable. If it can be done without a window
  and without a human approving a result, it has a command.
* **UI** -- everything interactive. Anything a person would reach for
  while looking at a graph, including the things only a live window can
  do: opening links, exporting to Calendar, approving a simulation.
* **Mora** -- the same charter as the UI, for one thing: writing and
  reading .modoc notes (see Notes below). A second window rather than a
  fourth kind of adapter -- it is the UI's charter applied to a surface
  a graph canvas has no room for.
* **Monastery** -- likewise, for motes that have a time (see The
  calendar below). A third window on the same terms, and for the same
  reason: a week is a shape a graph canvas cannot draw.
* **Momentum** -- likewise again, for motes that are work to be done
  (see Tasks below). A fourth window, for the same reason once more: a
  board, a planned week and a running timer are shapes neither a graph
  nor a calendar draws.
* **Monolith** -- likewise once more, for the garden as a whole rather
  than for any one kind of mote (see The table below). A fifth window,
  because a query across every mote and an edit applied to two hundred
  of them are shapes a graph, a page, a week and a board all lack: each
  of those shows a mote at a time, and this one shows the set.
* **Motion** -- likewise a sixth time, for the garden as a system that
  runs (see Simulation below, and `docs/simulation.md`). A sixth window,
  because a scenario rail, a tick-by-tick timeline and a rule's own
  bindings are shapes none of the other five draws -- and because
  simulation had been a settings panel and a scrub bar bolted onto
  Monarch, which is a graph editor and not a simulator.
* **Gardens** -- the gardens themselves, rather than anything inside
  one: create, rename, delete (with undo), import, export, and the
  per-garden sync folder. A seventh window, and the only one that opens
  on no garden at all -- which is exactly why it is separate. All six of
  the others open it ("Manage gardens…" in each garden dropdown) instead
  of carrying a copy, and it holds no garden connection, so renaming or
  deleting the garden the window that opened it is looking at is an
  ordinary operation rather than a case to refuse. Its own process, so
  it cannot switch that window's garden; each row offers **Open in**
  instead. Deliberately not: anything inside a garden -- it never reads
  or writes a mote.
* **MCP** -- reads plus graph edits (above). Deliberately not: writing a
  simulation to the graph, deleting or restoring gardens, Calendar
  export, opening links.

A capability missing from a surface its charter covers is a gap to
close, not a decision. A capability missing from a surface its charter
excludes should say so, here, in one line.

# Links and external export

`url` (`core/links.py`) makes a mote openable -- a web address or a local
file path, launched via the OS's default handler. See `docs/ui-setup.md`.

# Time

**When something happens is one property, under any key you like, and no
reserved name at all.** It is an ISO 8601 interval (`core/interval.py`),
which carries its own span and its own repetition:

```
2026-09-01                        an all-day event, one day
2026-09-01/2026-09-03             all-day, Sep 1 and 2 -- the end is EXCLUSIVE
2026-09-01T09:00                  timed, the default 60 minutes
2026-09-01T09:00/PT1H30M          timed, 90 minutes
2026-09-01T09:00/2026-09-01T10:30 timed, an explicit end
2026-09-01T09:00/PT1H
RRULE:FREQ=WEEKLY;BYDAY=TU        ...repeating, with an EXDATE line beneath
                                     excepting one occurrence
```

Any property whose value parses as one places the mote; `core/calendar.py`
calls each such key a **binding**. A garden where meetings say `when`,
tasks say `due` and events say `starts` needs no migration and no
declaration -- it is answered by looking, exactly as a species is
(`core/kinds.py`).

This replaced three reserved names with none. `date`, `duration` and
`recurrence` used to be separate properties, and only the first had a
shape you could recognize -- a bare `30` is not distinguishable from any
other number, so a span and a rule *had* to be found by name. Folding all
three into one self-describing value is what removed the last reason for
the calendar to know any name at all. `date` survives only as the key a
*new* mote's start is written to (`core/calendar.py::DEFAULT_BINDING`), a
default rather than a name anything reads.

Gardens written the old way are folded on open
(`core/db.py::_migrate_intervals`), idempotently, **keeping each garden's
own key name** -- `date` stays `date`, `when` stays `when`. An all-day
`ends` was inclusive as people wrote it and becomes an exclusive interval,
which is the end `core/calendar.py` always computed internally anyway.
Durations use days/hours/minutes/seconds only: years and months have no
fixed length, so an event lasting "one month" has no single answer.

The one thing this costs: hand-typing a separate `duration` beside a date
no longer does anything. An interval picker is the mitigation -- Monarch's
detail panel (`monarch/frontend/app.js::intervalField`) composes and reads
the notation above through a calendar grid and a repeat control (frequency,
interval, weekdays, and an end that is either a count or a date, matching
`FREQ`/`INTERVAL`/`BYDAY`/`COUNT`/`UNTIL`), rather than requiring anyone
hand-type `RRULE:FREQ=WEEKLY`. It writes only what it can fully represent: a
span or rule outside what its controls cover (a `BYMONTHDAY=`, a multi-day
timed span) is shown and edited as raw text instead of being silently
narrowed to fit.

MonOSS reaches no network. There was one exception -- a manual, one-way
export of dated motes to Google Calendar -- and it was removed once
Monastery made it redundant, taking the `gcal_event_id` reserved name and
the `google-auth`/`requests` dependencies with it. That export was also
the only thing that ever required *writing* to one fixed key, which is
why the calendar's read path could always be general while its write path
could not. Removing it is what made the rest of this possible.

# Notes

`modoc` (`core/modoc.py`) holds a **.modoc note**: prose written *about*
the graph, in a small LaTeX subset whose three MonOSS macros --
`\mote{...}`, `\val{...}`, `\graph{...}` -- resolve against the garden
every time the note is read. A due date or a task count in a note is a
lookup, not a copy, so a note cannot go stale the way a typed-in number
does. See `docs/modoc.md` for the language.

`\graph{...}` is the same idea for a figure, and is the one macro that
resolves to something another *window* made: it names a view saved in
Moxie, and `core/plotview.py` re-solves that view's saved specification
against the garden as it stands. A note therefore never describes a
figure of its own -- there is one place a figure is designed, one place
it is computed (`core/sweep.py`), and one renderer that draws it in both
windows (`frontend_shared/plot.js`). The note refers.

Storage is one reserved property on an ordinary mote, and that is the
whole design. A note is not a new object type, and carrying one is
deliberately **not** a kind (Kinds above): every entry in that table is a
job a mote does in the graph's machinery -- a rule solves, a property
model computes -- and `modoc` changes nothing about how a mote is
treated. It
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

**A note has no clock.** Every macro resolves against the garden as it
stands, and there is no step to ask for. Notes used to resolve at `t+N`
against `run_time_series(commit=False)`, with Mora carrying a stepper and
an `Apply t+N…` button that committed the projection; all of it is
removed. The line it was crossing is the one the windows are divided on:
a note reports what a garden *is*, Motion is where a garden moves through
time, and Moxie is where a property is swept. A note editor that could
also run and commit a simulation held a lever belonging in another room.
What is left is a stronger promise than "it asks first" -- the note path
has no write in it at all.

Nothing in the note path raises. A note with a typo still renders with
the broken part visibly broken, and every failure -- unknown command,
unresolved reference, unevaluable expression -- becomes a `Diagnostic`.
What counts as a problem is answered once, in
`core/modoc_bind.py::problems()`, for the same reason `core/kinds.py`
exists: it was briefly the CLI's own, and Mora promptly reported a note
with a dangling `\mote{...}` in it as having no problems at all.

`mora/` is the editor (`run_mora.py`) -- a second pywebview window with
its own garden connection and source/rendered panes. It parses nothing
itself: the rendered pane is built from the
tree `core/` produced and `mora/payload.py` shaped, which is why the
window and `monoss modoc render` cannot come to disagree about what a
note means. Only the source pane's syntax highlighting runs in
JavaScript, colour being presentation rather than meaning.

Monarch reaches it through `monarch/api.py::open_mora`, which spawns
`run_mora.py` detached under `sys.executable` -- a sibling process, not a
child window, so neither app's lifetime is tied to the other's. That
makes two processes on one garden file the normal case rather than the
exception, which is why `mora/api.py` sets `busy_timeout` on its
connection for the same reason `mcp_server/server.py` does. `core/db.py`
is deliberately left alone, so the CLI and Monarch keep the
behavior they have.

Two apps at once also means the two must not want the same HTTP port or
the same WebKit storage directory, and the port is the one with teeth:
pywebview pins its server to 42001 unless told otherwise, and the
process that loses that race does not fail visibly -- it points its
window at the winner's address and loads the other app's page against
its own bridge. `mora/app.py::choose_port` asks for 42002 and falls back
to an ephemeral port; `.webview_data_mora/` is its own storage. See
`docs/ui-setup.md`.

# The calendar

`core/calendar.py` places motes in time, and `monastery/` is the window
that draws them (`run_monastery.py`).

The design decision worth stating is what it does *not* do: there is no
calendar-entry type, and no property that means "this is an event."
Instead a **binding** is any property key whose value parses as a date,
discovered per garden rather than declared -- so a garden where meetings
say `when`, tasks say `due` and events say `starts` shows all three
without a migration and without agreeing on a key first. Nothing is
stored to make a key a binding, exactly as nothing is stored to make a
mote a species: it is answered by looking. This is the monism paying for
itself again -- the calendar needed no new object type, no new
relationship, and no new reserved property.

That generality is deliberately **read-side only** for *creation*.
Creating a mote from the grid always writes `date`, the conventional
name. Reading is general because a garden is; a *new* date has one right
home, and having two would be how a garden ends up with the same event
under three keys.

Editing goes the other way, and has to. A rectangle dragged across the
grid rewrites **the binding that placed it** -- drag a task placed by
`due` and `due` moves -- because an edit should change the thing you were
shown. Writing `date` instead would leave `due` where it was and place
the mote on the grid twice. A resize needs no such choice any more -- the
span lives inside the same value, so a move and a resize are both one
write to one key. All of it is computed by pure functions in
`core/calendar.py`'s writing section (`moved_properties`,
`resized_properties`, `detached_properties`), so what a drag *means*
is tested without a window, and `monastery/api.py::place_occurrence`
only writes down what it is handed. Move and resize are one bridge
method because the top edge of an event is both at once.

`recurrence` -- an RFC 5545 RRULE -- is expanded by `dateutil.rrule`
(the one new dependency, approved for this) into one occurrence per
repetition inside the visible range. Every occurrence carries the same
mote, so clicking any of them opens that mote: one mote → one event →
the whole series. A rule that
doesn't parse places the mote on its own date rather than hiding it --
the same permissiveness `core/modoc_bind.py` gives a broken note, and
for the same reason.

Deleting one occurrence of a series needed no new reserved property
either: RFC 5545 already says "except this one" with an `EXDATE` line,
and `dateutil` already reads a rule and its exceptions as one set. So the
exception lives on a line beneath the interval that placed it.

Moving or resizing *one* occurrence of a series is the same mechanism
read forwards: an `EXDATE` where it was, and an ordinary copy where it
now is, carrying the original's species and dropping its rule lines.
RFC 5545 would call that copy a `RECURRENCE-ID`
override; MonOSS has no such thing on purpose, and a plain mote says the
same thing without a second kind of event. Because one mote *is* the
whole series, the window asks which was meant rather than guessing --
the only question Monastery puts in front of a write.

Everything in `core/calendar.py` is pure: motes in, occurrences out, no
connection. That is the discipline `core/scenarios.py::apply()` and
`core/incidence.py` already follow, and it is why the whole calendar is
testable without a window (`tests/test_calendar.py`). `monastery/api.py`
adds only what a window knows -- which garden is open, and which
bindings the person switched off -- and `monastery/payload.py` only
reshapes.

Two exclusions are inherited rather than reinvented. Templates and
scenario overrides are never placed, even when dated, because they hold
replacement values rather than real data. And a mote is coloured by its
species through the same palette the graph canvas uses, so it reads the
same colour in all seven windows.

Monastery is a third process on the same terms as Mora, which means the
same two collisions to avoid: `monastery/app.py::choose_port` asks for
42003 (42001 is Monarch's, 42002 Mora's) and falls back to an
ephemeral port, and `.webview_data_monastery/` is its own WebKit
storage. And since nothing in MonOSS pushes -- no adapter announces its
writes -- the window **polls** while focused rather than waiting to be
told. See `docs/ui-setup.md`.

## Tasks

Momentum (`momentum/`) is a fourth window, over the same gardens, for
the motes that represent work: a board by status, a planned week, and a
timer that logs what the time went into.

**It reserves no new property names.** This is the point worth stating
plainly, because it is a deliberate reversal of how the calendar's
`date`/`duration` began. `core/calendar.py` already showed the better
shape -- a *binding* is any key whose value parses as a date,
discovered per garden rather than declared -- and Momentum applies that
idea to the two shapes a board needs and a date parser cannot see
(`core/task_candidates.py`):

* an **enum key** holds a short label out of a small repeating set, so
  `status: doing` and `priority: high` are recognized by the shape of
  the set rather than by any vocabulary. A garden whose statuses are
  `icebox` and `shipped` is as recognizable as one saying `todo`/`done`,
  and its columns say `icebox` and `shipped`.
* an **estimate key** holds a duration (`5h`, `90m`, `1d`, or a bare
  number of minutes).

Candidates are ranked, never chosen: the window offers them in a
sidebar and the person confirms, exactly as Monastery's sidebar offers
bindings, and the answer is remembered by the window rather than
written to the garden. A preferred *name* (`status`, `priority`,
`estimate`) only breaks a tie among discovered candidates.

**Scheduling is one write to an ordinary date property.** Dropping a
task on a day asks `core/calendar.py::earliest_free_slot` for the first
gap wide enough, read from the very occurrences Monastery draws, and
writes that instant to the binding the mote already carries -- or, for
an unscheduled one, to the key the garden already organizes itself by.
So a task scheduled in Momentum *is* an event in Monastery, with no
second store of "when I plan to do this" and no sync step between them.
An estimate becomes the `duration` the calendar draws, unless the mote
already declared one.

**A logged session is an ordinary mote**: an instance of a `Work
Session` species carrying `date` and `duration`, pointing at its task
with the reserved `about` relationship that Mora's notes already use.
Being ordinary is exactly why it appears in Monastery and Monarch
without either window knowing Momentum exists. Only a *finished*
session is written -- a running countdown is the window's own state,
the same preview/commit split `core/simulation.py` keeps.

Two things are deliberately left out of the board: logged sessions
(work already done is not work to do, though it still occupies the time
it took) and motes acting as a species (a species declares what a kind
of thing *is*). There is no positive gate beyond those -- a garden's
tasks are whatever its motes say they are, the same answer Monastery
gives about which motes are dated.

Momentum is a fourth process on the same terms as the other two:
`momentum/app.py::choose_port` asks for 42004 and falls back to an
ephemeral port, `.webview_data_momentum/` is its own WebKit storage,
and the window polls while focused. See `docs/ui-setup.md`.

# Species colour

A mote is coloured by its species, in all seven windows, and the rule is
one every window carries a copy of rather than one any of them decides.

Hashing a hue out of the species id -- which is what this was -- gave
every species an independent random hue, and at the lightness and chroma
the palette uses, 360 hues read as about five colours: a garden's tenth
species routinely looked like its third. Each species now claims one
entry of a fixed palette instead. Hues walk the wheel by the golden
angle so consecutive entries land far apart, and every twelfth entry
changes lightness and chroma, so the second time past a hue is still
visibly a different colour. A species mote itself draws as the brighter
variant of its instances' colour.

Which entry a species claims is decided by probing: it prefers the entry
its id hashes to -- stable across sessions, and unmoved by most later
additions -- and takes the next free one when that is claimed, which is
what stops two species sharing a colour until the palette is genuinely
full. Probing makes the answer depend on *which species exist*, and that
is why the universe is the garden's whole species list
(`api_common.py::species_ids`) rather than the ones a window can see:
Monarch under a hop limit draws a subgraph, and a palette claimed from a
subgraph would give one species two colours in two windows. Every
window's main payload carries that list, so no window pays a call for
it.

# The inspector

One mote, one surface, seven windows. Selecting a mote anywhere -- a node
in Monarch, a row in Monolith, a card in Momentum, a placement in
Monastery, a link in Mora, a node in Motion -- opens the same floating
panel, with the same tools in it.

It was not like that. Each window had grown its own docked detail panel,
and what you were allowed to do to a mote depended on which window you
happened to be standing in: Monarch could add a property, rename a key
and set a species; Motion could overwrite the values a mote already had
and nothing else, with no way to add one; Mora could read properties and
edit none. Same mote, six different sets of permissions, none of them
decided -- just whichever panel had been written when.

The panel floats rather than docking, and that is the same argument as
sharing it. A docked panel is part of the window it is docked to, and
six docked panels read as six panels however alike they look. One slab
of glass hovering over a graph, a table and a calendar reads as one
instrument you carried there. It opens in the same place in every window
(upper right), draws a dashed leader line back to whatever you clicked,
and remembers where you drag it -- per window, because each is its own
HTTP origin and localStorage cannot literally be shared between them.

**Three shared files, not one.** `frontend_shared/inspector.js` is the
panel; `interval.js` is the calendar a date-shaped property opens, which
is separate because two callers need it -- the panel, and Monastery's
draft editor, which is not a panel at all; `menu.js` is the right-click
dropdown, which is separate because what goes *in* it is each window's
own business and only the look, placement and dismissal are shared.

**Two halves, one contract.** `api_common.py::InspectorApi` is mixed into
all seven `Api` classes and is the whole bridge the panel speaks:
`inspect_mote` plus the `inspector_*` writes. `frontend_shared/
inspector.js` and `inspector.css` are the panel, symlinked into each
app's `frontend/` -- pywebview serves each frontend directory as its own
HTTP root, so a file above that root is not fetchable, and the symlink
is what makes six copies one file. `tests/test_inspector_api.py` asserts
every window implements the whole contract, so a seventh window that
grows an `Api` without the mixin fails there rather than at the first
click in a panel that silently does nothing.

**A species is a relationship, and the panel says so.** `instance_of`
is listed like anything else. It was hidden at first, on the grounds
that a species dropdown in Advanced said the same thing -- which made
the one structural fact the whole graph is built on the only one you
could not see in the list of what a mote is wired to. The dropdown is
gone; setting a species is adding that relationship, in the same picker
as any other. What stayed in Advanced is the template button, which is
not a restatement of anything: it declares that *this* mote is the one
new instances start from.

**Types come from the garden, not from a table.** The panel draws a
typed editor per property -- text, number, date, boolean, enum, or a
button that walks to another mote -- and decides which from the value's
shape and the mote's species, never from a hardcoded vocabulary of key
names. An enum is a key whose values, across the instances of that
species *and its template*, are a small repeated set; the template
counts because it is the species' own statement of what a value looks
like, and without it a four-instance garden shows `status` as free text
until two motes happen to agree. The "usually has" chips are the
template's keys this mote lacks. A species with no template offers none,
which is honest: nothing in the garden has said what that kind of mote
usually carries.

**What stayed in app chrome.** The panel is for one mote. A selection of
two hundred is a different question, so Monarch's and Monolith's bulk
editors stay where they were, as do Monarch's relationship panels,
Monastery's occurrence controls (which placement was clicked, moving it,
detaching it), Momentum's status pills and subtasks, and Motion's rule
editor. Deleting a mote likewise stays in each window's own chrome --
the inspector edits a mote, it does not end one.

**What a window may still contribute.** Two hooks, both narrow on
purpose. `valueEditor` lets a host offer a better editor for a value the
generic ones would flatten -- what is left there is Monarch's `provider`
summary, and the list is meant to keep shrinking: the interval picker
used to be in it and is now everyone's. `advancedSections` adds to the Advanced pane, which is also where the
two window-specific powers live: Motion's `why` (the run log's answer
for where a solved value came from -- it is the only window with a
solver, so `inspector_why` returns None in the other five and the
control is never drawn) and Monarch's "+ New instance". Anything only
one window can do is a gap in the other five, so both lists should stay
short and shrink.

**Motion writes differently.** While a scenario is selected, Motion
redirects property writes onto that scenario's override mote rather than
the mote itself -- looking at a hypothetical must never mean overwriting
the garden's own state (see Scenarios). That is an override of
`inspector_set_property`, one layer below the panel, so every editing
affordance the shared panel has -- add a property, rename a key, toggle
a boolean -- inherits the rule instead of each having to remember it.

# Dates and the interval picker

A date in MonOSS is an ISO 8601 interval (`core/interval.py`): a start,
an optional span after a `/`, and optional RRULE lines beneath it. That
is a good thing to store and an awful thing to type, so any property
holding one gets a calendar -- **by value shape, not by key name**.
`date` stopped being a key the engine reads when a start became an
interval, so `when`, `due`, `starts` or whatever this garden calls it
all get the same editor, repeating or not.

The widget is `frontend_shared/interval.js`, and it has two callers: the
inspector's property rows, and Monastery's draft editor. It did not
start that way. The picker was Monarch's, and the structured repeat
editor -- frequency, interval, weekdays, how it ends -- was Monastery's,
on the reasoning that Monastery is the window that actually reads a
`recurring` mote back and so should be the window that authors the rule.
Monarch's picker was correspondingly reduced to showing an RRULE as raw
text. Both halves of that argument stopped holding when the inspector
became one surface in seven windows: "edit this date properly" has to work
wherever you are standing. One widget now, one repeat editor inside it,
two callers.

It never expands a recurring value into occurrences -- that is
`core/calendar.py`'s job, via dateutil, and "no JavaScript reads an
RRULE" stays true for *that*. What it does is decompose a value into an
editable draft and recompose one back. Anything it cannot safely
decompose -- a multi-day span, two RRULE lines, a `BYMONTHDAY` -- is
marked `simple: false` and kept as raw, editable text. Structured
controls that silently dropped an `EXDATE` would be worse than an honest
textarea.

The popup is fixed-position rather than inline: the calendar needs real
width, and the panel hosting it is resizable down to sizes that would
crush it. It re-measures its anchor on every reposition and watches for
the host panel moving, because measuring once on open is what left the
calendar stranded at the coordinates the field used to be at.

# The table

Monolith (`monolith/`) is a fifth window: the garden as rows and
columns. It is where large edits happen -- forty motes given an owner in
one gesture -- and where structured questions get asked across
everything at once, rather than one mote at a time.

**Columns are discovered, never declared.** A garden has no schema, so
there is nothing to read a column list off. `monolith/payload.py::Schema`
builds one per request from what is actually there: every property key
anyone has used (with how many motes carry it, and whether it is a
reserved name), every relationship type anyone has drawn -- in both
directions -- and the handful of things every mote has: name, id,
species, degree, last-updated. The same discovery pays for the cell
editor, because a column's distinct values *are* its enum: clicking a
`status` cell offers `todo`/`doing`/`blocked` on a garden where nobody
ever declared a status type, and offers a plain text field on a column
where every value differs.

**A query is a first-class thing, and it lives in `core/`.**
`core/query.py` holds the language -- fields, operators, joins, sorting,
grouping -- and Monolith is only an editor for it, the same split
`core/modoc.py` and Mora draw. A **field** names one of four sources:

    property   a JSON property key
    builtin    name, id, species, degree, last-updated
    rel        the motes across one relationship type, in one direction
    graph      what the graph makes this mote: instance_of, kind, related

The last two are why this is a table over a *graph*. `→ depends_on
includes "Schema translation"`, `kind is species` and `related is empty`
(which is how you ask for orphans) are questions no property filter can
express. `rel` and `graph` fields are list-valued and therefore share one
set of operators, and a `rel` column is editable: setting a cell writes
relationships, which is the join a spreadsheet cannot do.

**A saved view is an ordinary mote** carrying a `query` property -- the
one reserved name this window adds. Like `modoc`, carrying one is data
rather than a job in the graph's machinery, so it is deliberately not a
`core/kinds.py` kind: a view mote that read as a kind would displace its
real species in every other window's type label. Being ordinary is what
makes a view sync between devices, appear in Monarch, and be linkable
from a note.

**Two gestures write relationships in bulk.** The relationship matrix
puts the visible rows on both axes and writes or erases one edge per
click, painting the type selected above it; and the bulk bar chains a
selection in row order or fans it into its first row. Thirty imported
motes into a dependency chain is one gesture here and twenty-nine in the
graph window.

Everything else the window does is ordinary writes through
`core/motes.py` and `core/relationships.py`, one primitive at a time, so
a two-hundred-mote edit is two hundred oplog rows that `core/sync.py`
replicates like any other. The one destructive action is guarded rather
than fast: `monolith/api.py::delete_motes` refuses without an explicit
confirmation, the same preview/commit split Mora keeps between saving a
note and applying a simulation step.

Monolith is a fifth process on the same terms as the other three:
`monolith/app.py::choose_port` asks for 42005 and falls back to an
ephemeral port, `.webview_data_monolith/` is its own WebKit storage, and
the draft query -- the one being edited before it is saved as a view --
is the window's own state in `localStorage`, not the garden's. See
`docs/ui-setup.md`.

# Simulation

Motion (`motion/`) is a sixth window: the garden as a system that runs.
Scenarios, solvers, runs, provenance and rule authoring are one subject,
and it is the one subject Monarch could not give a full window to -- it
lived there as a settings popover, a bottom scrub bar and a runs panel,
gating every edit in the graph behind "commit or discard first". Moving
it out removed that gate from Monarch entirely (`editBlocked` now
answers only for attractor mode) and gave simulation the room it needed.

**Nothing about the engine moved.** `core/simulation.py` never imported
from `monarch/`, so the split was two thin layers: the bridge methods
(now `motion/api.py`) and the UI. `core/simulation.py::step_payloads`
was already the shared shape for a run's steps, which is why a replayed
run and a live preview reach Motion's timeline through one path.

**What a node *is* is derived, like everything else.** A mote carries no
coordinates and no declared role in a simulation, so `motion/payload.py`
derives four node kinds from facts core already answers: `rule` (an
equation plus an `applies_to`, from `core/kinds.py`), `free` (declares
`freedoms`), `varied` (some scenario overrides it -- so this one is
relative to the scenario in hand) and `fixed`. The obvious carve-up
(pump, sensor, valve) would be domain modelling, and that belongs in the
garden rather than in a viewer. Layout is the frontend's and lives in
`localStorage` per garden: writing a dragged position back would make
every pan a graph edit.

**The timeline counts ticks, and says so.** A tick is one re-solve of the
equation set; the engine has no `dt`, no seconds and no integration.
Rather than let the dock imply otherwise, every run payload carries an
`axis` from `motion/payload.py::tick_axis`, which is the single place
that answers "what is step N called" -- the seam a real time axis
attaches to later. The solver menu is read from
`core/solver_registry.py` for the same reason: a future integrator
appears in Motion by being registered, not by editing the window.

Motion is a sixth process on the same terms as the other four:
`motion/app.py::choose_port` asks for 42006 and falls back to an
ephemeral port, and `.webview_data_motion/` is its own WebKit storage.
A commit made here is not observed by an open Monarch until it refetches
-- the same rule every adapter lives under. See `docs/ui-setup.md`.

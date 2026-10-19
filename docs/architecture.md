> Reference for the code as it is today. For the product direction, see
> `overview.md` and `roadmap.md`. Sections change as each roadmap phase
> lands.

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

## Projects: motes own gardens

`core/projects.py` reads the one convention `overview.md` -> *Structure*
builds projects from: a mote carrying the reserved `garden: <name>`
property owns that garden and is its cover.

- `owners()` scans every garden for claims, keyed by the garden claimed.
  It asks SQLite for the `garden` key alone and loads full properties only
  for the motes that carry it, since the scan crosses the whole library.
  It is read-only; it never writes to a garden it opened to look.
- `check()` is the one-owner check. It reports, never raises: `disputed`
  (two motes claim one garden), `missing` (a claim on a garden not on
  disk) and `loop` (a garden that owns itself through some chain, found
  by a depth-first walk). Each `Problem` carries a ready sentence.
- `tree()` is what Home draws. Roots are the gardens nobody owns -- a hub
  and a loose demo alike. A disputed garden hangs under every claimant so
  the duplicate stays visible, and a garden caught in a loop becomes a
  root of its own rather than vanishing.
- `start_project(conn, mote_id, name=None)` creates the garden and writes
  the property in one step. A name derived from the mote's never adopts
  an existing garden (it steps to `-2`, `-3`); a name passed in adopts a
  loose one. It refuses a mote that already owns a garden and a garden
  that already has an owner. Undo clears the property and leaves the
  garden on disk, loose: a garden is a file another surface may have
  open. `stop_owning` is the property half alone.
- `Owner.cover` is what Home lists and Mora's title block prints: `name`,
  `due`, `status`, `author`, `revision`, each None when absent. They are
  ordinary properties, not reserved -- a Canvas-synced homework mote
  already carries `due` and `status`. A number is read as its text, so a
  hand-typed `revision: 3` is `"3"`.

### Archive

`archived: true` on an owner archives the garden it owns *and everything
beneath it*; on a mote claiming no garden, it archives the garden the mote
lives in, which is how a loose garden is archived (decided). Disk stays
flat and nothing is renamed.

- `archived_by(garden)` says why a garden is archived (its own marker, an
  owner, or an ancestor), or None. `Node.archived` carries the same
  inherited flag, so Home can hide archived trees.
- **Read-only is enforced at the surfaces**, not in the store (decided):
  `ensure_writable(garden)` raises `ArchivedGarden`, and every MCP write
  tool and CLI write command calls it first. The console holds one
  connection per window, so it enforces the same rule at that seam:
  `console/session.py::Session.sync_archive` sets `PRAGMA query_only`
  while the open garden is archived (on open, `cd`, rename and after any
  `garden archive|unarchive`), `Session.describe` words the refusal, and
  the top bar shows `archived · read-only`. Sync (its own connection) and
  the store are untouched, so un-archiving always works.
- `set_archived(garden, bool)` writes the flag on the sole owner, or a
  marker mote named `Archived` inside a loose or disputed garden.
  Un-archiving clears owners and markers (deleting a marker it made,
  keeping any other mote), and refuses if an ancestor still archives the
  garden, naming it.

### Search across gardens

`core/search.py::search_all(query)` opens every garden read-only and runs
the same `motes.search_motes` a single garden uses, so query forms mean
the same everywhere. Each `Hit` carries its garden and that garden's
cover (`owner`, `project`), since a result opens as its project. Archived
gardens are searched after live ones and flagged; `truncated` names the
gardens that had more hits than `per_garden`. A blank query returns
nothing.

`monoss project start|stop|tree|check|archive|unarchive`, `monoss search`;
MCP `project_tree`, `start_project`, `archive_garden`, `search_gardens`.

# Species

A mote can act as a "species" -- a shared type that other motes are declared instances of. A species is still just a mote; nothing marks it as special structurally. Membership is a relationship: a mote is an instance of a species by having an `instance_of` relationship pointing at it. `instance_of` is a reserved relationship type recognized by the engine and UI, the same way any type system needs some minimal way to express "is-a."

Species are flat for now -- a species being an instance of another species is not interpreted as inheritance. A mote may be an instance of more than one species. In v1, species only matter as a way to target rules at every instance of a type at once (e.g. "this rule applies to every instance of Resistor"); enforcing required properties per species (e.g. every instance of Person must have `age` and `phone_number`) is a deferred extension, not built yet.

A species may have a **template**: a mote pointing at it via a `template_of` relationship, whose properties new instances start from. `template_of` is deliberately a different relationship type from `instance_of` -- a template holds placeholder values and belongs to no simulation, so it must not appear among a species' instances.

# Kinds

Species, template, property model and rule are all the same idea: an ordinary mote that *is* one of these -- a **kind** -- because of what points at it. Nothing is stored on a mote to say so -- that is what "everything is a mote" costs, and the price is that "what is this?" has to be answered by looking.

Called kinds and not roles, which is what this was: `core/roles.py` answers a different question -- what an assembly's parts are *called* (see Roles below). **A kind is what a mote is; a role is what something else calls it.** A kind is derived and never stored; a role is a declared property. The frontend was already using this word (`kindLabel`, `kindOf`), so the rename made existing names correct rather than inventing one.

`core/kinds.py` is where it is answered, once. It is an index, not an implementation: each kind's detection stays in the module that owns the concept -- `species_ids_in()`, `template_ids_in()`, `model_ids_in()`, `rule_ids_in()` + `has_equation()` -- and the `KINDS` table just says which is which, plus the human label every surface shows. A declarative table rather than a providers-style registry: there is no plugin story for kinds, and `docs/rules.md` rules out the global mutable state a `register_kind()` would need.

| kind | recognized by |
| --- | --- |
| species | target of `instance_of` or `template_of` |
| template | source of `template_of` |
| property model | target of `uses_model` |
| rule | source of `applies_to` **and** carries an `equation` |

Two forms, one answer: `kinds_in(motes, relationships)` for a caller holding an already-fetched subgraph (each kind's id set computed once, then intersected), and each module's own `list_*()` for the same question asked as a query. Kinds are not exclusive -- a `template_of` target counts as a species so that a species you've set up but not yet instantiated still registers as one.

The graph pane's payload (`core/graphview.py`) carries each mote's kinds, from the same table (`core/table.py::describe`). One list of names per mote replaced the `species_ids` array, one kind hardcoded into the payload's shape -- the frontend had no way to hear about the others. Adding a kind is now a change to the table, not to a payload.

Adapters must not answer "is this a species" for themselves. That is how a subtlety ends up true in one surface and false in another.

# Simulation

This is the "relationships modify mote properties programmatically" capability described above.

A rule is a mote with an `equation` property and an `applies_to` relationship. What that relationship points at decides the rule's reach, inferred rather than declared: a species means once per instance, any other mote means once about that mote (see "Roles" below).

Which of a mote's properties the solver may determine -- its **freedoms** -- comes from a `freedoms` property. On a mote acting as a species it declares what every instance is free to have determined; on any other mote it declares what that mote itself is free to have determined, and a mote's own declaration replaces its species' rather than adding to it. That is what lets two instances of one species be asked opposite questions -- sizing and rating from the same equation -- without editing what the species *is*.

`freedoms` was called `unknowns` while it could only live on a species; gardens are migrated on open. `freedoms` is the modelling word, and the solver's own vocabulary is deliberately left alone: `core/formula.py`'s `Unknown` and both solver backends still say unknown, because at that layer it is correct. `core/simulation.py`'s `_context()` is the single translation point.

`instance_of`, `template_of`, `applies_to`, `uses_model`, `overrides`, `varies` and `about` are reserved relationship types; `equation`, `freedoms`, `function`, `parameters`, `roles`, `url`, `provider`, `configuration`, `units`, `modoc`, `query`, `plot`, `source`, `garden`, `check`, `tolerance`, `uncertainty`, `archived`, `image` and `submission` are reserved property names. Everything else is ordinary user data. Reserved property names are still just ordinary, editable/deletable properties -- nothing in the schema enforces them, they're only recognized by convention (the UI/engine looks for that exact key).

The console reserves a third kind of name: the words that open a command line (`core/reserved.py::RESERVED_VERBS` -- `find`, `set`, `open`, `rename`, `unlink`, `template`, `plot`, `fit`, `sim`, `rule`, `scenarios`, `solve`, `why`, `step`, `play`, `pause`, `cd`, `layout`, `refresh`, `undo`, `redo`, `log`). They read verb-first: a line starting with one of them is that verb, even if a mote shares the name. Such a mote is still ordinary and reachable as `"log"` (quoted) or `#<id>`, and the statement that creates one says so. Each console port step adds its own verbs to the list.

A reserved name is a **last resort**, not the normal way to give a property meaning. Where a value has a shape of its own, the engine recognizes the shape and the key is yours to name: when something happens is an ISO 8601 interval under any key at all (see Time below). The names above survive because their values genuinely cannot be told apart by looking -- `roles`, `units` and `configuration` are all plain dicts, and an `equation`-shaped false positive would silently join the solver.

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
promotes them to a failure. See `docs/simulation.md` for the reference.

# Physical property models

A mote can reference a **property model** -- another mote describing how
one of its physical properties (e.g. a fluid's enthalpy) is determined --
via a `uses_model` relationship. The property-model mote itself carries a
`provider` property (which registered provider implements it, e.g.
`ideal_gas`) and a `configuration` property (whatever that provider needs
to instantiate, e.g. a gas constant). `core/model_resolver.py` turns that
relationship into an executable `PropertyProvider` (`core/providers.py`);
simulation rules never see the provider directly, only the `property()`
builtin described in `docs/simulation.md`.

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

`core.simulation.run_simulation()` is the only path that writes: it solves the garden once, writes the solved values back onto the motes. `run_time_series()` is a preview and never writes. Both call the same `solve_view()`, which is what makes a one-step preview show exactly what a solve would write. A run's stepping loop is `simulation.iterate()`, which `run_time_series()` and a plot over time (`plot … vs t`, `core/sweep.py`) both walk.

A preview can be time-based: `run_time_series()` solves a requested number of discrete steps, each one starting from the *previous* step's solved values rather than the database, so a rule can use the reserved `t` (the current step index) or `prev(x)` (the value `x` had entering the step) to express time-dependent behavior or a recurrence. It is computed entirely in memory and writes nothing. A solve that writes back is one-shot and has no previous step, so a rule using `prev()` is refused there rather than advanced by a tick.

No log of past runs is kept. The garden's state is its motes, and how a value came out is not stored: `why` solves the garden again in memory (*Provenance*, below). See `docs/simulation.md` for the language reference and what is deferred.

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

A rule marked `check: true` is sorted with the constraints whatever its
operator, which is how an *equality* is checked rather than solved. Its
tolerance is relative (`tolerance`, default 0.001) and sized by
`Equation.scale`, the largest magnitude the relation took, so no second
evaluation is needed. `core/rules.py::is_check` is the one reading of the
key; simulation, incidence (`EquationTouch.check`) and health all ask it.

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
scenario reports what would change and records no provenance, and
`run_time_series` is a preview under any scenario. The answer to a
hypothetical is not state, and there is no good answer to where a
scenario run's results would be written.

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
why it has its own surface (**Moxie**, the console's `plot` pane).

The console reaches it through a line, never through a hand-built spec:
`core/plotcmd.py` parses `plot y… vs x lo:hi [by y lo:hi] [--across]
[--set m.k=v] [--scenario s] [--log] [--hide m.k]`, rewrites a line for
a pane's gesture (a knob dragged, a zoom), and resolves it to the same
specification a saved plot holds; `core/plotview.py::figure_from_spec`
draws it, `sensitivity` ranks the knobs at a cursor, `save_plot` is the
one write (`plot save`, undoable), and `console/moxie.py` is the bridge
(`api.moxie`). Because the pane is its line, a layout restores the plot.

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
here: **a sweep writes nothing**. There is no database
in reach to write to. No write-back, no provenance, no scenario motes
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

# Names in an equation

An attribute base that isn't a role is a mote's `name`, searched in two
scopes in order: the anchor's immediate neighbours, then the whole garden
(`core/formula.py`'s `_connected_by_name` and `_named_in_garden`, both
deciding "exactly one or refuse" through the shared `name_match()`, which
`core/dimensions.py` calls too -- the two walks must agree about a name
exactly as they agree about roles).

**The garden scope is what made relationships mean something again.**
Only the neighbour scope existed, so `bd.l` needed `bd` wired to the
rule's anchor, and `FEA-Homework-4` carried six `related_to` edges whose
entire job was making a reference legal. The command bar drew them
automatically, which is the tell: an edge no human would think to add,
added to satisfy the resolver rather than to say anything. A name is
already unique enough to identify a mote to the person writing the
equation. `core/simulation.py`'s `_GardenView.by_name` is the index,
built from the same properties dict the run reads -- so an override mote,
whose properties are stripped from every run, lends no name to resolve
against.

The neighbour scope stays, and stays *first*, because it is now the
answer to a collision rather than the only way to resolve at all: a
garden with two motes called `bd` draws one edge to say which one this
rule means. `unresolved_equation_names()` reports exactly those
collisions for an editor to offer that edge on, not every unwired name:
with the garden scope in place an unwired name isn't broken.

What `applies_to` points at decides a rule's reach, inferred rather than
declared: a species means once per instance, any other mote means once
about that mote. `Rule.scope` carries the resolved answer and
`check_roles` lists every `once` rule, because making the first instance
of a mote silently converts every rule asserted about it.

See `docs/simulation.md` for the reference and its Rankine cycle
worked example.

# Provenance

`why` and `\work` explain a value from a **live solve of the garden as
it stands**, not from anything stored: which equations, reading which
already-known values, determined it. A record kept from "the last solve"
goes stale the moment a given is edited, so nothing is kept. `core/
provenance.py` collects the derivations during an in-memory solve
(`simulation.solve_journal`, writing nothing) and `core/derivations.py`
indexes and replays them, backing the `why` command in the CLI, the
console (`why ‹m.k›`) and the MCP server. An `Explainer` is one such solve,
made the first time it is asked and shared by everything after, so a
document with forty `\work` blocks solves once.

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
what the solver worked in -- unlike a solve's reported changes, which
convert back into each mote's declared unit.

Nothing is stored, so there is nothing to go stale or to undo, and the
answer holds for a scenario or a knob as well (`Explainer(scenario=…,
overrides=…)`). A garden that does not solve answers with the solver's
message instead of an explanation. A garden from before this, which kept
the last solve's record (and before that logged every run), has those
tables dropped on open (`core/db.py`).

# The worksheet

`core/worksheet.py` is a garden's calculation as lines of text — `bd.x = 80
in`, `bd.l = sqrt(bd.x^2 + bd.y^2)`. It writes ordinary motes, rules,
`applies_to` and freedoms, and nothing else; `docs/simulation.md` has the
line grammar.

**It is a projection, never a store.** There is no `worksheet` property and
no saved source text: `read()` renders the garden's equations and their
inputs back as lines, and `apply()` writes lines into the graph. That is
`philosophy.md`'s "editors are shortcuts over the graph, never a separate
store" taken at its word, and the price is paid rather than hidden —
comments, blank lines and line order do not survive a round trip (`read()`
puts its own order back: a block per independent calculation, inputs
first, each equation after the ones it reads). What does
survive is everything that means something: a line's `! name` becomes the
rule's `name`, and every other line is reconstructed from what it wrote.

Two decisions make the round trip *safe*, which is what a projection has to
be before `apply(read())` can be a no-op rather than a loss:

- **`=` declares a freedom; `==` does not.** `=` is the assignment — solve
  for this — and `==` is a relation between values free for some other
  reason. So `read()` renders `=` only where the left side is already a
  freedom, and re-applying cannot widen the model behind the reader's back.
- **A solved freedom is never printed as a given.** It is the answer, and
  `bd.l = 100` would pin the value the worksheet exists to solve for.
  `bd.l = ?` is how an unknown with no number yet is written, and how one
  comes back.

**Applying replaces.** The lines are the whole set of the garden's
`once`-scoped equations, so a rule whose line is gone is deleted — the
editor is an editor rather than an append-only log. A rule pointing at a
*species* is asserted once per instance, which is a reusable model and not a
line; those are neither rendered nor deleted, and counted separately so a
surface can say so.

**A rule a line makes is unnamed.** `core/rules.py::label_of` reads the
equation as the label instead, everywhere a rule is named. A name that
copies its equation is a second account of it that drifts the moment the
equation is edited, which is what `FEA-Homework-4`'s rule names had already
done. `core/query.py::Index.name_of` uses the same fallback, so a table row
reads `bd.l == sqrt(bd.x^2 + bd.y^2)` rather than an id prefix.

# Solve health

`core/health.py::check()` answers "can this be solved, and if not, which
line is wrong" **before anything is solved**. A failed solve says
*underdetermined*, which is true of the whole garden and names nothing.

**The verdict is read off the blocks, never off the totals.**
`core/incidence.py::Index.blocks()` cuts the garden's free values into
coupled groups — two values are in one block when some equation could
determine both, transitively — and a block is the smallest group whose
counts have to balance on its own, since no equation outside it can
determine anything inside it. Three equations and three unknowns may be two
blocks, one short and one surplus, and calling that balanced would be the
most misleading thing this module could say. An equation that determines
nothing is in no block, which is the structural form of "an inequality
cannot make a deficient group solvable".

Everything else it reports already existed and is only assembled here: the
culprits come from `incidence.requires()`'s own stored/derived/missing
distinction, and the unit problems from `simulation.check_garden()`, which
labels each one with its rule — and since a worksheet rule is labelled by
its equation, a units warning already reads back as the line that caused it.
Nothing is re-derived; a second account of what depends on what would drift
from the equations, which is why that index is built fresh per query.

`solvable` is deliberately not "will solve": a balanced block can still be
singular or inconsistent, and only running it can say.

**Display units live in the surface, not the garden.** `roadmap.md` asks for
the stored unit and the displayed one to be told apart. The stored one is
the `units` map, and solving never changes it; the displayed one is view
state, so `core/` offers `core/units.py::convert` and
`core/health.py::stored_unit` and writes no display preference anywhere.
Nothing about a person's current view of a number belongs in their data.

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

See `docs/simulation.md` for the reference.

# MCP server

`mcp_server/` is an adapter over `core/`, alongside `cli/` and
`console/`: it exposes the same repository functions as tools an MCP client
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

Where the console (`console/session.py`) holds one long-lived connection for the open garden,
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

`core/` is the whole system. `cli/`, `console/` and `mcp_server/` are
three adapters over it, and each one's job is a
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
  Monarch, the graph, is the console's `graph` pane now, not a window
  (see The graph below).
* **Mora** -- the same charter as the UI, for one thing: writing and
  reading .modoc notes (see Notes below). It is the console's `edit`,
  `read` and `notes` panes now, not a window.
* **Monastery** -- likewise, for motes that have a time (see The
  calendar below): a week is a shape a graph canvas cannot draw. It is
  the console's `agenda`, `week` and `month` panes now, not a window.
* **Monolith** -- likewise once more, for the garden as a whole rather
  than for any one kind of mote (see The table below): a query across
  every mote and an edit applied to two hundred of them are shapes a
  graph, a page and a week all lack. It is the console's `find` pane
  now, not a window.
* **Motion** -- likewise, for the garden as a system that runs (see
  Simulation below, and `docs/simulation.md`): scenarios, a tick-by-tick
  run and a rule's own bindings. It is the console's `sim`, `rule` and
  `scenarios` panes now, not a window.
* **Home** -- the gardens themselves, rather than anything inside one:
  the ownership tree, and making, renaming, removing (restorable),
  archiving, importing, exporting and syncing a garden. It was its own
  window, Gardens, because it was the only one that opened on no garden
  at all; it is the console's `gardens` and `garden` panes now
  (`core/home.py`, see Home below). A garden-level act is a `garden …`
  command outside any garden's undo, and the garden that is open is the
  one thing it refuses to remove.
* **Console** -- the one tiled Mosaic window the windows above are being
  ported into (`docs/console.md`; `run_console.py`, `console/`). Its
  root bridge (`console/api.py`) owns the single garden connection,
  lock and undo stack every ported app's namespace will share, and
  answers the command line: `core/commands.py` parses a line as an app
  suffix, a verb or a `core/command_bar.py` statement, so the grammar
  stays in `core/`. Every write a line makes comes back as one undo
  step: a verb's from `core/batch.py`-style commands, a statement's
  from `core/command_bar.py`'s journal, which records the inverse of
  each primitive write as the line runs -- and takes them all back if
  the line fails halfway, so a statement never lands half-written.
  Layouts and command history are UI state, kept in a per-garden sidecar
  (`gardens/<name>.db.console.json`) that follows the garden through
  rename, trash and restore and never syncs.
* **MCP** -- reads plus graph edits (above). Deliberately not: writing a
  simulation to the graph, deleting or restoring gardens, Calendar
  export, opening links.

A capability missing from a surface its charter covers is a gap to
close, not a decision. A capability missing from a surface its charter
excludes should say so, here, in one line.

# Importing measured data

`core/csv_import.py` brings a CSV file in as **one mote per row**. The
header names the properties, a unit rides in brackets (`load [kN]`), and
a header is made a key by turning its spaces into `_` (`mesh size [mm]`
is `mesh_size`, in millimetres) so an equation can name it. A `name`
column names the rows; otherwise they are `<import> 1`, `<import> 2`...

Every value is marked `source: {kind: measured, note: <file>}`, and every
row is linked `imported_from` one import mote carrying `file`, `rows` and
`columns`, so the rows can be found, and undone, as the one thing they
were. `imported_from` is an ordinary relationship type: nothing solves
or renders differently because of it.

An empty cell is no value, not a zero. `nan` and `inf` stay text, since
no measurement is either and neither survives JSON.

`parse(text, file)` is pure and never raises; each problem is a line
(an unknown unit, a repeated or reserved column, a row wider than the
header). `apply(conn, parsed)` refuses a file with *any* problem, because
half an import looks complete. It returns one undo Command for the lot.

`monoss import csv <file> [--dry-run]`; MCP `import_csv`.

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

No property name is reserved for it. How long and how often live inside
the start's value, because a bare `30` is not distinguishable from any
other number, so a span or a rule kept as a separate property could only
be found by name. One self-describing value is what lets the calendar
know no name at all. `date` survives only as the key a
*new* mote's start is written to (`core/calendar.py::DEFAULT_BINDING`), a
default rather than a name anything reads.

A garden that still has a separate `date`/`duration`/`ends`/`recurrence`
is folded on open (`core/db.py::_migrate_intervals`), idempotently,
**keeping each garden's own key name** -- `date` stays `date`, `when` stays
`when`. An all-day `ends` was inclusive as people wrote it and becomes an
exclusive interval, the end `core/calendar.py` always computed. Durations use days/hours/minutes/seconds only: years and months have no
fixed length, so an event lasting "one month" has no single answer.

The one thing this costs: hand-typing a separate `duration` beside a date
does nothing. The calendar panes are the mitigation: a move
or a resize there writes the interval for you (`core/agenda.py`), so the
notation is typed only to make a date in the first place.

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

The **calc sections** are the same idea one size up. `\given`,
`\answers`, `\assumptions` and `\checks` take no argument at all: what
belongs in a document's given list is a property of the *garden* -- the
numbers its equations read and are not free to determine -- and a note
that had to list them would be the copy this format exists to avoid.
`core/calc.py` answers all four, plus `\work`, which renders
`core/derivations.py`'s live explanation as three lines: the
equation, the equation with values in it, the result. It is the same
explanation `why` reads, so a document and a debugger cannot come to disagree
about how a value came out -- and a garden nobody has solved has no work
to show, which the macro says rather than deriving one of its own.

Two decisions inside `\work` are worth naming. Substituted values are in
each mote's **declared** unit, not the base SI the solve worked in: the
person who wrote `80 in` has to see 80, and the unit written beside the
number is what makes the line correct. That does not multiply out when a
line mixes `kohm` and `mA`, so where one line mixes units of a single
dimension they are converted to one and the line says which -- every line
is then either checkable by eye or explicitly labelled. And a value
several equations determined *together* gets one symbolic line each and
no substituted line at all, for the same reason `why` stops at a solver
block: there is no one expression to put numbers into, and inventing one
would imply an attribution the solve never made.

Where a value came from is the reserved `source` property, a per-property
map in exactly the shape `units` has (`{"x": "assumed"}`). One key covers
`assumed`, which `\assumptions` lists, and `external`, which a
verification table compares against a hand calc -- so the V&V work falls
out of the same convention rather than needing its own. The solver never
reads it: where a value came from changes nothing about how it is used.

The **calc sections** are the same idea one size up. `\given`, `\answers`,
`\assumptions` and `\checks` take no argument at all: what belongs in a
document's given list is a property of the *garden* -- the numbers its
equations read and are not free to determine -- and a note that had to list
them would be the copy this format exists to avoid. `core/calc.py` answers
all four, plus `\work`, which renders `core/derivations.py`'s live explanation
as three lines: the equation, the equation with values in it, the result. It
is the same explanation `why` reads, so a document and a debugger cannot come
to disagree about how a value came out. The result line is the live one: if a
given was edited after the last write-back, the stale stored answer is not
printed under a substitution that no longer adds up to it.

Two decisions inside `\work` are worth naming. Substituted values are in
each mote's **declared** unit, not the base SI the solve worked in: the
person who wrote `80 in` has to see 80, and the unit written beside the
number is what makes the line correct. That does not multiply out when a
line mixes `kohm` and `mA`, so where one line mixes units of a single
dimension they are converted to one and the line says which -- every line is
then either checkable by eye or explicitly labelled. And a value several
equations determined *together* gets one symbolic line each and no
substituted line at all, for the same reason `why` stops at a solver block:
there is no one expression to put numbers into, and inventing one would
imply an attribution the solve never made.

Where a value came from is the reserved `source` property, a per-property
map in exactly the shape `units` has (`{"x": "assumed"}`). One key covers
`assumed`, which `\assumptions` lists, and `external`, which a verification
table compares against a hand calc -- so the V&V work falls out of the same
convention rather than needing its own. The solver never reads it: where a
value came from changes nothing about how it is used.

`\graph{...}` is the same idea for a figure, and is the one macro that
resolves to something another *surface* made: it names a plot saved
with `plot save`, and `core/plotview.py` re-solves that view's saved specification
against the garden as it stands. A note therefore never describes a
figure of its own -- there is one place a figure is designed, one place
it is computed (`core/sweep.py`), and one figure payload
(`plotview.figure_json`) that the console's renderer
(`console/frontend/js/plot.js`) draws in a plot pane and in a note's
read pane alike. The note refers.

`\image{...}` is the other figure: a picture rather than a computation. It
names a mote carrying the reserved `image` property (`core/images.py`:
PNG or SVG, base64 in the mote, 4 MB cap, SVG cleaned on the way in), so
it rides sync, export and undo with no machinery of its own. Everything
that reads or writes the bytes goes through `core/images.py`, which is the
seam where side-file storage would replace base64 if it ever has to.
`core/document.py` ships the picture to the read pane as a data URI.

**Output is a projection, and a frozen copy of one.** `core/texexport.py`
writes a note's resolved blocks (the read pane's own, `core/document.py`) as
`.tex`; `core/tectonic.py` compiles it; `core/submit.py` freezes the result
as a `submission` mote (reserved property: `.tex`, PDF, figure files and the
quoted values, base64 like an image) and stamps the project's owner mote
with `status: submitted` and the revision. Nothing is stored that cannot be
re-derived except the frozen revision itself -- which is the point of
freezing it. See `modoc.md` -> *Output*.

**A document can span several notes, and is numbered as one.**
`\include{Method}` is the only macro that changes the *document* rather
than resolving to something inside it, so it is not grammar:
`core/modoc_bind.py::assemble()` needs the garden to find the note, and
`core/modoc.py::numbered()` then counts over the assembled result -- an
equation in an included section is Equation 3 of the document, not
Equation 1 of its own note, and a `\ref` crosses the splice in either
direction. That is also why numbering is a separate pure pass rather than
something the parser does on the way past: it has to be safe to run twice,
since each piece is parsed (and numbered) before the whole is assembled.
A cycle and a chain over eight deep are refused with a diagnostic.

**A cross-reference is a fact about the text, not about the garden.**
`\label`/`\ref` resolve in `core/modoc.py` with no connection in reach,
which is why `reference_text()` lives there and every surface can call it.
Three counters, as in LaTeX -- an equation, a figure and a table are
numbered independently, because a reader looking for Figure 2 should not
have to know how many equations came before it. A bare `\graph` earns no
number: it is an illustration between two paragraphs, and only a `figure`
environment makes it something a reader is sent to.

**`\rule{ΣFx}` cites a rule**, printing the equation the graph currently
holds as a numbered equation. A document that retyped it could drift, which
is exactly what `FEA-Homework-4`'s rule names had already done. Only a mote
that really is a rule is citable -- an `equation` *and* an `applies_to` --
since quoting a half-built one would be the document saying more than the
garden does. The rule's own name is its label, because it already has one.

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
notes are inside sync, garden export/import, the UI's undo, and
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
stands, and there is no step to ask for. The line is the one the panes
are divided on: a note reports what a garden *is*, `sim` is where a garden moves through
time, and `plot` is where a property is swept. A note editor that could
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

Notes are written in the console (`layout docs`: `notes | edit @sel | read
@sel`, docs/console.md → *Settled for U6*). The read pane parses nothing
itself: `core/document.py::render` hands it the whole document --
every block `core/modoc.py` parsed, with what `core/modoc_bind.py`
resolved it to inlined -- which is why the pane and `monoss modoc
render` cannot come to disagree about what a note means. Only the
editor's syntax highlighting runs in JavaScript, colour being
presentation rather than meaning; the list of commands it knows comes
from `core/modoc.py`.

The read pane renders the editor's *draft*, not the stored note, so it
follows unsaved typing (`modoc_bind.assemble(..., source=)`); figures
are memoised until the garden changes (`core/document.py::FigureMemo`).
A note saves on every pause in typing, and one editing session folds
into one undo step (the session's `UndoManager.push` coalescing key).
# The calendar

`core/calendar.py` places motes in time, and the console's calendar
panes draw them (`agenda`, `week`, `month`, over `core/agenda.py`;
docs/console.md → *Settled for U7*).

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
A new event (`n`) always writes `date`, the conventional name. Reading is general because a garden is; a *new* date has one right
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
is tested without a window, and `core/agenda.py::edit_line` turns the
answer into the `set` line the pane runs -- so every move and resize is
in the history, the log and undo like any other write. Move and resize
are one function because the top edge of an event is both at once.

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
same thing without a second kind of event. It is the one edit a `set`
can't say, so it is its own verb, `detach` (`core/agenda.py::detach`,
one undo step). Because one mote *is* the whole series, the pane asks
which was meant rather than guessing (`t` this one, `s` the series) --
the only question the calendar puts in front of a write.

Everything in `core/calendar.py` is pure: motes in, occurrences out, no
connection. That is the discipline `core/scenarios.py::apply()` and
`core/incidence.py` already follow, and it is why the whole calendar is
testable without a window (`tests/test_calendar.py`).
`core/agenda.py` adds what a pane's line says -- which range, which keys,
which species and conditions -- and the bars: a multi-day value, or a
start (`start`/`starts`, else when the mote's `garden` was written)
through a `due`.

Two exclusions are inherited rather than reinvented. Templates and
scenario overrides are never placed, even when dated, because they hold
replacement values rather than real data. And a mote is coloured by its
species through the same palette the graph canvas uses, so it reads the
same colour everywhere. See `docs/ui-setup.md` → *Calendar*.

# Species colour

A mote is coloured by its species, in all six windows, and the rule is
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
(`core/inspect.py::species_ids`) rather than the ones a pane can see:
the graph under a hop limit draws a subgraph, and a palette claimed from
a subgraph would give one species two colours in two panes. Every
pane's main payload carries that list, so no pane pays a call for it.

# The inspector

One mote, one surface. The console's `props` pane and its `p` float
draw the same payload, `core/inspect.py::inspect`, so they cannot
disagree about what a mote is (docs/console.md -> *Settled for U3*).

What you may do to a mote does not depend on which pane you are in:
every pane that shows one reaches the same commands.

**Every edit is a command.** `set`, `rename`, `unlink`, `A -> type ->
B` and `template` are what the props pane runs, so an edit is in the
history, the log and undo; the old panel called bridge methods directly.

**A species is a relationship, and the pane says so.** `instance_of` is
listed like anything else. Setting a species is adding that
relationship, in the same picker as any other. The template action is
not a restatement of anything: it declares that *this* mote is the one
new instances start from.

**Types come from the garden, not from a table.** The payload gives a
typed editor per property -- text, number, date, boolean, enum, or a
link that walks to another mote -- decided from the value's shape and
the mote's species, never from a hardcoded vocabulary of key names. An
enum is a key whose values, across the instances of that species *and
its template*, are a small repeated set; the template counts because it
is the species' own statement of what a value looks like. The "usually
has" hints are the template's keys this mote lacks. A species with no
template offers none, which is honest: nothing in the garden has said
what that kind of mote usually carries.

**Writing under a scenario is a command.** Looking at a hypothetical
must never mean overwriting the garden's own state (see Scenarios), so
the console never redirects an ordinary write: `set --scenario hot
C1.voltage 30` says where the value goes, and writes that scenario's
override (`core/commands.py`).

# Dates

A date in MonOSS is an ISO 8601 interval (`core/interval.py`): a start,
an optional span after a `/`, and optional RRULE lines beneath it. Any
property holding one is a date **by value shape, not by key name**, so
`when`, `due`, `starts` or whatever this garden calls it all show on the
calendar panes, repeating or not.

In the console a date is written as that text (`set Lab date
2026-10-08T13:00/PT2H`), or moved and resized on the calendar panes,
whose every edit is the `set` line the move means (`core/agenda.py`).
No JavaScript reads an RRULE: expanding a recurring
value into occurrences is `core/calendar.py`'s job, via dateutil.

# The table

The garden as rows and columns: where large edits happen -- forty motes
given an owner in one gesture -- and where structured questions get asked
across everything at once, rather than one mote at a time. It was a
window of its own (`monolith/`) and is now the console's `find` view
(`docs/console.md`, step U2); **all of it lives in `core/`**, in five
modules, because the console, the CLI and an MCP tool all have to agree
about what columns a garden has and what "set `status` on these forty"
does:

| module | subject |
| --- | --- |
| `core/query.py` | the query language: fields, operators, joins, order, grouping |
| `core/table.py` | the discovered schema, and one query's rows |
| `core/findtext.py` | that query as one typed line, both ways |
| `core/batch.py` | one edit applied to many motes, as one undo step |
| `core/views.py` | a query kept under a name |

The console's side is thin. `console/monolith.py` is the `api.monolith`
bridge: `table(command)` reads a `find …` line (filling in the garden's
default columns when it names none, and `--view` from a saved view) and
answers `core/table.py::table`'s payload plus `sort` and `group` as column
keys; `rewrite(command, change)` turns a header click or `g` into the new
line through `core/findtext.py::rewrite`, so JavaScript never writes find
syntax. Edits are commands, not bridge calls: a cell edit runs `set @sel
<key> <value>` (the whole selection) or `set #id …`, which
`core/commands.py::run_set` applies through `core/batch.py` as one undo
step. Every column a table shows has a written form -- which is why the
default Species column is `type` (`instance_of`), sorting and grouping by
species name -- and `core/table.py::field_of` reads a column key back.

**Columns are discovered, never declared.** A garden has no schema, so
there is nothing to read a column list off. `core/table.py::Schema`
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
grouping -- and a table is only an editor for it, the same split
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

**Two gestures write relationships in bulk.** The relationship matrix
puts the visible rows on both axes and writes or erases one edge per
click, painting the type selected above it; and the bulk bar chains a
selection in row order or fans it into its first row. Thirty imported
motes into a dependency chain is one gesture here and twenty-nine in the
graph window.

**A bulk edit is ordinary writes, and one undo step.** `core/batch.py`
holds all three (`set_property`, `clear_property`, `delete_motes`), and
three rules hold for each:

- **One primitive write per mote**, through `core/motes.py`, so a
  two-hundred-mote edit is two hundred oplog rows that `core/sync.py`
  replicates like any other. There is no bulk path in the store for a
  surface to have invented.
- **One undo step.** Each returns a single `undo.combine`d Command, since
  somebody who bulk-deleted a selection expects one Ctrl+Z rather than two
  hundred presses. The Command is *returned* rather than pushed: the undo
  stack belongs to whichever surface is open, and core has no business
  knowing which. That is also what makes deleting safe to leave
  unguarded -- a dialog is the wrong shape for a selection that might be
  one row or two hundred, and one Ctrl+Z is a better answer than one
  refusal.
- **A missing id is skipped, not fatal.** A selection outlives the rows it
  was made on, so writing the other 199 is right and raising is not.

`set_property` reads its value through `core/command_bar.py::parse_value`
-- the same reading `R1.x = 500 ohm` gets -- so typing `90` into a
duration column stores a number rather than a string `core/calendar.py`
would ignore, and typing a unit annotates the mote's `units` map instead
of storing the word. A second, unit-blind reading of the same keystrokes
is how one surface comes to disagree with another about what was typed.

**A query is also one line of text.** `core/findtext.py` reads
`status:todo due:<7d --sort -updated` into a `Query` and writes one back
out, and `format_find(parse_find(text)) == text` for canonical text. The
round trip is the reason it is a module rather than a regex in a bridge: a
console echoes the line you typed, a saved view shows the line that
reproduces it, and a completion extends the line you are halfway through
-- three things that only agree if one function answers what a query
*reads* as.

    key:value      is            (includes, on a list-valued column)
    key~value      contains
    key:>value     greater       (after, on a date column)
    key:<value     less          (before, on a date column)
    key!:value     is not        (excludes)
    key:?          is empty
    key!:?         is not empty

    or             join with OR rather than AND
    --sort key     ascending; --sort -key descending
    --group key
    --cols a,b,c
    --view name    start from a saved view; conditions here add to it

`type:`/`species:` ask what `instance_of` answers; `kind:` and `related:`
are the other graph fields; `name:`, `id:`, `degree:` and `updated:` are
the builtins; `->type:` and `<-type:` are a relationship in one
direction; anything else is a property key. Quoted values may contain
spaces.

**What a condition *means* is decided when it runs, not when it is
written.** A garden has no schema, so `estimate:>3` cannot know whether it
is a numeric `>` or a lexical `after` until somebody looks at what
`estimate` holds. So the parser records an **intent**
(`core/query.py`'s `IS`/`NOT`/`CONTAINS`/`GREATER`/`LESS`/`EMPTY`), and
`core/table.py::resolve_query` turns it into the operator the discovered
kind accepts at evaluate time -- along with resolving a relative date
(`today`, `7d`, `-3d`, `2w`, `1m`), which is stored verbatim for the same
reason. That is what keeps a saved view meaning what it said: `due:7d` is
the next week whenever it is opened, and `estimate:>3` becomes a numeric
comparison the day estimate stops being prose. An intent a column cannot
answer (`kind:>x` asks a list of kinds to be greater than something)
drops the condition and says so in the answer's `problems`, rather than
quietly meaning something else.

A date named without a time is a question about the day, so `is`, `before`
and `after` compare on the date prefix (`core/query.py`'s
`_same_precision`): `due:2026-10-01` matches a mote due
`2026-10-01T09:00`, and that mote is not *after* the 1st.

**A saved view is an ordinary mote** carrying a `query` property
(`core/views.py`) -- the one reserved name this subject adds. Like
`modoc`, carrying one is data rather than a job in the graph's machinery,
so it is deliberately not a `core/kinds.py` kind: a view mote that read as
a kind would displace its real species in every other surface's type
label. Being ordinary is what makes a view sync between devices, appear in
the graph, and be linkable from a note. Every saved view *is* declared an
instance of a `Saved View` species -- an ordinary mote, found or created by
name rather than reserved, so a garden that has never saved a view never
grows one.

The query being looked at is the pane's command, so it is saved with the
layout (`console/state.py`), never in the garden. See `docs/ui-setup.md`.

# Simulation

Simulation is the console's sim panes (`layout calc`, docs/console.md →
*Settled for U5*): `scenarios | sim | rule @sel`. It was a sixth window
(`motion/`), and before that a settings popover, a scrub bar and a runs
panel on Monarch, gating every graph edit behind "commit or discard
first"; both are gone.

**Nothing about the engine moved.** `core/simulation.py` answers every
run; `core/motion.py` is the console's grammar over it (`sim`, `rule`,
`why`, `solve`) and shapes its answers, and `console/motion.py` is
`api.motion`, which only calls it. The MCP server still takes a
preview's steps in `core/simulation.py::step_payloads`' shape.

**A run is a watch list, not a board.** A mote carries no coordinates
and no declared role in a simulation, so rather than derive a node
board, the sim pane shows the values a run is about: one row per value
with its state (`solved`, `varied`, `pinned`, `given` -- facts core
already answers: the run's changes, the scenario's overrides, declared
freedoms), and the row's whole series, so a playhead scrubs without a
second solve. A graph of the model is the `graph` pane (U8).

**One playhead.** The playhead is shell state (the store), like the
selection: every Motion pane reads the same tick, and the sim pane that
last ran or was focused owns it, publishing its run so the rule pane can
ask for its bindings' series under the same run. Not saved in commands.

**The timeline counts ticks, and says so.** A tick is one re-solve of
the equation set; the engine has no `dt`, no seconds and no integration.
`--ticks`, the timeline and the top bar all count ticks. The solver list
is read from `core/solver_registry.py`: a future integrator appears in
`sim --solver` by being registered.

**`solve` and `live`.** `solve` writes one static solve as one undo
step (`why` is live, so it needs no record put back). `solve --live on` folds a solve
into every write made from the window (`core/motion.py::live_solve`),
kept per garden in the console sidecar -- UI state, like layouts.

# The graph

The console's `graph` pane (`graph`, `layout graph`) is Monarch's
canvas renderer kept and restyled, not Cytoscape (docs/console.md ->
*Settled for U8*). `core/graphview.py` answers the line:

    graph [@sel|#id|‹name›] [--depth N] [--hide-rel a,b] [--view ‹saved›] [‹conditions›]

A root draws its neighbourhood -- every mote within `--depth` hops
(default 2), relationships counted both ways -- and no root draws the
whole garden. Find conditions and `--view` keep what they match, as in
`find`, and the root always stays. A hidden relationship type is neither
drawn nor hopped through, so `--hide-rel instance_of` stops a species
pulling in every instance it has. `[` and `]` rewrite `--depth`, so a
layout reopens on the same neighbourhood.

**The layout is species-clustered.** Each species owns a direction from
the centre, its instances cluster on that spoke and the species mote
sits further in on it; edges across species pull only weakly, so the
grouping carries the layout. Positions are seeded from a hash of the id,
so a garden lays out the same way each time it opens. The loop stops
once the layout settles rather than drawing every frame
(`console/frontend/js/graph.js`).

**Pins are UI state.** Dragging a node pins it there. Pins are kept per
garden in the console's sidecar (`console/state.py`, `pins`), beside the
layouts and history -- never in the .db and never synced, like every
other position: a mote carries no coordinates.

**Every edit is a command.** The pane writes nothing itself: ctrl+drag
from one node to another puts `A ->  -> B` on the command line with the
caret where the type goes, `n` puts `‹cursor› -> related_to -> `, and
`d d` runs `delete ‹cursor›` (or `unlink …` for an edge). They run, and
undo, like anything typed.

**Annotations are gone.** Monarch's sticky notes, group boxes and pen
strokes were commentary layered over its canvas, stored in their own
`annotations` table and never synced. They went with the window, and
`core/db.py` drops the table when a garden is opened. Commentary about
a model is a note (see Notes).

# Home

The console's gardens panes (`layout gardens`: `gardens | garden`, docs/console.md ->
*Settled for U9*) is the ownership tree of `core/projects.py` drawn as a
pane, and the garden manager.

`core/home.py` answers both panes and the `garden` subverbs. `gardens`
lists owned gardens under their owner's garden with the cover's name,
`due` and `status`, then loose gardens, then archived ones folded until
`--archived`. Reading it opens every garden, so it runs on a pane's run,
never per keystroke. A `.db` that isn't a garden still lists, uncounted:
the ownership and archive scans skip what they cannot read.

**Garden acts are commands outside undo.** `garden new|rename|rm|restore|
archive|unarchive|export|import|sync` act on gardens, not on the open
garden's motes, so none is on its undo stack; `rm` moves the file aside
(typed, a second ⏎ confirms it; the tree's `d d` is its own
confirmation) and `garden restore` brings it back. Removing
the open garden is refused; renaming it reopens the session under the
new name (`console/session.py::Session.rename`). Import always lands in
a new garden and removes it again if the file fails. Without a path,
export and import ask with a native file dialog.

**`project start|stop`** is the UI of phase 4's *Start project*: a write
verb on the open garden, one undo step, which never deletes a garden.

**The breadcrumb** is the open garden's ownership path
(`core/home.py::path_of`), carried in the boot payload; a crumb above
the open garden `cd`s there.

# Focus and the clipboard

`console/frontend/focus.js` is loaded first and installs
itself -- there is nothing to configure. It owns two window-wide rules
that would otherwise be re-implemented per pane and per shared widget,
each over its own subtree, with gaps between them:

- **Middle-click never pastes.** WebKitGTK runs X11's primary-selection
  paste on the middle button's *mouseup*, into whatever element has
  focus, not the one under the cursor. So guarding only mousedown, or
  only one subtree, let a middle-drag pan across a canvas paste into a
  field focused elsewhere (a field being edited). `focus.js` preventDefault()s both events in the capture phase
  on `window`. Middle-drag panning reads `button === 1` itself and never
  needed the default action.
- **Clicking outside a text field unfocuses it.** Browsers do this only
  when the mousedown's default action runs, and every canvas, drag
  surface, or resize handle that preventDefault()s its mousedown kept a
  stale field focused. `focus.js` blurs the focused field on any
  mousedown (capture phase, so `stopPropagation()` can't skip it) that
  isn't inside that field, on another focusable control, or inside an
  element marked `data-keep-focus` -- the explicit opt-out for a popup
  that must leave focus where it is.

No view should add its own middle-click or click-away handlers.

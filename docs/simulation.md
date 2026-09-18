# Simulation

MonOSS can solve for unknown mote properties from equations you write yourself. The engine knows nothing about circuits, thermodynamics, or any other domain -- all of that lives in ordinary motes.

Simulation has its own window: **Motion** (`run_motion.py`, and
`docs/ui-setup.md`). Press **Run** to preview a time-based run -- every
tick is solved in memory and nothing is written to the garden yet -- and
scrub the timeline to step or play through the sequence, with each
node's value at that tick on its card and every value that moved traced
as a lane in the dock. **Commit tick N** re-runs to that tick and writes
it for real; **Save** logs the whole run for later review without
touching the graph. A scenario run can never be committed, since it
answers what *would* happen.

> This used to live in Monarch, as a settings popover and a bottom scrub
> bar. It moved out wholesale: Monarch is a graph editor, and simulation
> is a subject that needs a window rather than a corner of one.

See "Time-based simulation" below.
Programmatically, a single steady-state solve is
`core.simulation.run_simulation(conn)`; a time-based run is
`core.simulation.run_time_series(conn, step_count, commit)` (see "Run
history" below for its `save` option).

A third question -- how one value moves as another changes -- has an
answer shaped like a curve rather than like a state, and so has its own
module and its own window: `core.sweep.run_sweep()`, and **Moxie**
(`run_moxie.py`). See "Sweeping" below.

## The three pieces

**Freedoms** are what the solver is allowed to determine, declared via a `freedoms` property:

```
name:      Node
freedoms:  voltage
```

`freedoms` accepts a comma-separated string (`voltage, current`) or a JSON list (`["voltage"]`).

Where you put it decides who it is about, by the same rule `roles` follows:

- On a mote **acting as a species** (something other motes point at with `instance_of` or `template_of`), it declares what every *instance* is free to have determined. Every mote with an `instance_of` relationship to the Node above now has a solvable `voltage`.
- On any **other mote**, it declares what that mote itself is free to have determined -- which is how a mote that is an instance of nothing can be solved at all.

A mote's own declaration **replaces** what its species declare rather than adding to them, so a mote can narrow its freedoms as well as widen them:

```
name:      R1
freedoms:  current      # ask this one resistor the inverse question
voltage:   12
```

That is the point of moving the declaration off the species. The same equation, `voltage == current * resistance`, sizes one resistor and rates another in the same garden, without editing what a Resistor *is*. Freedoms declared only on species behave exactly as they always have; several species a mote belongs to merge, since nothing orders them.

> `freedoms` was called `unknowns` until the declaration moved off the species. Gardens are migrated on open (`core/db.py`), scoped to motes acting as a species -- an ordinary property a user happened to name `unknowns` is left alone. The solver's own vocabulary is unchanged: `core/formula.py` and both backends still say *unknown*, because at that layer it is the right word.

**A rule** is a mote with an `equation` property and an `applies_to` relationship pointing at a species:

```
name:      Ohm's law
equation:  targets('a').voltage - targets('b').voltage == current * resistance

  --applies_to--> Resistor
```

The rule is evaluated once per instance of that species, with bare names resolving against that instance.

**Instances** are motes with an `instance_of` relationship to the species. Give them their known values; the solver writes the rest.

```
name:        R1
resistance:  100          <- you provide this
current:                  <- the solver writes this
```

## Expression language

Every relation is `left <op> right`, with exactly one comparison. `==`
makes it an **equation** the solver satisfies; `<=`, `>=`, `<` and `>`
make it a **constraint** the solver only checks (see
[Comparison relations](#comparison-relations)). Everything below applies
to both.

| Syntax | Meaning |
|---|---|
| `resistance` | A property of the mote the rule is running against. If it is among the mote's [freedoms](#the-three-pieces), it becomes something to solve for; otherwise it must already hold a number. |
| `turbine.work` | A property of the mote filling the declared role `turbine` (see [Roles](#roles)). A bare name is always a property; a name in this position is always a role. |
| `R1.current` | A property of a mote *connected* to the anchor -- any relationship type, either direction -- whose own `name` matches `R1`, tried when the identifier isn't a declared role (see [Connected-name references](#connected-name-references)). |
| `targets('a')` | The motes this one points at via a relationship of type `a`. |
| `sources('a')` | The motes pointing *at* this one via a relationship of type `a`. |
| `targets('a').voltage` | That property on those motes. Collapses to a single value when exactly one mote is reached -- unlike an `arity: many` role, which never collapses. |
| `sum(...)`, `count(...)` | Combine several motes into one value. Required when more than one mote is reached. |
| `+ - * / **` (`^` also works as exponent) | Arithmetic. Parentheses work as usual. An unknown may appear in a denominator -- see [Writing an equation the way you mean it](#writing-an-equation-the-way-you-mean-it). |
| `pi`, `e` | The math constants (3.14159..., 2.71828...). Reserved words -- shadow a property literally named `pi` or `e` inside a formula specifically, the same way `t` does. |
| `abs`, `min`, `max` | Only on values that are already known. |
| `sin`, `cos`, `tan`, `sqrt` | Also only on known values. `sin`/`cos`/`tan` take radians -- an argument declaring unit `deg` (see [Units](#units) below) arrives already converted; `sqrt` of a negative number is an error. |
| `t` | The current step's index within a time-based run (0 for the first step). A reserved word -- shadows a property literally named `t` inside a formula specifically, though that property is still ordinary and visible everywhere else. Always 0 in a plain (non-time-based) run. |
| `prev(x)` | The value `x` had *entering* the current step -- `x` must be a bare property/unknown name on the mote the rule is running against (not `targets(...)`'s result). This is how a rule expresses a recurrence: `balance == prev(balance) * 1.05`. Only meaningful in a time-based run; raises an error otherwise. |
| `property('rel', 'operation', ...)` | Evaluates `operation` (e.g. `'pressure'`) through the property model of the single mote reached by relationship type `'rel'`, e.g. `property('working_fluid', 'pressure', density, temperature)`. The relationship type and operation must be quoted string literals; the remaining arguments must already be known numbers -- a property model is an opaque function of its inputs, so like `abs`/`min`/`max` this is deferred (retried by the substitution loop) until they are. See "Physical property models" below. |

Anything else -- indexing, comprehensions, attribute access, `import`, calling arbitrary functions -- is rejected when the formula is parsed. The parser uses Python's `ast` module against a whitelist and never calls `eval`.

## What the solver can handle

### Writing an equation the way you mean it

An equation does not have to be arranged so the unknowns sit where the
solver would like them. Divide by one:

```
I / I0 == 0.1
```

Nothing about that is linear as written -- `I0` is under a division bar
-- but it says the same thing as `I == 0.1 * I0`, and the engine does
that rearrangement rather than asking you to. Each side of a relation is
carried as a numerator over a denominator, and the denominator is
cleared once, against the relation as a whole. So all of these are
ordinary linear equations here, solvable by the default `linear` solver:

```
I / I0         == 0.1
1 / resistance == 1 / r1 + 1 / r2      <- resistors in parallel
sources('feeds').pressure / pressure == 0.25
mass / volume  == density
x ^ -1         == 4
```

Two caveats, both of which follow from what the algebra can and can't
promise:

- **It cannot make a genuine nonlinearity go away.** `x / y == z`, with
  all three unknown, clears to `x == z * y` -- still a product of two
  unknowns. That defers to the substitution loop and then to the selected
  solver, exactly as it did before.
- **A constraint with an unknown denominator waits.** Multiplying
  `a / x <= b` through by `x` flips the comparison when `x` is negative,
  so it is not cleared. By the time constraints are checked the unknown
  is normally solved and the denominator is just a number; if it genuinely
  isn't, the constraint is reported as **not checked** rather than
  guessed at.

Writing `a / x` asserts that `x` isn't zero, and clearing the denominator
takes you at your word. Dividing by a literal zero is still an error, and
an equation whose numerator and denominator vanish together (`(x - 1) /
(x - 1) == 1`) is read as the cleared form rather than refused.

### The two stages

Solving runs in two stages:

1. **Substitution.** Any equation that has come down to a single unknown is solved outright and the answer fed back in, repeatedly. This is what makes a nonlinear formula work when its nonlinear part involves values that are already known:

   ```
   pressure    == sources('feeds').pressure * ratio
   temperature == sources('feeds').temperature
                  * (pressure / sources('feeds').pressure) ** 0.286
   ```

   The first equation determines `pressure`, which lets the second one evaluate. This stage is the same regardless of which solver is selected below -- it's free, exact, and handles the common case with no numerical error at all.

2. **The selected solver.** Whatever the substitution loop couldn't finish -- a still-linear system of several unknowns together, or an equation genuinely nonlinear *in the unknowns themselves* (`x * x == 4`) -- goes to the backend named by `solver` (see [Solvers](#solvers) below). Feedback loops are fine here -- a circuit's node voltages and branch currents all depend on each other and are found together.

## Solvers

`run_simulation`/`run_time_series` (and Motion's `preview`/`commit`/`save`) all take an optional `solver` (a name) and `solver_configuration` (a dict of backend-specific settings), the same pattern [physical property models](#physical-property-models) use for `provider`/`configuration` -- a pluggable, named backend rather than one hardcoded algorithm. Unlike a property model, solver choice can't be attached to a single mote: solving happens over the whole coupled equation system at once, so it's picked per run, not per mote.

- **`linear`** (the default). Exact, non-iterative least-squares solve of whatever's left after substitution. Fails with **could not be resolved** if any equation is genuinely nonlinear in its unknowns.
- **`newton`**. Finds a root of the combined residual by Newton-Raphson with a numeric (finite-difference) Jacobian, so it can solve the cases `linear` can't -- an equation of state, a nonlinear structural relation, a `property()` call whose input is itself unknown. Being iterative, it needs a starting guess for each unknown (the mote's existing property value if there is one, otherwise `1.0`) and can fail to converge or find a different root than expected for a badly-conditioned or multi-rooted system. Configuration: `max_iterations` (default 50), `tolerance` (default `1e-9`), `epsilon` (the finite-difference step, default `1e-6`).

## Errors

The engine refuses to guess. All of these stop the run with a message naming the motes involved:

- **underdetermined** -- more unknowns than independent equations. Usually a missing rule or a missing reference point (a circuit needs a ground).
- **inconsistent** -- no values satisfy every equation at once.
- **contradictory** -- two rules demand different values for the same thing.
- **could not be resolved** -- an equation stayed nonlinear in its unknowns and the `linear` solver was selected. The message names `newton` as the alternative, and suggests restating the rule where a linear form exists (an attenuation law `I == I0 * exp(-k*x)` is `k * x == -ln(I / I0)`, which is exact and needs no starting guess).
- **did not converge** / **stalled** -- the `newton` solver ran out of iterations, or backtracking couldn't improve the residual further, without reaching `tolerance`.
- **singular system** -- the `newton` solver's final Jacobian didn't independently constrain every unknown (the nonlinear analog of underdetermined).
- **failed while evaluating a rule** -- an iterative solver's guess reached a point where the arithmetic itself is impossible: a power too large to represent, zero to a negative power, or a negative base under a fractional exponent. The rule is usually fine and the starting guess is not -- `e ** (-k * x)` overflows once x is driven far enough negative, so seed the unknown near its expected value.

Being overdetermined is *not* an error, as long as the extra equations agree. This is normal: in any circuit, one of the Kirchhoff current equations is always implied by the others.

## Worked example

`scripts/example_voltage_divider.py` builds a complete circuit in a garden called `circuit-example`:

```
.venv/bin/python scripts/example_voltage_divider.py
```

A 10 V source across 100 Ω and 200 Ω in series, modelled with four rules -- Ohm's law, Kirchhoff's current law, a source law, and a ground reference. Terminal convention: current enters a component's `a` terminal and leaves its `b` terminal, so a source's own current comes out negative.

## Time-based simulation

A run solves `t = 0, 1, 2, ...` in sequence, each step's equations
starting from the *previous* step's solved values rather than the live
database -- that's what lets `prev(x)` express a recurrence, and it's
also why nothing is freedom-idempotent mid-run the way a single solve
is: each step deliberately builds on the last.

**Preview** computes the whole requested number of steps entirely in
memory. Nothing is written to the garden -- not even a log entry --
until you explicitly commit. If a step fails to solve, the run stops
there but keeps every step computed before it, so you can still scrub
to and commit an earlier good one rather than losing the whole preview.

**Scrubbing** moves through the already-computed steps client-side (no
further solving) to see how far each property has come from where it
started, at whichever step you're looking at. The slider's own tick 0
is the real, unprojected garden -- exactly what's in the database right
now -- with tick 1 through N being the cumulative state after that many
steps. Motes touched as of the current tick get a dashed ring on the
graph canvas, and selecting one shows its projected values directly in
the inspector panel rather than requiring a separate changes list; the
panel marks itself read-only while a preview is open, since committing
always re-solves from whatever is in the database at that moment --
editing mid-preview could silently diverge from what was previewed.

**Commit** re-solves up to whichever step you chose -- deterministic,
since it starts from the same garden state and rules every time -- and
writes only that cumulative result, in one shot, the same way a single
`run_simulation()` call always has. Re-running afterward continues from
the committed values, not the original ones.

A rule with no `t`/`prev()` in it just solves to the same values every
step, which is a perfectly normal, boring, valid case -- not every rule
needs to be time-dependent for time-based simulation to be useful (e.g.
mixing one recurrence with several ordinary static rules that just get
re-checked each step).

## Running and re-running

A single `run_simulation()` call (used by the worked example scripts) is
*implemented as* committing a 1-step time series, not merely equivalent
to one. It writes solved values straight onto the motes and is
idempotent: freedoms are re-derived from the graph every time, so a
solved value never gets mistaken for a fixed input. What it keeps of its
own is how it reports: it raises `SimulationError` instead of returning
a result carrying an error, because a caller asking for one answer wants
either the answer or the reason. It also logs a failed run, where a
preview deliberately leaves no trace.

A committed or *saved* run (see "Run history" below) is logged to
`simulation_runs`/`simulation_changes` -- including a failed single-step
run, with old and new values for each change. A discarded preview leaves
no trace. The per-garden run counter (`run_number` -- unrelated to a
time-based run's own step/tick numbering, a different thing that used to
confusingly share the same name) comes from that log.

## Run history

Every logged run can be reviewed later, independent of whether it's still
the graph's current state:

- **Entering a run** (from the runs panel) drops into the same scrub bar
  used for live previews, but walking through *that run's own ticks
  only* -- unrelated to any other run. A steady-state run has exactly
  one tick (before/after); a run committed or saved from an N-step
  time-series has N, each logged individually rather than only their net
  result, specifically so this is possible.
- **Save simulation**, next to Discard/Commit on the sim bar, logs the
  *entire* previewed run for later review without writing anything to
  the graph -- unlike Commit, which always writes up to whichever tick
  is currently scrubbed to. This is for comparing hypothetical parameter
  sets (e.g. running the same turbine with several different inlet
  conditions) where no single result needs to become the graph's
  canonical state just to keep a record of it. Programmatically:
  `core.simulation.run_time_series(conn, step_count, commit=False,
  save=True)`.

A run's `committed` flag (`core.simulation.RunRecord.committed`)
distinguishes the two in the runs panel and CLI history: `True` for a
normal commit (or any plain `run_simulation()` call, which always
writes), `False` for a saved-only run or any [scenario](#scenarios) run.

`RunRecord.scenario_id` names the scenario a run was solved under, or is
null for the garden as it stands. `simulate history` and `simulate show`
print it, because two runs over the same rules that reached different
answers differ only by which question they answered.

## Why a value is what it is

Every logged run records not only *that* a property changed but which
equations, reading which known values, determined it. `why` walks that
record backwards:

```
monoss why <mote-id> <property>          # argparse CLI
why id=<mote_id> key=<property>          # REPL
```

```text
N2.voltage = 6.66667  [solver]
  solved together by the 'linear' solver
  found together with: V1.current, R2.current, R1.current
  <- KCL on N2: sum(sources('b').current) - sum(sources('a').current) == 0
  <- Ohm's law on R1: targets('a').voltage - targets('b').voltage == current * resistance
  derived N1.voltage = 10
    N1.voltage = 10  [substitution]
      <- Source on V1: targets('a').voltage - targets('b').voltage == voltage
      property V1.voltage = 10
```

The two `method`s are the two stages of the solve, and they explain
differently:

- **`substitution`** -- one equation determined this value alone. It has a
  single cause, and each of its derived inputs expands the same way, so
  the explanation is a chain.
- **`solver`** -- several equations determined several values *together*
  (see [What the solver can handle](#what-the-solver-can-handle)). There
  is no smaller attribution to make, so the record names every equation
  that constrained the block and every other value found with it, under
  `found together with`. An explanation that expanded this into a tree
  would be inventing a causality the solve never had.

An equation the substitution loop already used up is not blamed for a
later block. Once `voltage == 0` has determined a ground node it
re-evaluates to the trivially true `0 == 0`, which stays in the open set
but constrains nothing.

Recursion stops on anything the run did not derive -- a stored property,
a `property()` model evaluation, or the state entering the first step --
which is exactly the set of things that need no explanation. A value the
run never determined reports that plainly rather than failing.

`prev(x)` is followed into the step before, because that is literally
where it came from: the value `x` had entering this step is the one the
last step determined. At step 0 there is no earlier step, and the answer
is that it came from the garden.

**Values shown are in each dimension's base SI unit** -- what the solver
worked in -- not the unit a mote declares. A resistance recorded as
`4.7 kohm` explains as `4700`, so that the equation printed beside it
multiplies out. The change log (`simulate show`) converts back into each
mote's own unit instead; the two are deliberately different views.

`why` defaults to the garden's most recent successful run, which is the
one whose values are on the motes now. `--run` explains an earlier one,
`--step` a particular step within a time-series run (the default is the
last step that determined the value), and `--depth` bounds how far back
the recursion goes.

A **discarded preview records nothing** -- committing re-solves from the
database anyway, and that solve is what gets recorded. A **saved** run
records its derivations without touching the graph. A **failed** run
records neither derivations nor changes: the partial work of a solve that
did not finish describes a state the garden never reached.

## Comparison relations

A rule's comparison need not be `==`. `<=`, `>=`, `<` and `>` are all
accepted, and they turn a rule into a **constraint**: something the
solver checks against the answer rather than something it satisfies.

```text
name:      Efficiency is physical
equation:  efficiency <= 1
```

```text
name:      Turbine outruns pump
equation:  turbine.work >= pump.work
```

This is one parser change and no second expression language. Every
relation evaluates to `lhs - rhs <op> 0` from the same walk of the same
grammar; the operator decides only what is done with the result -- solved
for zero, or read for a sign. Constraints are dimension-checked,
unit-converted, and may use roles, `property()` and everything else a
rule can.

### A constraint can never determine a value

Constraints are separated from equations *before* anything is solved, so
no solver backend ever receives one. That is the structural form of "an
inequality cannot determine a value": there is no code path by which one
could. Consequences worth knowing:

- A garden whose only rule for `x` is `x <= 1` leaves `x` unsolved, and
  says so, rather than picking a number that satisfies it.
- `producers x` never offers a constraint. `consumers x` does.

### Reported, not enforced

Violations arrive on the result as `constraint_violations`, separately
from `dimension_warnings` -- a violated constraint is a statement about
the **answer**, a dimensional mismatch one about the **model**, and
folding them together would make neither readable.

```text
Solved 2 values across 1 mote from 2 equations (run #1). 1 constraint problem.
  constraint: Efficiency limit on T1: efficiency <= 1 is not satisfied (off by 0.2)
```

The run still solved and still wrote. That is deliberate: an answer that
breaks a limit is very often exactly what you ran to find.
`--strict-constraints` (`strict_constraints=True`) promotes a violation
to a failure instead, mirroring `--strict-units`.

In a time-based run each step checks its own constraints, since a
constraint is about the values that step found. `TimeSeriesResult`
labels each violation with its step.

### Unchecked is not passing

A constraint that still depends on something the run could not determine
is reported as **not checked** rather than treated as satisfied. Silence
would be indistinguishable from passing, and a limit nobody could
evaluate is exactly the one worth mentioning. Under
`--strict-constraints` an unchecked constraint fails the run too.

### Boundaries

`<=` and `>=` get the solver's tolerance, so `x <= 0.3` with `x` landing
on `0.1 + 0.2` passes. `<` and `>` do not, because excluding the boundary
is the whole of what makes them strict.

Chained comparisons (`0 <= x <= 1`) are rejected with advice to write
them as two relations -- which is what they are.

## Connected-name references

A dotted identifier that isn't a declared role can still reach a mote by
its literal name, as long as that mote is directly wired to the rule's
anchor -- any relationship type, either direction:

```text
R1 --relates_to--> R2
```

```text
R1.voltage - R2.voltage == current * resistance
```

This is a shorthand for `targets('relates_to').voltage`, not a
replacement for it: it exists so a garden wired by hand doesn't need a
relationship *type* chosen and a role declared just to write one
reference, and it stops working the moment the relationship is removed
-- the connection stays the thing that makes a reference legal, exactly
as it always has. The name it matches is the target mote's own `name`
property, so it only ever reaches a mote you can already see labelled
that way on the board.

Two things keep this from becoming a second, looser navigation:

- **A role always wins.** If the identifier is a role declared on the
  anchor, it resolves as a role (see [Roles](#roles) below) and the name
  search never runs -- a role is a stated intention and takes precedence
  over an accidental name collision.
- **A name must pick exactly one mote.** Two motes connected to the same
  anchor sharing a name is rejected outright (`'R2' matches 2 motes
  connected to mote ...`), never silently combined the way a `many` role
  or `sum()` would. A name is for reaching one specific thing.

An identifier that matches neither a role nor a connected mote's name
falls through to an ordinary property lookup, and fails exactly as it
always did if the anchor has no such property either -- so a name that
exists elsewhere in the garden but isn't wired to this mote is simply
not visible from here, on purpose. Motion's equation editor detects this
case (a name that exists in the garden but isn't reachable) and offers
to draw the missing relationship rather than leaving you to work out
why the reference fails.

## Roles

A rule can always reach other motes by navigating relationships
(`targets('a').voltage`). A **role** is that same navigation, declared and
named, so an assembly's parts can be addressed directly:

```text
Rankine Cycle #1 --boiler----> Boiler A
                 --turbine---> Turbine A
                 --condenser-> Condenser A
                 --pump------> Pump A
```

```text
turbine.h_in == boiler.h_out
```

A role is **not a new kind of object**: it is an ordinary relationship
whose *type* is the role's name. There is no binding table, because a
relationship already is one, and a role may point at another assembly, so
nesting comes free.

### Declaring roles

The reserved `roles` property, in any of the shapes the freedom list also
accepts:

```text
["boiler", "turbine"]                       every role, arity one
"boiler, turbine"                           the same, comma-separated
{"turbine": "one", "heaters": "many"}       arity per role
{"turbine": {"arity": "one", "species": "<mote-id>"}}
```

`roles` on a mote that is **acting as a species** declares what its
*instances* have; on any other mote it declares what that mote itself
has. That second reading is what lets a one-off assembly name its parts
without inventing a species with one instance. An instance may add roles
of its own on top of its species'.

### Arity

- **`one`** must reach exactly one mote, and collapses to a single value
  in a formula. Zero or several is a wiring mistake, and guessing which
  was meant is exactly the silent choice that hides it.
- **`many`** accepts any number, zero included, and **always yields a
  list** -- even when only one mote is wired. So a formula that will need
  `sum(...)` the day a second part is added needs it from the start,
  rather than changing meaning that day.

Untyped `targets(...)`/`sources(...)` navigation is unchanged, single
motes still collapsing, because every existing garden relies on that.

### Roles never shadow properties

A **bare name** is a property of the anchor mote. A name in the
**attribute position** is a role. Nothing that used to mean a property
can start meaning a role, because reading an attribute off anything other
than `targets()`/`sources()` was already an error. An undeclared name in
the attribute position is rejected, naming what is declared.

`core/dimensions.py` resolves roles exactly as the evaluator does, so an
equation written with roles is dimension-checked like any other.

### Checking

```
monoss roles check       # argparse CLI
roles check              # REPL
```

Reports roles wired to the wrong number of motes or to a mote of the
wrong species. This is a **lint, not part of a run**, unlike the
dimensional check -- the difference being what happens when each goes
wrong. A dimensional mismatch is silent and yields a plausible wrong
number, so a run has to warn about it; a mis-wired role that any rule
actually uses fails that run outright, naming the role.

### Rule scope

What `applies_to` points at decides how widely a rule is asserted, and
that is **inferred from the target**:

```text
a species  ->  once per instance   (everything before roles existed)
any mote   ->  once, about it
```

"Acting as a species" is the same test `list_species` uses: being the
target of an `instance_of` or `template_of` relationship. One
relationship type covers both cases.

The cost of inferring is that making the first instance of a mote flips
every rule asserted about it from "once" to "each". `roles check` lists
every rule currently resolved to "once" for exactly that reason -- so the
change is visible before it produces a different answer.

A `once`-scoped rule can only determine values its mote is free to
change -- which is why `freedoms` may be declared on the mote itself and
not only on a species. A one-off assembly declares both its parts and its
freedoms directly, and needs no species at all.

### Worked example

`scripts/example_rankine_cycle.py` builds a complete Rankine cycle in a
garden called `rankine-cycle-example`:

```
.venv/bin/python scripts/example_rankine_cycle.py
```

Boiler, turbine, condenser and pump each solve their own state from
CoolProp; a `Rankine Cycle` assembly declares them as four roles and
closes the loop with rules like `turbine.h_in == boiler.h_out`. Those
linking rules are anchored on the *cycle* and solve unknowns belonging to
the *parts* -- which has always been possible, but reads as a model
rather than as graph navigation once the parts have names.

## Scenarios

A garden holds one state, but every question worth asking about a model
is comparative: design against off-design, best against worst case, what
breaks if the feedwater drops 20 K. A **scenario** is a named
perturbation, so those can be asked without editing the garden and
editing it back.

The shape mirrors templates, one arrow over:

```text
template_of : species  ::  overrides : mote
```

A template holds placeholder properties for a species and is excluded
from its instances. An **override** holds replacement properties for one
specific mote and is excluded from every run unless a scenario selects
it. A **scenario** gathers overrides:

```text
Winter Case --varies--> Winter Turbine --overrides--> HP Turbine
```

Two reserved relationship types, no new properties, no new tables. An
override's replacement values are **ordinary properties**, so the
property editor, units, and the whole MCP surface work on them unchanged.

### Running one

```
monoss simulate run --scenario <scenario-mote-id>
monoss scenario list
monoss scenario show --id <scenario-mote-id>
```

```text
High Load varies 1 mote(s):
  R1
    current: 2 -> 10
```

**A scenario run never writes to the graph.** `run_simulation` with a
scenario solves, logs the run as uncommitted with its `scenario_id`, and
reports what *would* change; `run_time_series` refuses `commit=True`
outright, since committing is an explicit request to make something the
garden's state. `save=True` keeps the run for later comparison, which is
usually what a scenario is for.

This is not a limitation to lift later. The answer to a hypothetical is
not the garden's state, and "where would a scenario run's results be
written" has no good answer.

### Changing the question, not just the inputs

An override may carry **`freedoms`**, which is what makes scenarios more
than a parameter sweep:

```text
override:  freedoms: resistance
           voltage:  24
```

The same equation that sized a resistor now rates it. That is why
[freedoms](#the-three-pieces) had to move off the species first: a
scenario perturbs *motes*, so anything it changes has to be declarable on
a mote.

`incidence_index(conn, scenario=...)` answers the same structural
question about a hypothetical -- what a scenario would make determinable,
and what it would leave missing -- before anything is solved.

### Details worth knowing

- An override **replaces every property it carries**, `name` included.
  There is no special case, exactly as a template supplies every property
  it carries to a new instance. Name an override for what its target
  becomes under that scenario, or leave `name` off it.
- Overrides merge **per property**: an override naming one pressure
  changes that pressure and nothing else about its target.
- Override motes are left out of **every** run, scenario or not. They
  hold replacement values rather than state, and one declaring `freedoms`
  would otherwise become solvable in its own right.
- A scenario id naming no scenario is an **error**, not a quiet fallback
  to the unperturbed garden -- it is nearly always a mistyped id, and
  silently answering a different question is the worst possible response.
- An override pointing at no target yet is skipped rather than fatal, the
  same reading a rule with no equation gets.
- Scenarios compose with time series: each step reads the perturbed
  state.

## Sweeping

A scenario lets a garden hold a second answer. A **sweep** lets it hold a
range of them: `core/sweep.py` re-solves the whole garden once per step
of a property's range and reports another property at every step. That
is a curve rather than a state, which is why it is a module of its own
rather than an option on `run_simulation()`.

It has its own window, **Moxie** (`run_moxie.py`, and `docs/ui-setup.md`).

```python
from core.sweep import Axis, Case, run_sweep

result = run_sweep(
    conn,
    Axis(mote_id=source, key="voltage", lo=0, hi=20, steps=200),
    [Case("base")],
    [(r1, "current")],
)
```

Three shapes, the same machinery three ways:

| Call | Shape |
|---|---|
| `run_sweep` | one axis → a line, or several |
| `run_surface` | two axes → a grid of cells |
| `run_correlation` | no axis → one dot per mote, from a single solve |

### A sweep writes nothing

No write-back, no run-log row, no scenario motes created and destroyed
around the edges. A sweep leaves the garden exactly as it found it,
which is what makes it safe to fire on every frame of a dragged slider.
This is not a default to be lifted later: the answer to "what would this
be across that range" is not the garden's state, for the same reason a
[scenario](#scenarios) run is never committed.

The mechanism is ordinary. `solve_properties()` (in `core/simulation.py`,
alongside the `solve_view()` that `run_simulation` and `run_time_series`
both now call) takes a properties **dict** rather than a connection --
so there is no database in reach to write to. Each point applies the
scenario, then the case's transient overrides, then the swept value, and
solves the copy.

### Cases, not series

A `Case` is one what-if evaluated at every point: a scenario, plus
transient overrides of the form `{mote_id: {key: value}}` that are never
written anywhere. Several curves that differ only in *which property
they read* are one case and one solve per step; two that differ in
scenario or in an override are two.

So the cost of a sweep is `len(cases) * steps`, not `series * steps`,
and a caller reading eight properties off one solve pays for one. Moxie
states that number in its run rail rather than capping it.

### Failure is a point, not an end

A solve can fail at one x and succeed at the next -- an equation that
divides by the swept value at zero, a Newton run that will not converge
at one end of a range. The failed point carries the solver's own message
and the sweep continues, because 219 good points and a labelled gap is a
better answer than an exception. `SweepResult.failures` counts them.

Constraints come back the same way. Every solve checks them anyway, so
each point carries the ones its answer does not satisfy, and
`SweepResult.broken_constraints` names each broken rule once. That is
the honest form of a limit line: rather than reading a fixed bound off a
rule and drawing it, a plot shades the range where the model actually
broke its own rule -- which stays right even where the bound itself
moves with the sweep.

### Pinning a freedom

Nothing stops a sweep varying a property the solver would otherwise
determine; the override simply wins for that solve. It is worth saying
out loud, though, because the answer under it is "what if this were so"
rather than "what the model determines" -- so `SweepResult.pinned` says
when the swept property was one of its mote's freedoms.
`simulation.garden_freedoms(conn)` answers the same question without
running anything.

### Sweeping off a snapshot

`read_garden(conn, scenario_ids)` reads everything a sweep needs in one
go, and every `run_*` above accepts that `Ground` in place of a
connection. A caller can therefore read under whatever lock guards its
connection and solve outside it -- which is what lets Moxie stay
responsive, since a sweep holding the connection for its whole run would
also be holding it against the keystroke meant to cancel it. `indices`
(and `run_surface`'s `stride`) compute a subset of the points, so a
coarse pass can be drawn before a fine one refines it, and
`should_cancel` is polled between points.

### Saved plots

`plot` is a reserved property (`core/reserved.py`). A mote carrying one
is a saved plot: **the specification** -- which motes, which keys, which
ranges, which scenarios -- and never the points it produced. Reopening
one therefore re-solves the garden as it stands now, which is the whole
difference between a saved plot and a screenshot. Written and read in
Moxie, and shaped exactly like `query`, Monolith's saved view.

## Dependency queries

`why` reads the run log. These four read the *rules*, so they answer
before anything has been solved, and they answer about the model rather
than about one run:

```
monoss impact    <mote-id> <property> [--distance N]
monoss requires  <mote-id> <property>
monoss producers <mote-id> <property>
monoss consumers <mote-id> <property>
```

The index behind them is **derived on every call, never stored**. The
equations are the single source of truth about what depends on what, and
a persisted copy would be a second one. It costs one probe evaluation per
rule/instance pair -- the same order of work the dimensional check
already does, and far less than solving, since nothing here iterates.

### The shape is bipartite, not a DAG

An equation does not have inputs and outputs. `V == I * R` determines V
in one garden and I in another, from the same text -- what decides is
which values are **free** (declared via `freedoms`). So the
index relates *equations* to *values*, and every query reads that
relation rather than following arrows.

Two consequences worth knowing before reading the output:

- **`producers` returns every equation that touches a free value.** If
  both `b` and `c` are free, `c == b + 1` is a producer of `b` as much as
  of `c`, because either is what it determines depending on what else is
  known when the solve runs. Reporting one of them would be inventing a
  direction the system does not have.
- **`producers` and `consumers` never overlap**, and which one an
  equation lands in is decided by the value's freedom, not by the
  equation. A free value is produced and never consumed; a stored value
  is consumed and never produced. `producers` coming back empty means
  *nothing can determine this*, which is the most common modelling
  mistake -- and `requires` tells you which of its two causes it is.

### `impact`

```text
Changing R1.resistance could move 6 values:
  1 step away
    N1.voltage  via Ohm's law on R1
    R1.current  via Ohm's law on R1
  2 steps away
    V1.current  via KCL on N1
```

A breadth-first walk: a change perturbs every equation touching the
value, each perturbed equation can move anything it is free to
determine, and those perturb further equations. **Distance is hops
through the equation graph, not a causal ordering** -- everything in a
coupled block is found at once. It is shown because the near values are
usually the ones a reader meant, not because the change arrives in that
sequence.

A value nothing is free to determine can be a cause but never an effect,
so it never appears in the results.

### `requires`

```text
To determine N2.voltage:

  determined by:
    Ohm's law on R1
    Ohm's law on R2

  needs (1 missing):
    missing  R2.resistance
    derived  N1.voltage
    stored   V1.voltage
```

Each prerequisite is **stored** (the mote holds a value), **derived**
(free, and some equation determines it), or **missing** (neither -- this
is what to go fix). Missing ones are listed first, since they are the
only part anyone has to act on.

The closure is flat rather than a tree, for the same reason `why` stops
at a solver block: a coupled block has no tree inside it.

Two failures get their own headline, because both are otherwise silent:

```text
Nothing can determine V1.voltage.
  It is not among its mote's declared freedoms, so the solver may not change it.
```

```text
Nothing can determine L1.x.
  It is declared free, but no equation constrains it.
```

A free property is reported as **derived** even when the mote already
holds a number for it -- that number is a previous run's answer and is
about to be replaced, so calling it a stored input would describe the
garden rather than the model.

## Physical property models

A mote can reference a **property model** -- another mote describing how
one of its physical properties is determined -- via a `uses_model`
relationship:

```
name:          Air
uses_model  --> Ideal Air
```

```
name:           Ideal Air
provider:       ideal_gas
configuration:  {"gas_constant": 287.05}
```

`provider` names a registered `PropertyProvider` (`core/providers.py`);
`configuration` is whatever that provider needs to construct itself.
Two providers are built in, both in SI units:

- `ideal_gas` (`core/ideal_gas_provider.py`), needing no external
  dependency: `pressure(density, temperature)`, `density(pressure,
  temperature)`, `temperature(pressure, density)`.
- `coolprop` (`core/coolprop_provider.py`), wrapping the CoolProp library
  for real fluids such as water/steam. Configuration is `{"fluid":
  "Water", "backend": "HEOS"}` (`backend` optional, defaults to `HEOS`).
  Operations are named `<output>_<input-initials>` rather than a bare
  output name, since a fluid property is commonly reachable from more
  than one input pair (`h(P,T)` and `h(P,s)` are different lookups that
  happen to share an output) and `PropertyProvider.capability()` resolves
  purely by matching the operation string: `enthalpy_pt(pressure,
  temperature)`, `entropy_pt(pressure, temperature)`, `density_pt
  (pressure, temperature)`, `temperature_ph(pressure, enthalpy)`, and
  `enthalpy_ps(pressure, entropy)` (the isentropic lookup a turbine uses
  to get its ideal outlet enthalpy). See
  `scripts/example_steam_turbine.py`. Quality (vapor mass fraction, 0-1,
  dimensionless) only means anything inside the two-phase dome, so it
  appears both as an input and an output: `quality_ph(pressure,
  enthalpy)` and `quality_ps(pressure, entropy)` read a state's quality
  back out -- e.g. checking a turbine's exit wetness after an isentropic
  expansion -- while `enthalpy_pq`, `entropy_pq`, `temperature_pq`, and
  `density_pq` all take `(pressure, quality)`. A state outside the dome
  (e.g. superheated steam has no quality) raises `OutsideDomain` --
  CoolProp itself silently returns a `-1` sentinel there instead of
  erroring, which `evaluate()` catches rather than letting flow through
  as a bogus quality value.

A rule reaches this through the `property()` builtin rather than
knowing anything about `ideal_gas` itself:

```
name:      Ideal gas law
equation:  pressure == property('gas', 'pressure', density, temperature)

  --applies_to--> Container
```

Resolution (`core/model_resolver.py`) is a pure, DB-free lookup with the
same shape as everything else in this table -- it follows `uses_model`
from the mote `property()` navigated to, reads `provider`/`configuration`
off that model mote, and constructs the provider. A provider is
constructed at most once per simulation run (cached across every
substitution pass and, for a time-based run, every step), not once per
formula evaluation.

## Units

A numerical property can optionally declare what it measures, through a
mote's reserved `units` property -- a map from property key to unit
name:

```
name:        R1
resistance:  4.7
current:     3
voltage:                  <- the solver writes this

units:  {"resistance": "kohm", "current": "mA", "voltage": "V"}
```

Units are **opt-in per property**. A key with no entry is simply
unitless, and a garden that declares none behaves exactly as it did
before this existed -- no warnings, no conversions, identical numbers.
They're declared per mote rather than per species, so two instances of
the same species can legitimately track the same quantity differently
(one resistor in `ohm`, another in `kohm`). A template's `units` map is
copied into new instances like any other starting property.

### What you can write

A unit name is either a registered unit or a compound expression over
them: `m/s`, `kg/m^3`, `J/(kg*K)`, `kg/m³`, `s^-1`. `unit list` shows
everything registered, grouped by dimension. SI prefixes work on the
usual units (`kohm`, `mV`, `MPa`, `km`). Common non-SI units are
included too -- `min`, `hr`, `day`, `in`, `ft`, `mi`, `lb`, `bar`,
`atm`, `psi` -- along with the affine temperature units `degC` and
`degF`, which carry an offset and so can only be used on their own,
never inside a compound.

Angle is genuinely dimensionless in SI, so there's no separate `rad`:
an unlabeled number is already radians, exactly what `sin`/`cos`/`tan`
expect, and `deg` converts into that on the way in like any other unit
(`angle: 90, units: {"angle": "deg"}` reaches a formula as `π/2`).

`%` is dimensionless for the same reason -- it's a ratio, and its only
content is the factor of 100. A property declared in `%` stores the
number a person reads and computes on the bare ratio
(`efficiency: 22.5, units: {"efficiency": "%"}` reaches a formula as
`0.225`), and a solved `0.775` is written back as `77.5`. In the command
bar it's the one unit written with no space before it: `P.efficiency =
22.5%` (`22.5 %` works too).

```
unit convert --value 36 --from km/hr --to m/s
```

### Solving

The solver works internally in base (SI) units. A declared value is
converted on the way in and the answer converted back on the way out, so
mixed units combine correctly:

```
4.7 kohm  x  3 mA  ==  14.1 V
```

Crucially, **solving never changes the unit a property is recorded in**.
If R1 declares its voltage in `mV`, it gets `14100`; if it declares `V`,
it gets `14.1`. The same is true of logged changes and of every step of
a time-based run.

An unrecognized unit name fails the run rather than being ignored --
treating `kohm` as if it were already ohms would be a silent
factor-of-1000 error in the answer. A malformed `units` property (not a
map, or with non-string values) leaves the mote un-annotated instead of
breaking the run.

### Inference

A **freedom** the solver just determined is different from any other
property: it is about to be written regardless, and if the equation that
determined it names a dimension in plain sight, leaving the result
unitless would throw away information the garden already had. So when a
freedom has no `units` entry of its own, the solver looks at whichever
equation determined it and, if that equation reduces to the bare freedom
on one side (`resistance == voltage / current`), reads a unit straight
off the dimension of the other side:

```
resistance:               <- solved, previously undeclared
voltage:     10
current:      2

units:  {"voltage": "V", "current": "A"}     <- becomes {..., "resistance": "ohm"}
```

`resistance` comes out as `5`, in ohms, and the mote's `units` map is
updated to say so -- both in the same write. A dimension with no name of
its own (`distance / time`) falls back to the base-SI product, written
in the same caret notation a person typing a unit by hand would use
(`m*s^-1`) via `describe_expression()`, rather than the unicode
superscripts `unit list` and `describe()` use for an error message
(`m·s⁻¹`) -- so a value the solver named on its own looks exactly like
one a person named, once `format_unit()` renders either back with
superscripts for display. Leaving the property silently unnamed would
be worse than either.

This only ever *adds* a unit, never overrides one a mote already
declares, and only ever fires when the freedom is the **entire** left-
or right-hand side of the equation -- `voltage == 2 * resistance` says
nothing safe about `resistance` in isolation, so a freedom buried inside
a larger expression is left alone rather than guessed at. Like the
dimension checker, it stays silent by default: a garden that never
declares any units contributes no dimensions to infer from, so nothing
changes for it. A scenario run never triggers this either, for the same
reason it never writes anything else back to the graph.

### Checking equations

Because units carry dimensions, an equation can be checked for
consistency before its answer is trusted:

```
unit check
```

```
1 dimensional problem(s):
  Bad rule on R1: ohm cannot be added to A -- different dimensions
```

This walks the same expression grammar the solver evaluates, propagating
dimensions instead of values: `+`/`-` require a match, `*`/`/` add and
subtract dimensions, `**` scales by its (constant) exponent, and a
`property()` call takes the unit its provider's capability declares.

A mismatch is reported only when **both** sides are actually declared,
which is what keeps a partly-annotated garden quiet. A bare number
adapts to whatever it's compared with (`resistance == 100` is fine), and
`t` constrains nothing, since a rule may read it as either a step count
or elapsed time. A *computed* dimensionless value is different: if
`a / b` cancels to a pure ratio, comparing it to a voltage is a real
mistake and gets reported.

By default this is advisory -- warnings appear on the run's result
(`SimulationResult.dimension_warnings`) and the run proceeds. Passing
`--strict-units` (programmatically, `strict_units=True` on
`run_simulation`/`run_time_series`) turns a mismatch into a failure
instead, for a garden whose units are complete enough to enforce.

### Worked example

`scripts/example_radiating_panel.py` builds a garden called
`radiating-panel-example`:

```
.venv/bin/python scripts/example_radiating_panel.py
```

Two radiating panels solving `power == emissivity * stefan_boltzmann *
area * temperature ** 4` for their surface temperature. The same rule
demonstrates both halves of this page: it is genuinely nonlinear in its
unknown, so `linear` refuses it and `newton` solves it; and the two
panels are the *same* panel under the same load described in different
units -- watts, square metres and kelvin for one, kilowatts, square
centimetres and degrees Celsius for the other -- so they must, and do,
come out at the same temperature, each written back in its own declared
unit. The script closes by showing what the dimension checker says about
a rule that doesn't add up.

## Not built yet

- **Continuous dynamics.** Time-based simulation is discrete steps, not an ODE integrator -- a numerical approximation, deliberately, not SPICE-style companion models. Fine for recurrences; genuinely continuous behavior (capacitors and inductors modelled from first principles, rather than as a discrete update rule) would need real integration. A solver backend for this shape (`dx/dt` rather than `F(x) == 0`) could join `linear`/`newton` later without changing how a rule is written.

  There is no `dt` anywhere in the engine, and no seconds: **a tick is one re-solve of the equation set**, not an integration step. Motion says so rather than implying a clock -- its timeline is labelled in ticks, and the payload carries the axis it is counted in (`motion/payload.py::tick_axis`) so that a real time axis is one place to add rather than a frontend assumption to unpick. An integrator would reach Motion's solver menu by being registered in `core/solver_registry.py`, which is the only registration point either side reads.
- **Per-literal unit syntax in an equation** (`100[ohm]`) -- a bare number takes on the unit of whatever it's compared with. (A solved freedom with no unit of its own *is* now given one -- see [Inference](#inference) above -- but only when it is the entire side of the equation that determined it; anything shapelier still has to be declared.)
- **Non-physical units.** Currencies, story points, and other non-SI "units" aren't in the dimension system. A passthrough dimension for them could be added without disturbing the design.
- **A default solver per garden/species.** `solver` is a per-call argument only; there's no way to persist "this garden always uses `newton`" so every caller has to name it.
- **Species inheritance.** Species are flat.
- **Multi-mote models as roles.** A rule reaches other motes by
  navigating relationships (`targets('a').voltage`), which works but ties
  the model to graph topology rather than to named roles. See
  `docs/modeling_proposal`.
- **Scenarios.** A garden holds one state, so asking a second question
  means editing it. See `docs/modeling_proposal`.

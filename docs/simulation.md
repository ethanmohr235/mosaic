# Simulation

MonOSS can solve for unknown mote properties from equations you write yourself. The engine knows nothing about circuits, thermodynamics, or any other domain -- all of that lives in ordinary motes.

Simulation lives in the console's sim panes (`layout calc`,
`docs/ui-setup.md` → *Sim*). `sim` previews a time-based run -- every
tick is solved in memory and nothing is written to the garden -- as a
table of the values that matter, scrubbed with one shared playhead.
`solve` solves the garden once and writes the answer back, one undo
step; with `solve --live on` that happens after every edit. A scenario
is never written, since it answers what *would* happen. The same answers
are `core/motion.py`'s, so the CLI and MCP can reach them too.

See "Time-based simulation" below.
Programmatically, a single steady-state solve is
`core.simulation.run_simulation(conn)`; a time-based run is
`core.simulation.run_time_series(conn, step_count)`, which is always a
preview.

A third question -- how one value moves as another changes -- has an
answer shaped like a curve rather than like a state, and so has its own
module and its own console pane: `core.sweep.run_sweep()`, and the `plot`
pane. See "Sweeping" below.

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
| `R1.current` | A property of the mote named `R1` -- a neighbour of the anchor if one is named that, otherwise any mote in the garden with that name, tried when the identifier isn't a declared role (see [Name references](#name-references)). |
| `targets('a')` | The motes this one points at via a relationship of type `a`. |
| `sources('a')` | The motes pointing *at* this one via a relationship of type `a`. |
| `targets('a').voltage` | That property on those motes. Collapses to a single value when exactly one mote is reached -- unlike an `arity: many` role, which never collapses. |
| `sum(...)`, `count(...)` | Combine several motes into one value. Required when more than one mote is reached. |
| `+ - * / **` (`^` also works as exponent) | Arithmetic. Parentheses work as usual. An unknown may appear in a denominator -- see [Writing an equation the way you mean it](#writing-an-equation-the-way-you-mean-it). |
| `pi`, `e` | The math constants (3.14159..., 2.71828...). Reserved words -- shadow a property literally named `pi` or `e` inside a formula specifically, the same way `t` does. |
| `abs`, `min`, `max` | Only on values that are already known. |
| `sin`, `cos`, `tan`, `arcsin`, `arccos`, `arctan`, `sinc`, `sqrt` | Also only on known values. `sinc(x)` is the unnormalized `sin(x)/x`, with `sinc(0) = 1` (numpy's `sinc` is `sin(πx)/(πx)`; this one is not). `sin`/`cos`/`tan` take radians -- an argument declaring unit `deg` (see [Units](#units) below) arrives already converted; their inverses `arcsin`/`arccos`/`arctan` return radians the same way, and `arcsin`/`arccos` require an argument between -1 and 1. `sqrt` of a negative number is an error. |
| `t` | The current step's index within a time-based run (0 for the first step). A reserved word -- shadows a property literally named `t` inside a formula specifically, though that property is still ordinary and visible everywhere else. Always 0 in a plain (non-time-based) run. |
| `prev(x)` | The value `x` had *entering* the current step -- `x` must be a bare property/unknown name on the mote the rule is running against (not `targets(...)`'s result). This is how a rule expresses a recurrence: `balance == prev(balance) * 1.05`. Only meaningful in a time-based run; raises an error otherwise. |
| `phi(x)` | A call to a [function mote](#functions) named `phi`: its body, evaluated with each parameter bound to its argument. An unknown may be an argument; it passes into the body as itself. |
| `integrate(phi, a, b)`, `deriv(phi, x0)` | The definite integral of the function mote `phi` from `a` to `b`, and its derivative at `x0`, both numerical. A function of several parameters names the one it is along, then gives the others: `integrate(f, 'y', a, b, x0)`. Deferred like `sin` until every bound or point is a number. See [Calculus](#calculus). |
| `property('rel', 'operation', ...)` | Evaluates `operation` (e.g. `'pressure'`) through the property model of the single mote reached by relationship type `'rel'`, e.g. `property('working_fluid', 'pressure', density, temperature)`. The relationship type and operation must be quoted string literals; the remaining arguments must already be known numbers -- a property model is an opaque function of its inputs, so like `abs`/`min`/`max` this is deferred (retried by the substitution loop) until they are. See "Physical property models" below. |

Anything else -- indexing, comprehensions, attribute access, `import`, calling a Python function -- is rejected when the formula is parsed. The parser uses Python's `ast` module against a whitelist and never calls `eval`.

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

`run_simulation`/`run_time_series` (and the console's `sim --solver`) all take an optional `solver` (a name) and `solver_configuration` (a dict of backend-specific settings), the same pattern [physical property models](#physical-property-models) use for `provider`/`configuration` -- a pluggable, named backend rather than one hardcoded algorithm. Unlike a property model, solver choice can't be attached to a single mote: solving happens over the whole coupled equation system at once, so it's picked per run, not per mote.

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

A complete circuit in a garden called `circuit-example`: a 10 V source across 100 Ω and 200 Ω in series, modelled with four rules -- Ohm's law, Kirchhoff's current law, a source law, and a ground reference. Terminal convention: current enters a component's `a` terminal and leaves its `b` terminal, so a source's own current comes out negative.

## Time-based simulation

A run solves `t = 0, 1, 2, ...` in sequence, each step's equations
starting from the *previous* step's solved values rather than the live
database -- that's what lets `prev(x)` express a recurrence, and it's
also why nothing is freedom-idempotent mid-run the way a single solve
is: each step deliberately builds on the last.

**Preview** computes the whole requested number of steps entirely in
memory. Nothing is written to the garden; a time
series is only ever a preview. If a step fails to solve, the run stops
there but keeps every step computed before it, so you can still scrub
through the good ones rather than losing the whole preview.

**Scrubbing** moves through the already-computed steps client-side (no
further solving) to see how far each property has come from where it
started, at whichever step you're looking at. In the console's `sim`
pane tick `t` is the state after the solve whose `tick` variable read
`t`, so ticks run 0 to N-1; the value before the run (the garden, under
the scenario) is each row's `start`, which tick 0's Δ is measured from.

**Plotted.** The same run is a curve: `plot bd.level vs t 0:50` draws ticks
0 to 50 of it, through everything a sweep does -- knobs and scenarios, `plot
save`, the exports and `\graph{…}`. Both walk `simulation.iterate`, the one
stepping loop, so the plot is the `sim` pane's rows by construction. The
points are solved in order (each from the last), so a time axis is never
sampled coarsely; a range `lo:hi` runs from tick 0 and shows `lo` to `hi`;
a tick that fails ends the run there and draws as a failed span. `fit` over
time and `--data` points on a time axis are not done.

A rule with no `t`/`prev()` in it just solves to the same values every
step, which is a perfectly normal, boring, valid case -- not every rule
needs to be time-dependent for time-based simulation to be useful (e.g.
mixing one recurrence with several ordinary static rules that just get
re-checked each step).

## Running and re-running

A single `run_simulation()` call (what the console's `solve`, `simulate`
and the worked example scripts use) solves the garden once and writes
the solved values straight onto the motes. It is idempotent: freedoms
are re-derived from the graph every time, so a solved value never gets
mistaken for a fixed input. It raises `SimulationError` instead of
returning a result carrying an error, because a caller asking for one
answer wants either the answer or the reason, and a failed solve writes
nothing.

It goes through the same `solve_view()` as each step of a preview, so a
one-step preview shows exactly what a solve would write. The difference
is `prev(x)`: a solve that writes back is one-shot and has no previous
step, so a rule using `prev()` is refused rather than quietly advanced
by a tick. A recurrence is something you preview.

Nothing keeps a log of past runs. The garden's state is its motes; `why`
solves it again in memory when asked (below).

**Stored values are the record, and only `solve` writes them.** A
document does not wait for one: `live_garden()` is the garden as a solve
would leave it, in memory. Solved values are laid over the stored ones in
their declared (or inferred) units, and a freedom the solve did not
determine is taken out and named in `unsolved`. That is what `\val`,
`\work` and the calc sections read (`modoc.md` -> *A note reads the
garden as a solve leaves it*). Surfaces that show stored values (the
console's table and `open`) mark a freedom that holds no number as *not
solved*.

## Why a value is what it is

A solve finds not only *that* a property takes a value but which
equations, reading which known values, determined it. `why` solves the
garden as it stands, in memory and writing nothing, and walks that
derivation backwards -- so it answers before anything has been written
and can never describe a value an edit has since changed:

```
monoss why <mote-id> <property>          # CLI
why <m.k>                                # console
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

Recursion stops on anything the solve did not derive -- a stored
property or a `property()` model evaluation -- which is exactly the set
of things that need no explanation. A value no solve determines
reports that plainly rather than failing.

**Values shown are in each dimension's base SI unit** -- what the solver
worked in -- not the unit a mote declares. A resistance recorded as
`4.7 kohm` explains as `4700`, so that the equation printed beside it
multiplies out. The changes `simulate` prints are converted back into
each mote's own unit instead; the two are deliberately different views.

`why` explains the garden **as it stands now**, so it is never older than
the last edit; `--depth` bounds how far back the recursion goes. It writes
nothing, and the same explanation works under a scenario or a knob
(`derivations.Explainer(scenario=…, overrides=…)`). A garden that does not
solve answers with the solver's message in `note` rather than an
explanation. One `Explainer` serves a whole document: its first question
solves, the rest read the same journal.

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
Solved 2 values across 1 mote from 2 equations. 1 constraint problem.
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

### Equality checks

An equation can be a check too. A rule carrying the reserved `check: true`
is evaluated against the answer and never solved, whatever its operator,
which is how `R1 + R2 == W` verifies the reactions two moment equations
found instead of being used to find one of them. It counts among the
checks, not the equations, in solve health; it determines nothing in the
incidence index; and it never stands in for a missing equation -- a value
only a check mentions stays unsolved.

An equality needs a tolerance, and a relative one: `|lhs - rhs|` may be
up to the rule's `tolerance` (default `0.001`, i.e. 0.1%) times the
largest magnitude the relation involved, with the solver's own absolute
tolerance as a floor so a relation whose terms are all zero is not held to
exact floating point. A failure says what it was allowed: `off by 10,
allowed 1 (0.001 relative)`. A comparison ignores `tolerance`.

In the worksheet an equality check is written `?=`:
`b.R1 + b.R2 ?= b.W  ! ΣFy`.

## Uncertainty

A given carries its ± in the reserved `uncertainty` property, a
per-property map shaped like `units`: `{"x": 0.5}` is ± 0.5 in x's own
unit, `{"x": "2%"}` is ± 2% of its value. The value itself stays a plain
number, so the solver and everything else that reads it are unchanged.
In the worksheet: `bd.x = 80 ± 0.5 in` (or `+-`, or `± 2%`).

A result's ± is **derived on read and never stored** --
`core/uncertainty.py::propagate(conn)`, like solve health, answers from
the garden as it stands, so there is nothing to go stale when a given's ±
is edited. It is first order with the givens independent, combined
root-sum-square:

    σ_y = sqrt( Σ_i (∂y/∂x_i · σ_i)² )

Each ∂y/∂x_i is a **central difference of the solve itself**: the garden
is solved on copies with x_i nudged up and down by 10⁻⁴ of max(|x|, σ),
writing nothing. To first order that is exactly the implicit-function
derivative −J_y⁻¹·J_x a Newton step would give, and it works for every
solver backend (the linear one has no Jacobian to hand over), including
units, substitution and property models, because it *is* the solve. It
costs two solves per uncertain given.

Each result carries its **budget**: every given's sensitivity and share of
the variance, largest first, the shares summing to 1. A result nothing
uncertain reaches is ± 0, not "no ±". An `uncertainty` entry on a freedom
is ignored, since a result's ± is what this computes. An unreadable entry
is reported, never dropped: a ± that vanished would make an answer look
more certain than it is.

Values and ± come back in each result's declared unit, or else the one
inferred for it (see *Inference*); a result with neither is in base SI.

`\val`, `\given` and `\answers` print a value with its ± (`100.0 ± 0.54
in`), the ± to two significant figures whatever the value is quoted to.
`monoss uncertainty`; MCP `propagate_uncertainty`.

## Name references

A dotted identifier that isn't a declared role reaches a mote by its
literal `name` property:

```text
bd.l == sqrt(bd.x ^ 2 + bd.y ^ 2)
```

Nothing has to be wired up for that to work. The name it matches is the
mote's own `name`, so it only ever reaches a mote you can already see
labelled that way on the board.

### Three scopes, asked in order

1. **A declared role** on the anchor (see [Roles](#roles) below). A role
   is a stated intention and takes precedence over an accidental name
   collision, so the name search never runs for one.
2. **A mote connected to the anchor** -- any relationship type, either
   direction. This is what settles a garden holding two motes of the same
   name: draw one relationship, and that edge decides which one this rule
   means.
3. **Any mote in the garden.** *Garden-scope names*: a name unique across
   the whole garden resolves with no relationship at all.

An identifier matching none of the three falls through to an ordinary
property lookup, and fails exactly as it always did if the anchor has no
such property either.

### A name must pick exactly one mote

Two motes reachable in the same scope sharing a name fails the run
outright, naming every one of them:

```text
'bd' matches 2 motes in the garden (7f3a1c08, c41e9b2d) -- rename all but
one, or reach it with targets('relationship-type')
```

It is never silently combined the way a `many` role or `sum()` would be.
A name is for reaching one specific thing, and a garden with two `bd`s
has a modelling problem no solve should paper over. `simulation
.unresolved_equation_names()` reports exactly these collisions, with the
colliding mote ids, so an editor can offer to draw the one relationship
that resolves it (scope 2 beats scope 3). The console's rule pane shows
them as hints, and the command bar uses it too.

### Why the garden scope exists

`FEA-Homework-4` -- 10 motes expressing about 9 numbers and 5 equations
-- carried six `related_to` relationships whose only job was making a
reference legal. They said nothing about the model, they were a second
thing to remember when adding a mote, and a reference broke the moment
one was tidied away. A name is already unique enough to identify a mote
*to the person writing the equation*; where it genuinely isn't, the
refusal above says so.

Relationships in a garden now mean something. `targets(...)`/`sources
(...)` navigation and roles are unchanged -- they are how a *model*
expresses structure, and a name reference was never a replacement for
either.

## Functions

A rule is an equation about specific motes, so it holds at one point: the
flux at whatever `reactor.r` holds. A **function** is the relation
itself, asked at whatever point a caller names. It is an ordinary mote
(`core/functions.py`) with a reserved `function` body and reserved
`parameters`:

```text
name:        phi
function:    A * sin(B * r) / r
parameters:  r                         <- a comma string or a JSON list
units:       {"r": "cm", "B": "1/cm"}
A:           5e13
B:           0.0628
```

```text
reactor.flux        = phi(reactor.r)
reactor.flux_center = phi(0)
```

- **A call is the body, evaluated with each parameter bound to its
  argument** (`core/formula.py::_function_call`). Arguments are evaluated
  where the call is, so an unknown goes into the body as itself, and the
  body is exactly as linear, or as deferred, as it would be written
  inline. `line(s.x) == 7` solves with the default solver; `sq(s.x) == 9`
  needs `newton`, just as `s.x * s.x == 9` would. Every surface that
  evaluates a formula gets calls with no changes: rules, checks, `\val`,
  `\work`, `why`, sweeps, `fit`, uncertainty and solve health.
- **A bare name in the body is a parameter first, then a property of the
  function mote.** That is the same anchor rule a rule's bare names
  follow, which is what lets constants live on the function mote. A
  qualified name (`reactor.k`) reaches any mote, as usual. A parameter
  shadows `t`, `pi` and `e` inside the body, so `decay(t) = exp(-k * t)`
  means the parameter.
- **A parameter's unit is the function mote's ordinary `units` entry.** It
  says what a *bare number* passed to that parameter means: `phi(0)` is
  0 cm. A number with no declared unit is base SI. An argument that reads
  a property already carries that property's unit, so it is never
  reinterpreted. `[1]` declares a parameter dimensionless.
- **The name resolves like a garden-scope name**: the one mote in the
  garden with that name *and* a `function` body. Two is the same hard
  error two `bd`s are, and none is an unknown function. A function cannot
  be named like a builtin, `t`, `pi` or `e`, since a call to one could
  never reach it.
- **Refused:** the wrong number of arguments (the message names the
  parameters), a function that calls itself, directly or not, and keyword
  arguments. An error inside a body says which function it came from.
- A function mote is **never solved**. It has no freedoms and no rule
  applies to it; it is read from, like a property model.
- **The unit check walks into the body** (`core/dimensions.py`), with
  each parameter given its declared dimension, or else its argument's. A
  problem inside the body is the calling equation's problem, and a
  solved value's unit is inferred through a call as through any
  expression. Two warnings are about calls themselves: an argument whose
  dimension is not the parameter's declared one, and **a bare number
  passed to a parameter that declares no unit**, since `phi(5)` read as
  5 m when 5 cm was meant is the slip this exists to catch. A declared
  parameter, including `[1]`, never warns.
- **Solve health lists a function no call can reach**
  (`Health.function_problems`): a builtin's name, unreadable
  parameters, or a body that does not parse. These are reported even
  when nothing calls the function yet.

### Limits

`sin(B r)/r` is 0/0 at r = 0, and the number anyone means there is the
limit, B. **At a function call, and only there**, a body that lands
exactly on 0/0 is answered with its two-sided limit (`core/limits.py`),
and the solve says so:

```text
Solved 1 value across 1 mote from 1 equation. 1 value came from a limit (phi: r → 0 cm).
```

- **Only at a call**, because there the parameter is the variable to
  approach along. A plain rule that hits 0/0 (`reactor.r = 0`, `flux =
  sin(B * reactor.r) / reactor.r`) has no such variable, since any of its
  givens could be the one meant, so it stays an error. The error says to
  write the expression as a function or use `sinc`.
- **0/0 only.** A nonzero number over zero is a pole and stays a division
  by zero. Two sides that settle on different values are a jump, and a
  side that never settles has no finite limit. Each of these fails with
  its own message rather than averaging into a number.
- **How:** from each side, in steps that halve from a starting step,
  Richardson-extrapolated to a zero step. The starting step is a
  thousandth of the parameter's declared unit (a thousandth of a cm for
  `r [cm]`), or else a thousandth of the point's size, or of 1. It is
  retried a thousand and a million times finer before giving up. With
  several parameters, the limit is approached along each in turn, and the
  first that settles is used.
- **Reported everywhere the answer is:** `SimulationResult.limits`,
  `TimeSeriesResult.limits` and each step's summary. It is also a
  `limit` read in provenance (the function mote, and `r → 0 cm` in the
  key's place), which is how `why` and `\work` say it.
- `sinc(x)` gives the same number with no limit: `A * B * sinc(B * r)`.

### Calculus

A function can be integrated or differentiated along one of its
parameters, numerically (`core/calculus.py`). The first argument is the
function mote's bare name:

```text
reactor.Q  = integrate(phi, 0, reactor.R)      <- ∫₀ᴿ φ(r) dr
reactor.g  = deriv(phi, reactor.R)             <- φ′(R)
plate.I    = integrate(f, 'y', 0, 1, plate.x)  <- ∫₀¹ f(x, y) dy at x = plate.x
plate.dfdy = deriv(f, 'y', 0.5, plate.x)       <- ∂f/∂y at (x, 0.5)
```

- **The parameter it is along** is named as a quoted string, which may be
  left out when the function has exactly one parameter. Then come that
  parameter's two bounds (an integral) or its point (a derivative), then
  every other parameter's value in declared order, held fixed. The wrong
  count, an unknown parameter name, or a function of several parameters
  with none named is refused, and the message says what the call takes.
- **Arguments are read as a call's are**: a bare number takes the
  parameter's declared unit (`integrate(phi, 0, 10)` into `r [cm]` is 0
  to 10 cm), and an argument that reads a property carries its own.
- **An unknown bound or point defers**, like `sin`. The substitution loop
  retries it once another equation has solved it, and `newton` solves
  through it: `integrate(q, 0, m.L) == 8` finds `L`, and `deriv(p, m.x)
  == 0` finds a stationary point. A body that reads an unsolved value
  defers the same way.
- **Bounds either way round**: `a > b` gives the negated integral, and
  `a == b` gives 0. Infinite bounds are not supported.
- **Composes.** A body may itself call `integrate` or `deriv`, so a second
  derivative is a function mote whose body is `deriv(g, x)`. A function
  that integrates itself is refused, as any recursion is.
- **How:** an integral is adaptive Gauss–Kronrod (7 points within 15),
  splitting the worst piece until the error estimate is within 1e-10 of
  the integral (or 1e-12 of ∫|f|, for one that cancels to 0). Kronrod
  nodes never sit on a bound, so an integrand undefined there (sin(r)/r
  from 0) is never asked there. A node that lands exactly on 0/0 inside
  the bounds is answered with its [limit](#limits). A derivative is
  Ridders' method: central differences Richardson-extrapolated over a
  shrinking step, which starts at a tenth of the parameter's declared
  unit (or of the point's size, or of 1). It is retried from a thousand
  and a million times finer before giving up.
- **Fails rather than guesses:** a pole inside the bounds, a kink (`abs`
  at 0: the left and right slopes are named), a function undefined beside
  the point (`sqrt` at 0), or an estimate that does not reach tolerance.
  Each says which call and why.
- **Units:** an integral is the body's unit times the parameter's, and a
  derivative is the body's over it, so `integrate(force, 0, 3)` with
  `x [m]` and a body in N is in J. The bounds, point and held arguments
  get the same two warnings a call's arguments do.
- **Reported in provenance, not the summary.** Each estimate is an
  `integral` or `derivative` read (the function mote, and `∫ r 0 cm → 10
  cm` or `d/dr at 10 cm` in the key's place, with the relative error when
  it is above float roundoff). That is how `why` and `\work` say it:
  `\work` notes it on the result, the way it notes a limit. ± propagation
  works through either, since it differentiates the whole solve.
- **LaTeX** sets `integrate(phi, 0, R)` as ∫₀ᴿ φ and `deriv(phi, x0)` as
  φ′(x₀). With a parameter named, the integrand is written out with the
  held arguments after a semicolon (∫ f(y; x₀) dy, ∂f/∂y(y₀; x₀)), since
  the typesetter has no garden to put them back in declared order.

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
**attribute position** is a role. A name that means a property cannot
start meaning a role, because reading an attribute off anything other
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

A complete Rankine cycle in a garden called `rankine-cycle-example`.
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
scenario solves and reports what *would* change; `run_time_series` under a scenario is a preview like any
other. To compare scenarios, preview them side by side: two `sim` panes
under different `--scenario`s, or one with `--vs` for a difference
column.

**Editing one.** `set --scenario hot C1.voltage 30` writes `voltage` on
the override through which `hot` replaces C1 -- creating the override
and its two relationships the first time, and the scenario mote itself
when no mote has that name (`core/scenarios.py::get_or_create_override`;
one undo step). `--clear` takes the override's value away, so the
scenario reads the garden's again. A unit written on an override is
merged into the target's `units` map, since an override replaces whole
properties.

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

### Knobs: a trial value with no mote

A scenario is a mote, so trying one value still means making motes. A
**knob** is the lighter form: `--set bd.E=200 GPa` on a `plot`, `sim`, `fit`
or `graph --flow` line (`core/knobs.py`). The solves take knobs as the same
`{mote_id: {key: value}}` overrides a sweep point does, laid over the
scenario's (`scenarios.overlay`): `run_time_series(overrides=…)`,
`trace(overrides=…)`, `sweep.Case.overrides`. Nothing is written, and
`run_simulation`, the solve that writes, takes none. A typed unit is
converted into the property's own unit, never relabelled; `--keep`
writes the knobs through `set`'s writer when the trial turns out to be the
answer. See `console.md` -> *Settled for knobs*.

## Sweeping

A scenario lets a garden hold a second answer. A **sweep** lets it hold a
range of them: `core/sweep.py` re-solves the whole garden once per step
of a property's range and reports another property at every step. That
is a curve rather than a state, which is why it is a module of its own
rather than an option on `run_simulation()`.

Its surface is Moxie, the console's `plot` pane (`plot bd.sigma vs bd.l
4:24`; `core/plotcmd.py` turns the line into a specification,
`core/plotview.py` draws it; `docs/ui-setup.md` → *Plots*).

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

No write-back, no scenario motes created and destroyed
around the edges. A sweep leaves the garden exactly as it found it,
which is what makes it safe to fire on every frame of a dragged slider.
This is not a default to be lifted later: the answer to "what would this
be across that range" is not the garden's state, for the same reason a
[scenario](#scenarios) run is never written.

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
and a caller reading eight properties off one solve pays for one. The
plot pane states the number of solves in its status line rather than
capping it.

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
connection and solve outside it, so a sweep need not hold the connection
against the keystroke meant to cancel it. The console's plot pane does exactly this (`plotview.read_for` under
the session lock, then `figure_from_spec(ground=…, should_cancel=…)`
outside it), and a newer request from the same pane cancels the older. `indices`
(and `run_surface`'s `stride`) compute a subset of the points, so a
coarse pass can be drawn before a fine one refines it, and
`should_cancel` is polled between points.

### Saved plots

`plot` is a reserved property (`core/reserved.py`). A mote carrying one
is a saved plot: **the specification** -- which motes, which keys, which
ranges, which scenarios -- and never the points it produced. Reopening
one therefore re-solves the garden as it stands now, which is the whole
difference between a saved plot and a screenshot. Written by `plot save
‹name›` (`core/plotview.py::save_plot`, an instance of `Saved Plot`),
opened by `plot ‹name›`, drawn in notes by `\graph{name}`, and shaped
exactly like `query`, Monolith's saved view. The console's `plot` line
is the same specification typed out (`core/plotcmd.py`):

| Line | Specification |
|---|---|
| `plot A.y B.z vs A.x 0:10 --steps 50` | `mode: sweep`, `x: {mote, key, lo, hi, steps}`, `series: [{id, mote, key, scenario}]` |
| `… by A.w 1:3` | `mode: surface`, `x`, `y`, `output: {mote, key}` |
| `y vs x --across` | `mode: correlate`, `xKey`, `yKey` |
| `--set A.k=5`, `--scenario s`, `--log` | `knobs: [{mote, key, value}]`, `scenario` (each series', and a surface's or correlation's own), `yScale` |

## Fitting

`core/fit.py` runs the other way from a sweep: given measured points, it
finds the inputs that make the garden's answer pass through them.

    fit C1.current vs C1.voltage --data I vs V [--from bench.csv] --free C1.r_a [--scenario hot] [--apply]

- **The model** is `C1.current` as the garden solves it with `C1.voltage`
  set to a point's x (the same perturbed solve a sweep makes).
- **The data** is every mote carrying both data keys as numbers (`I`,
  `V`), the model's own motes excepted; `--from` narrows it to one CSV
  import's rows (`imported_from`). A data unit converts into the model's
  where both declare one.
- **The fit** moves the `--free` inputs from their current values by
  Levenberg–Marquardt (numpy): a forward-difference Jacobian, one extra
  solve per input per iteration, until the sum of squares stops falling.
  About (inputs + 1) × points × iterations solves.
- **The answer** is each value with its σ from s²(JᵀJ)⁻¹, RMS, R², the
  iteration count and whether it converged. A point that fails to solve
  at the start is reported and left out. Inputs the data can't tell apart
  (a singular JᵀJ -- `r_a` and `r_b` when only their sum shows) come back
  with no σ and a message saying so.
- **`--apply`** is the only write: each value, its σ into `uncertainty`,
  and `source: {kind: fitted, note: ‹the line›}`, one undo step. So
  `\given` and the V&V table say where the number came from.

Surfaces: the console's `fit` pane (`docs/ui-setup.md`), `monoss fit …`,
and MCP `fit_model`.

## The worksheet

`core/worksheet.py` is a garden's calculation as lines of text, top to
bottom:

```text
bd.x = 80 in
bd.y = 60 in
bd.Ax = ?
bd.Bx = ?

bd.l = sqrt(bd.x^2 + bd.y^2)
bd.Ax + bd.Bx == 0            ! ΣFx
```

```
monoss worksheet read
monoss worksheet apply --file sheet.txt      # or on standard input
```

In the console it is the `worksheet` pane: typing only reads each line
(`core/worksheet.py::preview` -- its kind, why it can't be written, and
an unknown's current value), and `esc` applies the sheet as one undo step
(`apply_undoable`, which compares the garden before and after). See
`docs/ui-setup.md` → *The worksheet*.

`FEA-Homework-4` is the case this exists for: 10 motes and 10
relationships to express about 9 numbers and 5 equations, built one dialog
at a time. Six of those relationships existed only so `bd.l` would resolve
— [garden-scope names](#name-references) deleted those — and the rest was
a mote and a rule per line. Eight lines say the same thing.

| line | means |
| --- | --- |
| `bd.x = 80 in` | a **given**: the property, and `in` in the mote's `units` map |
| `bd.l = sqrt(...)` | an **equation**: a rule mote, `applies_to bd`, and `l` free on `bd` |
| `bd.l [m] = sqrt(...)` | an equation that declares `m` for `l`, refused if its right side is not a length |
| `bd.Ax = ?` | an **unknown**: `Ax` free on `bd`, with no value |
| `bd.Ax [N] = ?` | an unknown that declares `N` for `Ax` |
| `bd.Ax + bd.Bx == 0` | a **relation**: an equation, and no freedom declared |
| `bd.sigma <= 250` | a **check**: evaluated, never solved |
| `b.R1 + b.R2 ?= b.W` | an **equality check**: stored as `==` with `check: true` |
| `bd.x = 80 ± 0.5 in` | a given with a ±, in its `uncertainty` entry (`± 2%` is relative) |
| `phi(r [cm]) = A * sin(B * r) / r` | a **function**: the mote `phi` with that `function` body, `parameters: [r]`, and `cm` for `r` in its `units` (see [Functions](#functions)) |
| `... ! ΣFx` | names the rule that line makes |
| (blank) | ignored |

A single `=` is a given when its right side is a bare number, an unknown
when it is `?`, and an equation otherwise. That is the whole rule, and it
is what the shapes above already read as: `80 in` is not an expression (`in`
is a Python keyword, so it cannot even parse as one) and `sqrt(bd.x^2 +
bd.y^2)` obviously is.

**`=` and `==` differ in one way that matters.** Both make an equation;
only `=` also declares its left side a freedom. `=` is the assignment —
*solve for this* — and `==` is a relation between values that are free for
some other reason, which is how a residual is written without the parser
guessing which of two unknowns it determines. `= ?` is how the person who
wrote that residual says which values it may determine.

**`[unit]` on the left declares the unit of the value a `=` line solves
for**, so the line that says what to solve for also says what the answer
is measured in. The line is refused (struck in the margin as you type,
and not written) when its right side has a different dimension:
`reactor.leakage [s] = 4 * pi * reactor.r^2 * current(reactor.r)` says
*left side is s but right side is Hz*. The check reads the garden as the
whole sheet would leave it, so a given corrected a few lines up counts.
Too few units to tell is not a problem. Leave the brackets off and the
value declares nothing, so a unit it declared is taken away and the solve
infers one (see [Inference](#inference)). `read()` writes `[unit]` back
for every freedom that declares one, which is how a stale unit shows up.
A given's unit stays after its number (`bd.x = 80 in`), and a unit on a
`==` line, a check or a comparison is refused: those solve for nothing.

**A rule a line makes is unnamed.** A name that copies its equation is a
second account of it that drifts the moment the equation is edited — which
is exactly what `FEA-Homework-4`'s rule names had done. So the equation
*is* the label (`core/rules.py::label_of`), everywhere a rule is named: in
a solve's messages, in `why`, in a table row. A trailing `! ΣFx` writes a
real name, for when a person has a better word for it than the algebra.

### A projection, never a store

There is no `worksheet` property and no saved source text. `read()` renders
the garden's equations and their inputs back as lines; `apply()` writes
lines into ordinary motes, rules, `applies_to` and freedoms. That is
`philosophy.md`'s rule — "editors are shortcuts over the graph, never a
separate store" — taken at its word, and what it costs is real:
**comments, blank lines and the order you typed in are not recoverable.** A
line's `! name` survives because it becomes the rule's `name`; nothing else
about the text does. `read()` gives back its own order instead: one block
per independent calculation (equations that share a mote), a blank line
between blocks, each block's givens and unknowns first and every equation
after the ones that work out what it reads.

`read()` is **complete** for what it covers, which is what makes the round
trip safe: applying what it returns deletes nothing and widens nothing. Two
details make that true. A solved freedom is never printed as a given — it
is the answer, and printing `bd.l = 100` would pin the value the worksheet
solves for. And a rule is rendered with `=` only when its left side is
already a freedom; otherwise it keeps `==`, since a `=` would make that
property free on the next apply.

### Applying replaces

The lines are the whole set of the garden's `once`-scoped equations, so **a
rule whose line is gone is deleted**. That is what makes the editor an
editor rather than an append-only log, and it is the one destructive thing
here — `Applied.deleted` names what went. Nothing is deleted while any line
fails to parse, so a half-written worksheet cannot take the rules out from
under the lines that are fine.

**A rule about a species is never rendered or deleted.** It is asserted
once per instance — a reusable model, which species and templates are still
for — and is not a line. `Applied.untouched` counts them, so a surface can
say so rather than implying a worksheet is the whole garden.

**A function line** is a call on the left of a single `=`, which no other
line can be, since an assignment's left side is always `mote.key`. Each
parameter may carry a unit in brackets. Its constants are ordinary given
lines (`phi.A = 5e13`). `read()` puts every function, with its
constants, in a leading block of definitions. Applying replaces here too:
a function whose line is gone loses its body and parameters, and the mote
itself is deleted when nothing else is left on it (`Applied.deleted` says
`phi()`).

One asymmetry worth knowing: applying **adds** freedoms and never removes
one. A freedom may have been declared in another surface about a mote the
worksheet happens to mention, and taking it away would be reaching past
what the lines are about.

## Solve health

```
monoss health
```

```text
3 equations, 3 unknowns -- underdetermined
  2 unknowns (bd.Ax, bd.Bx) share 1 equation(s) -- another equation needed
  1 unknown(s) (bd.l) have 2 equations -- 1 more than needed, which is fine
    as long as they agree
```

A solve either answers or fails with a message about the system as a whole.
That is correct and useless: "more unknowns than independent equations" is
true of the whole garden and tells nobody which line to go fix.
`core/health.py::check()` answers **before anything is solved**, from what
already exists — `core/incidence.py` for the counts and the culprits,
`check_garden` for the units. Nothing there is re-derived.

**The count is not a verdict.** `equations == unknowns` is neither
necessary nor sufficient: overdetermined is normal and fine as long as the
extra equations agree (in any circuit one Kirchhoff current equation is
implied by the others), and the example above has three of each while being
genuinely underdetermined. So the verdict is read off the **blocks**, never
off the totals.

### Blocks are the smallest thing worth naming

`Index.blocks()` cuts the garden's free values into coupled groups: two
values are in one block when some equation could determine both,
transitively, and an equation belongs to the block of the values it
determines. A block is the smallest group whose counts have to balance on
their own, because no equation outside it can determine anything inside it.
Naming which block is short is the difference between a message somebody
can act on and one they cannot.

A constraint determines nothing, so it is in no block at all — which is the
structural form of "an inequality cannot make a deficient group solvable".
A free value no equation determines comes back as a block of one with no
equations, so "nothing constrains this" and "these three share two
equations" are the same answer read at two sizes.

Alongside the blocks, `check()` reports:

- **`culprits`** — free values nothing can determine, and values an
  equation reads that nothing holds. The part somebody has to act on.
- **`inert`** — equations that touch nothing the solver may determine. Not
  an error (a check is *meant* to constrain rather than determine), but
  when a system is overdetermined this is the honest shortlist, rather than
  pretending one equation can be blamed.
- **`unit_problems`** — the same dimensional check `unit check` runs, and
  already per line: each problem is labelled with the rule it was found on,
  and a worksheet rule is labelled by its own equation, so a warning reads
  back as the line that caused it.
- **`function_problems`** — each [function mote](#functions) no call can
  reach or evaluate: a builtin's name, unreadable parameters, a body that
  does not parse.
- **`checks`** — comparison relations, counted apart from the equations.
  Counting one among them would make a model look balanced because of a
  line that cannot determine anything.

`solvable` means nothing is structurally wrong, deliberately not "will
solve": a block whose counts balance can still be singular or inconsistent,
and only running it can say. A scenario can be passed to ask the same
question of a hypothetical, which matters most when it changes `freedoms`
rather than a value.

### Display units versus stored units

The unit in a mote's `units` map is the unit the value is **stored** in,
and solving never changes it (see [Units](#units)). What a *surface* shows
is separate, and deliberately not in the garden: it is view state, so
`core/` offers only `core/units.py::convert` and
`core/health.py::stored_unit`, and no display preference is written
anywhere. A worksheet typed in inches whose `l` comes out as `2.54` is
correct — the equation named metres, so the solve labelled it `m` (see
[Inference](#inference)) — and showing `100 in` is the reader's own choice,
converted on the way to the screen.

## Dependency queries

`why` explains one solve of the garden as it stands. These four read the
*rules*, so they answer without solving at all, and they answer about the
model rather than about one solve:

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
Three providers are built in, all in SI units:

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
  to get its ideal outlet enthalpy). Quality (vapor mass fraction, 0-1,
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
- `materials` (`core/materials_provider.py`), structural material
  properties shaped like `coolprop` (decided): configuration `{"material":
  "6061-T6"}` names a bundled alloy by name or alias (case, spaces and
  hyphens ignored), and operations take no inputs --
  `youngs_modulus()`, `poisson_ratio()`, `yield_strength()`, `density()`,
  and `shear_modulus()` (E / 2(1+ν) unless given). A rule reads one
  through the part's material, `stress <= property('material',
  'yield_strength')`, so the number is never copied onto a mote and
  changing the material is editing one configuration. The data is a file,
  `core/data/materials.json`, not code: nominal, room-temperature,
  typical handbook values with their source stated in the file. A garden
  gives `values` (SI) in the configuration to override one property or to
  define a custom material. Temperature-dependent operations
  (`youngs_modulus_t(temperature)`) wait for published data. `monoss
  model materials` and MCP `list_materials` list what is bundled.

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
it gets `14.1`. The same is true of the changes a solve reports and of
every step of a time-based run.

An unrecognized unit name fails the run rather than being ignored --
treating `kohm` as if it were already ohms would be a silent
factor-of-1000 error in the answer. A malformed `units` property (not a
map, or with non-string values) leaves the mote un-annotated instead of
breaking the run.

### Inference

A **freedom** is different from any other property: the solver writes a
number onto it, and if the equation that determines it names a dimension
in plain sight, showing the result unitless would throw away information
the garden already had. So when a freedom has no `units` entry of its
own, the equation that determines it is read and, if it reduces to the
bare freedom on one side (`resistance == voltage / current`), a unit is
read straight off the dimension of the other side:

```
resistance:               <- solved, previously undeclared
voltage:     10
current:      2

units:  {"voltage": "V", "current": "A"}     <- resistance reads in ohm
```

`resistance` comes out as `5`, in ohms. **The inferred unit is never
stored**: the solve writes `5` and leaves `units` as it was, and every
surface that shows the value works the unit out again when it reads it
(`inferred_units()`, which reads dimensions and solves nothing). A unit a
solve saved would read as declared from then on and stop following its
equation: retype a given from `1/cm*s` to `1/(cm*s)` and the result has
to go from `s` to `Hz`, not keep the `s` it was first given. A garden
from before this has its saved units dropped once, on open, where they
equal what is inferred today (`core/db.py`, `PRAGMA user_version` 1); one
that differs stays, and reads as declared.

Inference repeats until it finds nothing new, so a unit inferred for one
freedom feeds the next (`y == 2 * x` once `x` has one). A dimension with no name of
its own (`distance / time`) falls back to the base-SI product, written
in the same caret notation a person typing a unit by hand would use
(`m*s^-1`) via `describe_expression()`, rather than the unicode
superscripts `unit list` and `describe()` use for an error message
(`m·s⁻¹`) -- so a value the solver named on its own looks exactly like
one a person named, once `format_unit()` renders either back with
superscripts for display. Leaving the property silently unnamed would
be worse than either.

This never overrides a unit a mote declares, and only ever fires when the freedom is the **entire** left-
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

A garden called `radiating-panel-example`: two radiating panels solving `power == emissivity * stefan_boltzmann *
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

  There is no `dt` anywhere in the engine, and no seconds: **a tick is one re-solve of the equation set**, not an integration step. The console's `sim` says so rather than implying a clock -- `--ticks`, the timeline and the playhead all count ticks. An integrator would reach `sim --solver` by being registered in `core/solver_registry.py`, which is the only registration point either side reads.
- **Symbolic calculus.** `integrate` and `deriv` are numeric. Symbolic derivatives (and, for `\work`, the derivative written out as an expression) are planned next; symbolic antiderivatives would need a computer-algebra dependency. Time derivatives (`dx/dt` in a rule) wait on the continuous-dynamics question above.
- **Per-literal unit syntax in an equation** (`100[ohm]`) -- a bare number takes on the unit of whatever it's compared with. (A solved freedom with no unit of its own *is* now given one -- see [Inference](#inference) above -- but only when it is the entire side of the equation that determined it; anything shapelier still has to be declared.)
- **Non-physical units.** Currencies, story points, and other non-SI "units" aren't in the dimension system. A passthrough dimension for them could be added without disturbing the design.
- **A default solver per garden/species.** `solver` is a per-call argument only; there's no way to persist "this garden always uses `newton`" so every caller has to name it.
- **Species inheritance.** Species are flat.

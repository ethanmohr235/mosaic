# Simulation

MonOSS can solve for unknown mote properties from equations you write yourself. The engine knows nothing about circuits, thermodynamics, or any other domain -- all of that lives in ordinary motes.

Enter a number of steps and click **Simulate** to preview a time-based
run: every step is solved in memory, nothing is written to the garden
yet, and a bar at the bottom of the graph lets you step or play through
the whole sequence -- the graph itself stays visible and interactive
throughout, with changed motes getting a dashed ring on the canvas and
their projected values readable straight from the inspector panel
(which flags itself read-only while previewing). Choose **Commit this
tick to real graph** to write the scrubbed tick for real, or **Discard**
to throw the preview away. See "Time-based simulation" below.
Programmatically, a single steady-state solve is
`core.simulation.run_simulation(conn)`; a time-based run is
`core.simulation.run_time_series(conn, step_count, commit)` (see "Run
history" below for its `save` option).

## The three pieces

**A species** declares what the solver is allowed to determine, via an `unknowns` property:

```
name:      Node
unknowns:  voltage
```

`unknowns` accepts a comma-separated string (`voltage, current`) or a JSON list (`["voltage"]`). Every mote with an `instance_of` relationship to this species now has a solvable `voltage`.

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

Every equation is `left == right`, exactly one `==`.

| Syntax | Meaning |
|---|---|
| `resistance` | A property of the mote the rule is running against. If the species declares it as an unknown, it becomes something to solve for; otherwise it must already hold a number. |
| `targets('a')` | The motes this one points at via a relationship of type `a`. |
| `sources('a')` | The motes pointing *at* this one via a relationship of type `a`. |
| `targets('a').voltage` | That property on those motes. Collapses to a single value when exactly one mote is reached. |
| `sum(...)`, `count(...)` | Combine several motes into one value. Required when more than one mote is reached. |
| `+ - * / **` | Arithmetic. Parentheses work as usual. |
| `abs`, `min`, `max` | Only on values that are already known. |
| `t` | The current step's index within a time-based run (0 for the first step). A reserved word -- shadows a property literally named `t` inside a formula specifically, though that property is still ordinary and visible everywhere else. Always 0 in a plain (non-time-based) run. |
| `prev(x)` | The value `x` had *entering* the current step -- `x` must be a bare property/unknown name on the mote the rule is running against (not `targets(...)`'s result). This is how a rule expresses a recurrence: `balance == prev(balance) * 1.05`. Only meaningful in a time-based run; raises an error otherwise. |
| `property('rel', 'operation', ...)` | Evaluates `operation` (e.g. `'pressure'`) through the property model of the single mote reached by relationship type `'rel'`, e.g. `property('working_fluid', 'pressure', density, temperature)`. The relationship type and operation must be quoted string literals; the remaining arguments must already be known numbers -- a property model is an opaque function of its inputs, so like `abs`/`min`/`max` this is deferred (retried by the substitution loop) until they are. See "Physical property models" below. |

Anything else -- indexing, comprehensions, attribute access, `import`, calling arbitrary functions -- is rejected when the formula is parsed. The parser uses Python's `ast` module against a whitelist and never calls `eval`.

## What the solver can handle

Solving runs in two stages:

1. **Substitution.** Any equation that has come down to a single unknown is solved outright and the answer fed back in, repeatedly. This is what makes a nonlinear formula work when its nonlinear part involves values that are already known:

   ```
   pressure    == sources('feeds').pressure * ratio
   temperature == sources('feeds').temperature
                  * (pressure / sources('feeds').pressure) ** 0.286
   ```

   The first equation determines `pressure`, which lets the second one evaluate.

2. **Linear solve.** Whatever remains goes to a single least-squares solve. Feedback loops are fine here -- a circuit's node voltages and branch currents all depend on each other and are found together.

An equation that is nonlinear *in the unknowns themselves* (`x * x == 4`) can't be solved and is reported as such.

## Errors

The engine refuses to guess. All of these stop the run with a message naming the motes involved:

- **underdetermined** -- more unknowns than independent equations. Usually a missing rule or a missing reference point (a circuit needs a ground).
- **inconsistent** -- no values satisfy every equation at once.
- **contradictory** -- two rules demand different values for the same thing.
- **could not be resolved** -- an equation stayed nonlinear in its unknowns.

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
also why nothing is unknowns-idempotent mid-run the way a single solve
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

A single `run_simulation()` call (used by the worked example script, and
equivalent to committing a 1-step time series) writes solved values
straight onto the motes and is idempotent: unknowns are re-derived from
the species every time, so a solved value never gets mistaken for a
fixed input.

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
writes), `False` for a saved-only run.

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

## Not built yet

- **Continuous dynamics.** Time-based simulation is discrete steps, not an ODE integrator -- a numerical approximation, deliberately, not SPICE-style companion models. Fine for recurrences; genuinely continuous behavior (capacitors and inductors modelled from first principles, rather than as a discrete update rule) would need real integration.
- **Units.** Properties are plain numbers. Use naming conventions (`resistance_ohms`) if it helps; nothing enforces or converts.
- **Nonlinear systems.** Only nonlinearity over already-known values works. Genuinely nonlinear systems would need an iterative solver.
- **Species inheritance.** Species are flat.

# Simulation

MonOSS can solve for unknown mote properties from equations you write yourself. The engine knows nothing about circuits, thermodynamics, or any other domain -- all of that lives in ordinary motes.

Enter a number of steps and click **Run simulation** to preview a
time-based run: every step is solved in memory, nothing is written to
the garden yet, and a scrub overlay lets you step through the whole
sequence before choosing **Commit this step** to write it for real (or
**Cancel** to discard the preview). See "Time-based simulation" below.
Programmatically, a single steady-state solve is
`core.simulation.run_simulation(conn)`; a time-based run is
`core.simulation.run_time_series(conn, step_count, commit)`.

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
started, at whichever step you're looking at.

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

Only a *committed* run is ever logged to `simulation_runs` and
`simulation_changes` -- including a failed single-step run, with old and
new values for each change. A discarded preview leaves no trace. The
per-garden tick counter (unrelated to a time-based run's own step
numbering) comes from that log.

## Not built yet

- **Continuous dynamics.** Time-based simulation is discrete steps, not an ODE integrator -- a numerical approximation, deliberately, not SPICE-style companion models. Fine for recurrences; genuinely continuous behavior (capacitors and inductors modelled from first principles, rather than as a discrete update rule) would need real integration.
- **Live scrub visualization on the graph.** The scrub overlay's own summary/changes list is the display; the graph canvas itself doesn't yet recolor or reposition nodes as you drag the slider.
- **Units.** Properties are plain numbers. Use naming conventions (`resistance_ohms`) if it helps; nothing enforces or converts.
- **Nonlinear systems.** Only nonlinearity over already-known values works. Genuinely nonlinear systems would need an iterative solver.
- **Species inheritance.** Species are flat.

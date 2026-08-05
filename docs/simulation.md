# Simulation

MonOSS can solve for unknown mote properties from equations you write yourself. The engine knows nothing about circuits, thermodynamics, or any other domain -- all of that lives in ordinary motes.

Run a simulation with the **Run simulation** button, or `core.simulation.run_simulation(conn)`.

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

## Running and re-running

Each run writes solved values straight onto the motes, and re-running is idempotent: unknowns are re-derived from the species every time, so a solved value never gets mistaken for a fixed input. Change an input, run again, and everything downstream updates.

Every run is logged to `simulation_runs` and `simulation_changes` -- including failed ones, with old and new values for each change. The per-garden tick counter comes from that log.

## Not built yet

- **Time-dependent behaviour.** Solving finds a steady state; there is no `prev()` and no notion of a previous timestep. Capacitors and inductors need this. The intended approach is SPICE-style companion models, which turn a transient into a sequence of static solves and so reuse everything here.
- **Nondestructive forecasting.** Runs mutate the live garden. Simulating a copy and choosing whether to keep the result is the eventual goal.
- **Units.** Properties are plain numbers. Use naming conventions (`resistance_ohms`) if it helps; nothing enforces or converts.
- **Nonlinear systems.** Only nonlinearity over already-known values works. Genuinely nonlinear systems would need an iterative solver.
- **Species inheritance.** Species are flat.

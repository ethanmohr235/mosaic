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

A rule is a mote with an `equation` property and an `applies_to` relationship pointing at a species. The engine runs each rule against every instance of its species. A species declares which of its instances' properties the solver may determine, via an `unknowns` property.

`instance_of`, `template_of` and `applies_to` are reserved relationship types; `equation`, `unknowns`, `url`, `date`, `duration`, `recurrence` and `gcal_event_id` are reserved property names. Everything else is ordinary user data. Reserved property names are still just ordinary, editable/deletable properties -- nothing in the schema enforces them, they're only recognized by convention (the UI/engine looks for that exact key).

Equations are written in a small expression language parsed against a strict whitelist -- never `eval`. Evaluating one produces a weighted sum of unknowns plus a constant, so an expression that happens to contain no unknowns is simply a number. That is why there is only one kind of rule: an assignment and a coupled equation are the same thing to the solver.

Solving happens in two stages. A substitution loop repeatedly resolves any equation that has come down to a single unknown and feeds the answer back, which allows nonlinear formulas as long as the nonlinear part involves values already known. Whatever remains must be linear and goes to a single least-squares solve.

A run can be time-based: `core.simulation.run_time_series()` solves a requested number of discrete steps, each one starting from the *previous* step's solved values rather than the database, so a rule can use the reserved `t` (the current step index) or `prev(x)` (the value `x` had entering the step) to express time-dependent behavior or a recurrence. This is computed entirely in memory and nondestructive by default -- nothing is written, and nothing is logged -- until a specific step is explicitly committed, which re-solves up to that step (deterministic, given the same garden and rules) and writes only the cumulative result in one shot. A single steady-state `run_simulation()` call is equivalent to committing a 1-step time series.

Only a committed run is recorded in `simulation_runs` and `simulation_changes`, failures included -- a discarded preview leaves no trace. The per-garden tick counter is derived from that log, and is a different thing from a time-based run's own step numbering (which resets to 0 every run and is never persisted). See `docs/simulation.md` for the language reference and what is deferred.

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
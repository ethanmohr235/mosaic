Yes. I think the previous idea needs one important refinement before you build it:

> **Do not make “Model” just another container around equations.**
>
> Make MonOSS represent a model as an explicit **computational graph over a graph of real objects**.

That gives you a clean path away from Excel while preserving the flexibility that made the current rule/property system attractive.

I'm going to propose this as if we were designing the actual MonOSS architecture from scratch, but with a migration path from what you already have.

# 1. The fundamental architecture

MonOSS should distinguish five things:

```text
┌─────────────────────────────────────────────┐
│                  ONTOLOGY                   │
│                                             │
│  Species, property definitions, relation    │
│  definitions, types, capabilities           │
└──────────────────────┬──────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────┐
│                   SYSTEM                    │
│                                             │
│  Actual motes and their relationships       │
│                                             │
│  Turbine A → Condenser B                    │
└──────────────────────┬──────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────┐
│                   MODEL                     │
│                                             │
│  Computational description of how this      │
│  system behaves                             │
└──────────────────────┬──────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────┐
│                COMPUTATION                  │
│                                             │
│  Nodes that consume values and produce      │
│  values                                     │
└──────────────────────┬──────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────┐
│                 EXECUTION                   │
│                                             │
│  Runs, results, provenance, diagnostics     │
└─────────────────────────────────────────────┘
```

The crucial distinction is:

### Ontology

What kinds of things exist?

```text
Turbine
Pump
Pipe
Fluid
```

### System

What things actually exist in this garden?

```text
main_turbine
feedwater_pump
condenser_pipe
steam
```

### Model

How do these things behave together?

```text
Rankine Cycle Model
```

### Computation

What transformations occur?

```text
temperature + pressure
        │
        ▼
   CoolProp
        │
        ▼
    enthalpy
```

### Execution

What actually happened during a particular run?

```text
Run #184
```

That separation is the backbone of the entire proposal.

---

# 2. The core new abstraction: `Computation`

I would introduce a first-class object called a **Computation**.

Every calculation in MonOSS eventually becomes one.

Conceptually:

```python
class Computation:
    id: UUID
    name: str
    kind: str

    inputs: list[InputBinding]
    outputs: list[OutputBinding]

    implementation: Implementation
    metadata: dict
```

A computation says only:

> **I consume these values and produce these values.**

For example:

```text
Computation: turbine_power

Inputs
    mass_flow
    inlet_enthalpy
    outlet_enthalpy

Outputs
    power

Implementation
    power = mass_flow * (inlet_enthalpy - outlet_enthalpy)
```

This immediately replaces the ambiguity of your current rule equations.

Instead of MonOSS seeing:

```text
Rule:
    power = mass_flow * delta_h
```

and having to infer everything from that string, it stores:

```text
mass_flow ────────┐
                  │
inlet_enthalpy ───┼──► turbine_power ───► power
                  │
outlet_enthalpy ──┘
```

That graph is explicit.

---

# 3. Computations need a common interface

Every computation type should conform to the same conceptual interface:

```python
class Computation:
    def validate(self, context) -> ValidationResult:
        ...

    def dependencies(self) -> list[Reference]:
        ...

    def execute(self, context) -> ComputationResult:
        ...
```

But different computation types implement execution differently.

## Expression computation

Your current Excel-like equations become:

```text
ExpressionComputation

Inputs:
    a
    b

Outputs:
    c

Expression:
    c = a + b
```

Internally:

```python
class ExpressionComputation(Computation):
    expression: ExpressionAST
```

---

## Function computation

For custom Python logic:

```text
FunctionComputation

Backend:
    monoss.models.turbine.calculate_power

Inputs:
    ...

Outputs:
    ...
```

---

## External model computation

For CoolProp:

```text
ExternalModelComputation

Provider:
    coolprop

Operation:
    PropsSI

Inputs:
    temperature
    pressure
    fluid

Outputs:
    enthalpy
```

---

## Solver computation

Later:

```text
SolverComputation

Equations:
    [...]

Unknowns:
    [...]

Solver:
    scipy.root
```

---

## Simulation computation

Eventually:

```text
SimulationComputation

Model:
    thermal_network

Initial State:
    [...]

Parameters:
    [...]

Outputs:
    time_series
```

The important thing is that **all of these look identical to the dependency system**.

They consume references.

They produce references.

The implementation is merely a backend.

---

# 4. Replace implicit references with explicit bindings

This is where your current formula system needs the biggest upgrade.

Right now:

```text
=pressure * area
```

probably resolves names dynamically.

Instead, when the computation is created, MonOSS resolves every symbol.

The user may write:

```text
=mass_flow * (inlet_enthalpy - outlet_enthalpy)
```

But internally:

```text
ExpressionAST

Multiply
├── Reference(input: mass_flow)
└── Subtract
    ├── Reference(input: inlet_enthalpy)
    └── Reference(input: outlet_enthalpy)
```

Then the computation explicitly defines:

```text
InputBinding:
    alias: mass_flow
    source: PropertyReference(...)
```

For example:

```python
InputBinding(
    alias="mass_flow",
    reference=PropertyReference(
        participant="turbine",
        property="mass_flow"
    )
)
```

This is important because formulas remain pleasant:

```text
mass_flow * delta_h
```

while MonOSS actually knows what `mass_flow` means.

No mysterious strings. No namespace roulette.

---

# 5. Introduce `Reference`

Everything in the computational system should be addressable through a common reference object.

Something conceptually like:

```python
class Reference:
    kind: Literal[
        "property",
        "parameter",
        "port",
        "computation_output"
    ]

    target_id: UUID
```

For example:

```text
PropertyReference

mote:
    main_turbine

property:
    mass_flow
```

Or, in a reusable model:

```text
ParticipantPropertyReference

participant:
    turbine

property:
    mass_flow
```

This distinction matters.

A reusable model cannot directly reference:

```text
main_turbine
```

because that specific turbine might not exist somewhere else.

Instead:

```text
Model Definition

Participant:
    turbine

Reference:
    turbine.mass_flow
```

Then an instance binds:

```text
turbine
    ↓
main_turbine
```

Now the exact same model can be reused.

---

# 6. Models are templates for computational systems

I would define a model as:

```python
class Model:
    id: UUID
    name: str
    description: str

    participants: list[Participant]
    computations: list[Computation]
    constraints: list[Constraint]
    connections: list[Connection]
```

A model does **not** necessarily own motes.

Instead:

```text
RankineCycleModel

Participants

    boiler:
        requires species Boiler

    turbine:
        requires species Turbine

    condenser:
        requires species Condenser

    pump:
        requires species Pump
```

The model describes the roles.

Then:

```text
RankineCycleInstance

boiler:
    boiler_1

turbine:
    main_turbine

condenser:
    condenser_1

pump:
    feedwater_pump
```

This separation is extremely useful.

You can have:

```text
Rankine Cycle Model
        │
        ├── Nuclear Plant A
        ├── Nuclear Plant B
        └── Classroom Example
```

Same model.

Different systems.

---

# 7. Model instances compile into a computational graph

This is the part I would make the centerpiece of the implementation.

When a model is instantiated:

```text
Model Definition
+
Participant Bindings
+
Actual Motes
```

MonOSS compiles it into a graph.

For example:

```text
                 main_turbine.mass_flow
                           │
                           │
                           ▼
                    ┌─────────────┐
                    │             │
                    │ TURBINE     │
                    │ POWER       │
                    │             │
                    └──────┬──────┘
                           │
                           ▼
                   main_turbine.power
```

With external properties:

```text
main_turbine.temperature ─┐
                          │
main_turbine.pressure ────┼──► CoolProp ──► enthalpy
                          │
fluid ────────────────────┘
```

Internally I would represent this as a directed graph:

```text
PropertyNode
      │
      ▼
ComputationNode
      │
      ▼
PropertyNode
```

A property can feed multiple computations.

A computation can consume multiple properties.

A computation can produce multiple properties.

That gives you a genuine dependency graph.

---

# 8. Properties need a stronger data model

I would not store only:

```python
property.name
property.value
```

Instead:

```python
class PropertyValue:
    property_id: UUID

    value: Any
    unit: Unit | None

    status: PropertyStatus

    source: ValueSource | None

    updated_at: datetime
    revision: int
```

Status could initially be:

```text
manual
calculated
observed
unknown
invalid
stale
```

For example:

```text
main_turbine.power

Value:
    12.4 MW

Status:
    calculated

Produced by:
    computation:turbine_power
```

The distinction between the **property definition** and its **current value state** is useful.

```text
PropertyDefinition

Name:
    power

Expected Type:
    quantity

Dimension:
    power
```

versus:

```text
PropertyValue

Value:
    12.4 MW

Status:
    calculated
```

Eventually you may support time-dependent values, uncertainties, and distributions without redesigning everything.

---

# 9. Provenance is not optional

I would build provenance into the architecture from the beginning.

Every time a computation executes:

```text
ComputationRun
```

is created.

Conceptually:

```python
class ComputationRun:
    id: UUID

    computation_id: UUID
    model_instance_id: UUID

    started_at: datetime
    completed_at: datetime

    input_snapshot: dict
    output_snapshot: dict

    status: RunStatus

    implementation_version: str
```

Example:

```text
Computation Run #821

Computation:
    turbine_power

Inputs:

    mass_flow:
        10 kg/s

    inlet_enthalpy:
        3200 kJ/kg

    outlet_enthalpy:
        1900 kJ/kg

Output:

    power:
        13 MW
```

Then the property stores:

```text
source_run_id:
    821
```

This means `why` doesn't need to reconstruct history.

It follows pointers.

---

# 10. `why` becomes a provenance traversal engine

The command:

```text
why main_turbine.power
```

works like this:

## Step 1

Resolve:

```text
main_turbine.power
```

Find:

```text
source_run_id = 821
```

## Step 2

Load run:

```text
turbine_power
```

## Step 3

Display the inputs.

## Step 4

For each input, recursively inspect its source.

This produces:

```text
main_turbine.power
= 13 MW
│
└── produced by turbine_power
    │
    ├── mass_flow
    │   = 10 kg/s
    │   └── manually specified
    │
    ├── inlet_enthalpy
    │   = 3200 kJ/kg
    │   └── produced by fluid_state
    │       │
    │       ├── temperature = 800 K
    │       ├── pressure = 10 MPa
    │       └── backend = CoolProp
    │
    └── outlet_enthalpy
        = 1900 kJ/kg
        └── ...
```

That is enormously more powerful than:

```text
power = mass_flow * delta_h
```

because it explains the **actual value**, not merely the formula.

You should support modes:

```text
why turbine.power
why turbine.power --deep
why turbine.power --inputs
why turbine.power --formula
```

Eventually:

```text
why turbine.power --run 821
```

for historical debugging.

---

# 11. `inspect` becomes a neighborhood traversal

`inspect` answers a different question.

```text
inspect main_turbine
```

Should show the object's place in the system.

Something like:

```text
MAIN TURBINE
════════════════════════════════

Species
    Turbine

Model Roles
    RankineCycle.turbine

Properties

    mass_flow
        10 kg/s
        manual

    inlet_enthalpy
        3200 kJ/kg
        calculated

    outlet_enthalpy
        1900 kJ/kg
        calculated

    power
        13 MW
        calculated

Connections

    inlet
        ← boiler_1.outlet

    outlet
        → condenser_1.inlet

Computations

Produces:
    inlet_state
    outlet_state
    turbine_power

Consumes:
    boiler_state
    condenser_pressure

Constraints

    efficiency ∈ [0, 1]
```

Internally, `inspect` traverses outward from the mote:

```text
Mote
 │
 ├── Species
 ├── Properties
 ├── Relationships
 ├── Model Bindings
 ├── Producer Computations
 ├── Consumer Computations
 └── Constraints
```

This is fundamentally a graph query.

---

# 12. Add `impact`

This architecture naturally gives you the inverse of `why`.

```text
impact main_turbine.inlet_pressure
```

The engine walks forward through the dependency graph:

```text
inlet_pressure
     │
     ▼
fluid_state
     │
     ▼
inlet_enthalpy
     │
     ▼
turbine_power
     │
     ▼
power
     │
     ▼
generator_output
```

Output:

```text
Changing main_turbine.inlet_pressure may affect:

    inlet_enthalpy
        produced by inlet_state

    turbine.power
        produced by turbine_power

    generator.output
        produced by generator_power

    plant.efficiency
        produced by plant_efficiency
```

Now you have the three fundamental graph navigation operations:

```text
inspect
    Explore sideways

why
    Explore backward

impact
    Explore forward
```

I think these should become core MonOSS concepts, not just CLI commands.

---

# 13. Rules should be demoted from "the modeling system"

This is an important architectural correction.

Your current system has rules doing too much.

Right now, rules are apparently responsible for:

* applying to species
* finding related motes
* evaluating equations
* setting properties

That's a lot of semantic responsibility packed into one abstraction.

I would split it.

## Rules

Rules should represent declarative logic.

Examples:

```text
IF efficiency > 1
THEN invalid
```

```text
IF temperature > maximum_temperature
THEN status = overheated
```

```text
All Turbine motes require:
    efficiency
    inlet_pressure
```

Rules can:

* validate
* classify
* trigger
* enforce defaults
* perform reactive behavior

But they should not be the primary representation of physics or system behavior.

That becomes the model/computation system.

---

# 14. Introduce constraints separately from rules

A constraint is something that must be satisfied.

For example:

```text
efficiency >= 0
efficiency <= 1
```

or:

```text
mass_in = mass_out
```

Conceptually:

```python
class Constraint:
    id: UUID
    expression: Expression
    severity: Severity
```

Constraints can initially be simple validation.

Later, they become solver inputs.

For example:

```text
Constraint:
    mass_in = mass_out
```

Initially:

```text
Check whether true.
```

Eventually:

```text
Use it to solve for an unknown.
```

Same data structure. Increasing capability later.

This is exactly the kind of architectural seed you want.

---

# 15. Ports and connections should come later, but the design should anticipate them

I would not build a giant port system immediately.

That would be premature unless you're already constructing multi-component physical networks.

But your relationship system should gain the ability to carry semantics.

Right now:

```text
A → connected_to → B
```

Eventually you want:

```text
Connection

Source:
    turbine.outlet

Target:
    condenser.inlet

Type:
    fluid

Properties:
    flow_direction
    resistance
```

This can support:

* hydraulic networks
* electrical circuits
* heat transfer networks
* project dependencies
* logistics

However, I would make ports a **modeling extension**, not force every mote relationship to become a port.

A contact relationship doesn't need an inlet and outlet. Humanity already has enough abstractions pretending everything is a pipe.

---

# 16. The execution engine

The execution engine should compile the model graph into an execution plan.

Conceptually:

```text
Model Instance
      │
      ▼
Graph Compiler
      │
      ▼
Dependency Graph
      │
      ▼
Validation
      │
      ▼
Execution Plan
      │
      ▼
Computation Runner
```

## Graph compiler

Build:

```text
Property → Computation → Property
```

edges.

## Validation

Check:

* missing references
* incompatible species
* duplicate producers
* circular dependencies
* invalid units
* missing required inputs

## Execution plan

Perform a topological sort.

Example:

```text
1. Calculate inlet_state
2. Calculate inlet_enthalpy
3. Calculate outlet_state
4. Calculate outlet_enthalpy
5. Calculate turbine_power
6. Calculate generator_output
```

Obviously, real models eventually contain cycles.

For the first version:

```text
Cycles are errors.
```

Later:

```text
Cycles become solver groups.
```

That is an extremely sane incremental path.

---

# 17. Multiple producers must be handled explicitly

This is a problem you should solve early.

What if:

```text
temperature
```

can come from:

```text
sensor_reading
```

and:

```text
thermal_model
```

and:

```text
manual input
```

You cannot just allow three computations to silently fight over the same property.

I recommend that a `PropertyValue` has a **source mode**:

```text
manual
observed
computed
```

And computations produce named **candidates**.

For example:

```text
temperature

Available sources:

    manual_override
    sensor_001
    thermal_model
```

Then an active source is selected.

Conceptually:

```text
ValueSelection

property:
    temperature

strategy:
    observed_preferred

fallback:
    computed
```

For the initial implementation, keep this brutally simple:

> A property can have one active producer.

If another computation tries to own it:

```text
Error:
temperature already has an active producer.
```

Later add source selection.

Do not prematurely invent a data-fusion framework because two numbers looked at each other funny.

---

# 18. The actual database structure

I would add tables roughly like this.

## Existing

```sql
motes
species
properties
relationships
rules
```

## New

### `models`

```sql
models
------
id
name
description
created_at
```

### `model_participants`

```sql
model_participants
------------------
id
model_id
name
required_species_id
```

Example:

```text
RankineCycle | turbine | Turbine
```

---

### `model_instances`

```sql
model_instances
---------------
id
model_id
garden_id
name
```

---

### `participant_bindings`

```sql
participant_bindings
--------------------
model_instance_id
participant_id
mote_id
```

---

### `computations`

```sql
computations
------------
id
model_id
name
kind
implementation
metadata
```

The `implementation` might initially contain:

```text
mass_flow * (h_in - h_out)
```

For an external model:

```json
{
    "provider": "coolprop",
    "operation": "enthalpy"
}
```

---

### `computation_inputs`

```sql
computation_inputs
------------------
id
computation_id
alias
reference_type
reference_data
required
```

---

### `computation_outputs`

```sql
computation_outputs
-------------------
id
computation_id
alias
reference_type
reference_data
```

---

### `computation_runs`

```sql
computation_runs
----------------
id
computation_id
model_instance_id
status
started_at
completed_at
```

---

### `computation_run_inputs`

```sql
computation_run_inputs
----------------------
run_id
binding_id
value_snapshot
```

---

### `computation_run_outputs`

```sql
computation_run_outputs
-----------------------
run_id
binding_id
value_snapshot
```

This might feel like more tables than your current architecture.

It is.

Because you are now storing information that previously only existed implicitly inside formula strings and procedural execution.

That's the entire point.

---

# 19. The expression system should survive

I would **not remove your Excel-like language**.

It becomes the expression implementation for `ExpressionComputation`.

For example:

```text
mass_flow * (h_in - h_out)
```

is still perfectly valid.

The difference is that it now exists here:

```text
Computation
    kind = expression

    inputs:
        mass_flow
        h_in
        h_out

    outputs:
        power

    expression:
        mass_flow * (h_in - h_out)
```

The spreadsheet-like syntax is now the **surface language**, not the architecture.

That distinction is everything.

---

# 20. The model creation workflow

This is how I think users should actually create a model.

## Step 1: Define species

```text
species create Turbine
```

Properties:

```text
mass_flow
inlet_pressure
inlet_temperature
outlet_pressure
efficiency
power
```

---

## Step 2: Create model

```text
model create SimpleTurbine
```

---

## Step 3: Add participants

```text
model participant add turbine --species Turbine
```

---

## Step 4: Add computations

```text
model computation add inlet_enthalpy
```

Then:

```text
inputs:
    temperature = turbine.inlet_temperature
    pressure = turbine.inlet_pressure

outputs:
    enthalpy = turbine.inlet_enthalpy

backend:
    coolprop
```

Then:

```text
model computation add turbine_power
```

```text
inputs:
    mass_flow = turbine.mass_flow
    h_in = turbine.inlet_enthalpy
    h_out = turbine.outlet_enthalpy

outputs:
    power = turbine.power

expression:
    mass_flow * (h_in - h_out)
```

The user never has to manually create graph edges.

They emerge from the declared bindings.

---

# 21. Instantiation

Create actual motes:

```text
create turbine main_turbine
```

Then:

```text
model instantiate SimpleTurbine plant_model
```

Bind:

```text
turbine → main_turbine
```

Now MonOSS compiles:

```text
SimpleTurbine
+
main_turbine
=
ExecutableModelInstance
```

That instance owns:

```text
participant bindings
compiled graph
execution state
run history
```

---

# 22. What happens when a property changes?

Suppose:

```text
set main_turbine.inlet_temperature 900 K
```

MonOSS should:

## 1. Update the property

```text
revision += 1
```

## 2. Find downstream computations

```text
inlet_enthalpy
```

depends on it.

## 3. Mark outputs stale

```text
inlet_enthalpy → stale
power → stale
generator_output → stale
```

## 4. Either:

### Lazy evaluation

Don't calculate until needed.

```text
inspect power
```

triggers recalculation.

Or:

### Eager evaluation

Immediately recompute downstream nodes.

I recommend starting with **lazy evaluation**.

It's simpler and more efficient for experimentation.

So:

```text
set inlet_temperature
```

results in:

```text
Updated.

Marked stale:

    inlet_enthalpy
    outlet_enthalpy
    power
```

Then:

```text
get power
```

causes:

```text
Executing:

    inlet_state
    outlet_state
    turbine_power
```

---

# 23. The execution context

Every computation should receive an execution context.

Something like:

```python
class ExecutionContext:
    model_instance
    garden

    value_resolver
    unit_registry
    model_registry

    run_manager
```

The computation itself shouldn't know how SQLite works.

It asks:

```python
context.get(input_reference)
```

and returns:

```python
ComputationResult(
    outputs={
        "power": Quantity(...)
    }
)
```

The execution engine handles persistence.

This is important for keeping computation plugins clean.

---

# 24. The plugin architecture

External computational backends should implement an adapter.

Something like:

```python
class ComputationBackend(Protocol):

    kind: str

    def validate(self, computation):
        ...

    def execute(
        self,
        computation,
        inputs,
        context
    ):
        ...
```

Then:

```text
expression
```

uses:

```text
ExpressionBackend
```

CoolProp uses:

```text
CoolPropBackend
```

Later:

```text
PythonBackend
SciPyBackend
OpenMCBackend
CircuitBackend
```

MonOSS itself only knows:

```text
Computation
    │
    ├── declared inputs
    ├── declared outputs
    └── backend
```

This fits beautifully with the compatibility-layer idea you were already considering for property models.

---

# 25. The key query architecture

This design enables a whole family of useful commands.

## `inspect`

Explore the local neighborhood.

```text
inspect main_turbine
```

---

## `why`

Trace backward.

```text
why main_turbine.power
```

---

## `impact`

Trace forward.

```text
impact main_turbine.inlet_temperature
```

---

## `requires`

Find prerequisites.

```text
requires main_turbine.power
```

```text
To calculate power:

    mass_flow
    inlet_enthalpy
    outlet_enthalpy

Currently missing:

    inlet_temperature
```

---

## `producer`

```text
producer main_turbine.power
```

```text
turbine_power
```

---

## `consumers`

```text
consumers main_turbine.mass_flow
```

```text
turbine_power
velocity
reynolds_number
```

---

## `graph`

Eventually:

```text
graph main_turbine.power --depth 3
```

The Observatory can render the same underlying query graphically.

That is important.

**The CLI and visualization system should query the same model graph.**

Do not build two different interpretations of the system.

---

# 26. Why this is fundamentally better

Your current architecture:

```text
Species
    │
    ▼
Rules
    │
    ▼
Equations hidden inside rules
    │
    ▼
Property values
```

has several problems.

### Dependencies are implicit

You have to parse formulas to discover relationships.

### Execution is mixed with modeling

A rule both describes and performs behavior.

### Provenance is difficult

Once a value changes, you may not know how it was derived.

### External models don't fit naturally

CoolProp is not really an equation rule.

### Simulation becomes awkward

You eventually need execution graphs, solver groups, and state.

### Inspection is reconstruction

The system has to infer what the model is after the fact.

---

The proposed architecture:

```text
Motes ───────┐
             │
Relationships│
             ▼
        Model Instance
             │
             ▼
      Computational Graph
             │
      ┌──────┴──────┐
      ▼             ▼
 Computation     Constraint
      │
      ▼
 Property Values
      │
      ▼
 Provenance
```

makes those relationships explicit from the beginning.

The system doesn't need to guess.

---

# 27. The migration strategy I would actually use

Do this incrementally.

## Phase 1: Build `Computation`

Keep your existing rules and equations.

Create:

```python
Computation
InputBinding
OutputBinding
```

---

## Phase 2: Convert equations

Every existing equation rule becomes internally:

```text
ExpressionComputation
```

The user-facing behavior doesn't have to change.

---

## Phase 3: Build the dependency index

Persist:

```text
Property → Computation
Computation → Property
```

Now you can implement:

```text
why
impact
producer
consumers
```

Even before models exist.

This is probably your highest-value next step.

---

## Phase 4: Add execution state

Introduce:

```text
manual
calculated
stale
unknown
invalid
```

Implement lazy recalculation.

---

## Phase 5: Add computation runs

Store input and output snapshots.

Now `why` can explain historical values.

---

## Phase 6: Introduce Models

A model initially is just:

```text
participants
+
computations
+
constraints
```

Don't add ports yet unless a use case demands them.

---

## Phase 7: Add external computation backends

CoolProp first.

Then other property packages.

---

## Phase 8: Add constraints and solver groups

Start with validation.

Later:

```text
cyclic dependency
```

becomes:

```text
equation system
```

which can be handed to a solver.

---

# The final conceptual structure

This is what I think MonOSS should become:

```text
Garden
│
├── Ontology
│   │
│   ├── Species
│   ├── Property Definitions
│   └── Relationship Definitions
│
├── System
│   │
│   ├── Motes
│   ├── Property Values
│   └── Relationships
│
├── Models
│   │
│   ├── Participants
│   ├── Computations
│   │   │
│   │   ├── Expression
│   │   ├── Function
│   │   ├── External Model
│   │   ├── Solver
│   │   └── Simulation
│   │
│   ├── Constraints
│   └── Connections
│
├── Model Instances
│   │
│   ├── Participant Bindings
│   ├── Compiled Dependency Graph
│   └── Execution State
│
└── Execution History
    │
    ├── Computation Runs
    ├── Input Snapshots
    ├── Output Snapshots
    └── Provenance
```

My strongest opinion here is that you should **not try to jump directly from Excel formulas to a grand symbolic mathematics system**. That's how you end up spending a month building an equation parser while the actual modeling architecture remains vague.

Build the graph first.

Specifically:

> **Explicit inputs → Computation → Explicit outputs**

Then make those references real, persist the graph, and build `why`, `impact`, staleness, and provenance on top of it.

Once that exists, a `Model` becomes almost embarrassingly natural: it is a structured collection of motes playing roles and computations connecting their properties.

And that's the architectural transition you're looking for:

```text
Excel:
    cells contain formulas

MonOSS now:
    properties contain formulas

Proposed MonOSS:
    things have state
    computations transform state
    models organize computations
    graphs explain dependencies
    execution records explain history
```

That is a much more serious foundation. It also happens to leave the door open for turbines, circuits, reactor models, optimization, experiments, digital twins, and whatever else you decide to throw into the machine while civilization continues insisting that spreadsheets are a programming language.

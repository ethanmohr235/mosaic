# Roadmap

This is the decided direction, taken from the project-oriented brainstorm
of 2026-09-29. Each phase still goes through plan mode and approval
(`rules.md`). Anything listed under *Open questions* is undecided, so ask
before assuming.

## Why this direction

Real usage had already split the gardens in two.

- **`college-fall-2026`** (89 motes) holds homework, classes, exams,
  events, and time. It has no equations.
- **`FEA-Homework-4`** (10 motes) is all math, but mostly plumbing: 10
  motes and 10 relationships to express about 9 numbers and 5 equations.
  Six `related_to` edges exist only so `bd.l` resolves, and rule names
  copy their equations and drift out of date.

Portals, folders, and the library were removed because nobody used them.
Each one was a separate act of filing. That lesson became a rule in
`philosophy.md`.

## Decided

### Structure
- A mote may own a garden through a `garden: <name>` property, and a
  garden has at most one owner. The owning mote is the cover. Nesting is
  recursion. Gardens stay flat on disk. See `overview.md` → *Structure*.
- *Start project* on a mote creates and links the garden.
- *Submit* is the only write from a project back to its owner, and it's
  explicit (sets `status` and adds a link to the frozen output).
- Home replaces the Gardens window's list. It shows the ownership tree
  plus loose gardens, and opens them. Health indicators (solve and
  document) come later.
- **Decided in building it:** `garden` is a reserved property. The
  one-owner check reports two claims, a claim on a missing garden and an
  ownership loop, and never refuses to open anything. A name *Start
  project* derives from the mote never adopts an existing garden, while a
  name given explicitly adopts a loose one. Undo of *Start project* clears
  the property and leaves the garden on disk. See `architecture.md` ->
  *Projects*.
- **Cover fields (decided 2026-09-30):** `name`, plus `due`, `status`,
  `author` and `revision` as ordinary, unreserved properties. *Submit*
  (phase 6) writes `revision`; nobody types it.
- **Archive (decided 2026-09-30):** a property on the owner, `archived:
  true`, archives its whole subtree. A loose garden is archived by a
  marker mote inside it carrying the same property.

### Calculation
- **Garden-scope names.** A formula reference `bd.l` falls back, after
  roles and connected names, to the mote named `bd` when that name is
  unique in the garden. If the name isn't unique, the solve fails with a
  hard error that names every colliding mote. This deletes
  `related_to`-for-resolution edges.
- **Worksheet editor.** Top-to-bottom lines like `bd.l =
  sqrt(bd.x^2 + bd.y^2)` or `bd.x = 80 in` write ordinary motes, rules,
  `applies_to`, and freedoms. It's one editor among several, not the
  only way to model. Rules it creates are unnamed by default, with the
  equation as their label. A trailing comment (`! ΣFy`) names the rule.
  Species and templates remain for reusable models. **Decided:** it is a
  pure projection of the graph — no stored source text, so comments and
  line order don't survive a reopen — and applying it replaces, so a rule
  whose line is gone is deleted. `= ?` declares an unknown, and `==`
  declares no freedom. See `simulation.md` → *The worksheet*.
- **Solve health.** Show an equation/unknown count, and name the culprits
  when a system is over- or under-determined (`core/incidence.py`). Check
  units per line before solving. Show display units separately from
  stored units. **Decided:** the verdict is read off *coupled blocks*
  rather than off the totals, since three equations and three unknowns can
  be two blocks with one short; and a display unit is a surface's own view
  state, never written into the garden, so `core/` offers only a
  conversion. See `simulation.md` → *Solve health*.
- **Checks.** A `check` relation is evaluated but never used to solve,
  and shows as a green/red checklist. This builds on the existing
  comparison constraints. **Decided:** a rule carrying reserved `check:
  true` is a check whatever its operator; an equality check's tolerance is
  relative, 0.1% by default, overridable per rule with `tolerance`; the
  worksheet writes one as `?=`. See `simulation.md` -> *Equality checks*.
- **External values / V&V.** A given can be marked `source: external`
  with a note (e.g. an FEA probe). Mora gets a hand vs. external vs. %
  error table. Measured data can be imported from CSV. **Decided:** one
  mote per row, the header naming properties with an optional unit in
  brackets (`load [kN]`), each value marked `source: measured` with the
  file as its note, and every row linked to one import mote recording the
  file. An external value names the hand value it verifies with `checks`
  in its own `source` entry, and the % error is relative to the external
  value. See `modoc.md` -> *`\verification`*.
- **Uncertainty.** A given can carry `±`. First-order propagation uses
  the Newton Jacobian, and every result shows its ±. **Decided:** the ±
  is the reserved `uncertainty` map (`{x: 0.5}`, `{x: "2%"}`); a result's
  ± is derived on read and never stored; givens combine root-sum-square.
  The derivative is taken as a central difference of the solve, which to
  first order *is* the Jacobian's and works for every backend. See
  `simulation.md` -> *Uncertainty*.
- **Materials provider.** A built-in provider supplies E, ν, Sy, and ρ
  for common alloys, shaped like the CoolProp provider. **Decided:** a
  PropertyProvider over a bundled, cited data file
  (`core/data/materials.json`), nominal values only; a configuration's
  `values` adds or overrides. See `simulation.md` -> *Physical property
  models*.
- **Time simulation** keeps ticks, but runs become curves to plot. Cut
  *commit tick N* and saved run history.

### Documents (Mora)
- **Calc macros:** `\work` (symbolic equation → substituted values with
  units → result), `\given`, `\answers`, `\assumptions`, and `\checks`.
  `\val` gets unit conversion and significant figures. **Decided:**
  substituted values are in each mote's declared unit, converted to one
  where a line mixes two of a dimension; the four sections take no
  argument and cover the whole garden; a format goes in a second brace
  (`\val{bd.l}{in, 4}`); and a value's provenance is the reserved
  `source` property (`assumed`, `external`), which is also what phase 7's
  V&V table reads. See `modoc.md`.
- **LaTeX basics:** numbered equations with `\label`/`\ref`, citable
  rules, figures with captions, `tabular`, `enumerate`, and `\include`
  for multi-note documents. **Decided:** `\caption`/`\label` go inside
  the environment they describe; a bare `\graph` stays unnumbered and
  only a `figure` environment numbers one; a table's first row is its
  header; a cited rule's own name is its label; `\include` splices and
  the whole assembled document is numbered as one. See `modoc.md`.
- **Output:** export `.tex` with every value frozen and graphs written as
  figure files. **Bundle tectonic** so real PDFs work everywhere. This
  was approved in principle; confirm packaging in its phase.
- **Submit / revisions:** freeze the PDF and `.tex`, record the revision
  and date on the cover (owning mote), and diff values between
  revisions.

### Surfaces
- **One tiled window (the console).** Apps become panes in one
  keyboard-first, all-monospace window. It gets built in parallel with
  the other phases as steps U1–U9. The design, architecture, view
  contract and step order are in `console.md`. Once U1 lands, new UI is
  built only there.
- **Momentum:** remove it. Keep `status` and `estimate` as plain
  properties.
- **Monastery:** becomes the agenda that launches work: project bars
  (start → due), plan-my-week (auto-placed work blocks as ordinary dated
  motes), click-to-open (a lecture opens its note, a work block opens its
  project), and planned vs. actual time.
- **Monarch:** freeze it as a structure-debugger tile. No new
  investment. **Done:** the console's `graph` pane (U8), annotations
  removed.
- **Monolith:** keep the general table, and add a project variables pane
  (value, unit, given/solved/external, ±).
- **Moxie:** add publication-quality export (SVG/PDF, unit-labelled
  axes, log axes), CSV data overlaid on theory curves, and error bars.

### Across gardens
- **Cross-garden search:** read-only across every `.db`, and results open
  the project.
- **Archive:** mark a finished tree read-only and out of the way. It stays
  flat on disk and remains searchable. **Decided:** read-only is refused
  at the CLI and MCP write paths and flagged for the UI, not enforced in
  the store. See `architecture.md` -> *Archive*.

## Phases

The order is cleanup first, then smallest shippable. The tiled window (`console.md`, steps U1–U9) runs
alongside all of them.

| # | Phase | Proves |
|---|---|---|
| 1 | **Done.** Cleanup: remove Momentum; freeze Monarch; cut commit-tick and run history | Fewer surfaces, less code |
| 2 | **Backend done.** Garden-scope names, worksheet editor, solve health | Rebuild `FEA-Homework-4` in about 8 lines |
| 3 | **Backend done.** Mora calc macros and LaTeX basics | A gradeable homework document |
| 4 | **Backend done.** Garden ownership, *Start project*, one-owner check, Home tree | The owner ↔ project loop without filing |
| 5 | **Bars and click-to-open done (console U7).** Monastery agenda: plan-my-week, planned vs. actual | Time view that routes into projects |
| 6 | tectonic, `.tex` export, Submit and revisions | Real PDFs and frozen submissions |
| 7 | **Backend done except Moxie export.** Checks, external values / V&V, CSV import, Moxie publication export | The FEA V&V workflow |
| 8 | **Backend done.** Uncertainty propagation | Lab reports |
| 9 | **Backend done.** Cross-garden search, archive, materials provider | Payoff of many small gardens |

## Acceptance workflows

Test phases against these end-to-end.

1. **Weekly homework.** Canvas sync → homework mote → *Start project* →
   model → checks green → Mora with `\work` → Submit → PDF, and the owner
   shows `status: submitted`. No hand-typed numbers.
2. **FEA V&V write-up.** Hand calc → FEA results as external values →
   mesh-convergence CSV → Moxie plot → V&V table → report.
3. **Cumulative report.** One project garden with a note per section,
   assembled with `\include`.
4. **Lab report.** Givens with ± → propagated ± → error bars → `\val`
   prints ±.
5. **Thermo cycle.** `steam-turbine-cycle` with CoolProp → state table,
   T-s plot, efficiency in `\answers`.

## Not planned

These were considered and left out for now. Don't build them without
asking.

- `\cite` with bibliography motes, and `\formulas{course=…}` formula
  sheets.
- *New project from a past project* and *Import model from…*.
- Scenarios presented as homework parts or design options (scenarios
  still work as they do today).
- FBD sketch surface (Monarch's ink tools it would have built on were
  removed in U8).
- Automations (still deferred).
- Live links between gardens (`not-goals.md`).

## Open questions

- **Renaming to Mosaic** in code, packages, and MCP: when, and how much.

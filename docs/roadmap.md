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
- Home replaces the Gardens window's list (**done**, console U9: `mo
  saic`, with `project start|stop` and the breadcrumb). It shows the ownership tree
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
- **Images:** `\image{name}` shows a stored PNG or SVG (a mote with the
  reserved `image` property), bare or in a captioned `figure`.
  **Decided:** base64 in the mote, 4 MB cap, behind `core/images.py` so
  side files can replace it; auto-named `shot-NNN`, renamed later with
  `monoss image rename`. Backend done; paste-a-screenshot is console
  work. `.tex` export writes the file and emits `\includegraphics`
  (SVG needs converting for LaTeX).
- **Output (done 2026-10-05):** `.tex` with every value frozen; graphs
  written as pgfplots from the solved data; units as `\mathrm`;
  `article` with a title block from the cover. **Decided:** tectonic is
  downloaded on first use (a pinned release, SHA-256 checked) into a
  per-user cache, a `tectonic` on PATH wins, and `.tex` export never
  needs it. PNG images export; an SVG is a reported problem until a
  converter is approved.
- **Submit / revisions (done 2026-10-05):** a `submission` mote per
  revision holds the `.tex`, PDF, figures and the quoted values;
  the owner gets `status: submitted`, `revision` and `submitted`.
  **Decided:** revisions count `r1`, `r2`...; the document is the named
  note or the garden's one root note; a failed check or unresolved value
  refuses unless `--force` (recorded as `forced`). `revisions diff` lists
  changed values. See `modoc.md` -> *Output*.

### Surfaces
- **One tiled window (the console).** Every surface is panes in one
  keyboard-first, all-monospace window, built as steps U1–U9 (all done).
  There are no apps: views are named for their verbs, and built-in
  layouts group them by task (`console.md` → *Polish and unification*).
- **Momentum:** removed. `status` and `estimate` stay plain properties.
- **Agenda** (`agenda`, `week`, `month`): becomes the agenda that
  launches work: project bars (start → due), plan-my-week (auto-placed
  work blocks as ordinary dated motes), click-to-open (a lecture opens
  its note, a work block opens its project), and planned vs. actual time.
  **Done 2026-10-05, decided:** `plan` places open motes (`due` +
  `estimate`) earliest deadline first into free work hours (flags, default
  9-17, 2 h blocks, 7 days) and only previews until `--apply`; a block is a
  `Work block` mote with `when`, `kind: planned` and a `for` link; `worked`
  logs `kind: actual` blocks; `budget` sums estimate / done / planned. See
  `console.md` -> *Plan my week*.
- **Graph:** frozen as a structure debugger. No new investment.
  **Done:** the `graph` pane (U8), annotations removed.
- **Table:** keep the general table, and add a project variables pane
  (value, unit, given/solved/external, ±).
- **Plots (done 2026-10-05):** publication export (`plot export
  svg|pdf|tex`: unit-labelled axes, log axes), measured points overlaid
  with `--data` (picked as `fit` picks them), error bars. **Decided:** SVG
  is drawn by core, PDF is the pgfplots figure compiled by tectonic; the
  theory curve carries the propagated ± as a shaded band, the data points
  their own `uncertainty` as bars; the overlay is a flag on the line, not
  part of a saved plot. Sweeps only.

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
| 4 | **Done** (UI: console U9). Garden ownership, *Start project*, one-owner check, Home tree | The owner ↔ project loop without filing |
| 5 | **Done** (2026-10-05). Monastery agenda: bars, click-to-open, plan-my-week, planned vs. actual | Time view that routes into projects |
| 6 | **Done** (2026-10-05). tectonic, `.tex` export, Submit and revisions | Real PDFs and frozen submissions |
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
  Since 2026-10-02 the UI shows no app names, so this would also cover
  the app-named modules and API namespaces (`console/motion.py`,
  `api.moxie`, `core/motion.py`, …).

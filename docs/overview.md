# Mosaic Project Overview

Mosaic was called MonOSS during early development. Code, packages, and
the MCP server still use the old names (`monoss`, `Monarch`, `Mora`, …).
Don't mass-rename anything until a phase approves it.

## Purpose

Mosaic is where an engineer's projects live. A project is a model (givens,
equations, solved values, units), the documents that show that work, the
plots that illustrate it, and the deadlines and time it runs on. The first
user is an engineering student: homework sets, lab reports, FEA
verification write-ups. The same shape later becomes a working engineer's
calc package, with givens, assumptions, method, results, checks, and
revisions.

Most of the work happens in the calculation. That's where most of the
investment goes, but the core treats a deadline and an equation as the
same kind of thing.

What sets Mosaic apart is **model + live document + plots over one graph,
local, with units**. EES solves equations but has no structure or
documents. Mathcad puts math inside a document, but the document *is* the
model. LaTeX looks good, but its numbers go stale. Excel hides formulas
and has no units.

---

## The model

Everything is a **mote**: one object type with free-form JSON properties.
Motes are connected by typed **relationships**. A **garden** is a
separate graph stored as one SQLite file in the `gardens/` folder.

Specialized behavior comes from data, not from new object models:

- A mote is a **species** when others point at it with `instance_of`. A
  species may have a **template** via `template_of`.
- A mote with an `equation` is a **rule**. Rules solve for **freedoms**
  and reach other motes through `applies_to`, roles, and names -- a name
  unique in the garden needs no relationship drawn for it.
- **Units**, **property models** (CoolProp, ideal gas), **scenarios**,
  **sweeps**, and **comparison constraints** are all properties and
  relationships the engine recognizes by reserved names (`core/reserved.py`).
- A mote with `modoc` is a **note**: a LaTeX-like document whose `\val`,
  `\mote`, and `\graph` read the graph live.

Properties describe; relationships connect. Never duplicate a
relationship as a hidden reference inside a property.

See `architecture.md` and `simulation.md` for the full reference.

---

## Structure: motes own gardens

Projects are organized by one convention: **a mote may own a garden**.

- A mote carrying `garden: <name>` owns that garden. Usually the owner is
  a deliverable, like the `HW 4` homework mote in `college-fall-2026`.
  *Start project* on a mote creates the garden and writes the property.
  Creating the project is the only act of filing.
- **The owning mote is the project's cover.** Its title, `due`,
  `status`, author, and revision are what Home lists and what Mora's
  title block prints. The child garden holds only the work.
- **Nesting is recursion.** A mote inside a project garden can own a
  garden of its own, so projects within projects need nothing new.
- **One owner per garden.** If two motes claim the same garden, that's
  flagged, which keeps the structure a tree and the cover unambiguous.
- **A "hub"** is just a garden whose motes own other gardens, such as a
  semester or a job. It holds coordination (deadlines, classes, events,
  time), and its children hold calculation. Nothing marks it as special.
- **Loose gardens** (demos, scratch work) have no owner and still work
  everywhere.
- **On disk, gardens stay flat.** The tree lives only in `garden:`
  properties, and the filename is the garden's identity. There are no
  subdirectories and no registry.
- **Across gardens, access is read-only.** Cross-garden search can read
  every garden, and importing makes a copy that records its source.
  There are no live links between gardens.

This is a convention, not a schema. The store doesn't enforce it, and
every feature built on it must still work when it's absent.

---

## Surfaces

Everything happens in **one tiled window**, the console: one workspace
of panes, each a command and its output, with a compact, minimal,
terminal-like look. There are no separate apps; panes are grouped by
task, and a built-in layout opens each group (`layout calc`).
`console.md` has the design (with the visual reference in
`docs/design/mosaic-console/`), the architecture and the view contract.

| Surface | Panes | Role |
|---|---|---|
| **Calc** | `sim`, `rule`, `scenarios`, `worksheet`; `layout calc` | The model: runs, scenarios, solve health, the worksheet editor. |
| **Documents** | `notes`, `edit`, `read`; `layout docs` | Engineering documents: shown work, given/answers, LaTeX export, submit and revisions. |
| **Plots** | `plot`, `fit` | Plots and fits, heading toward publication-quality export. |
| **Table** | `find`, `open` (props) | The general table and a mote's properties, plus a planned project variables pane (value, unit, given/solved/external, ±). |
| **Agenda** | `agenda`, `week`, `month`; `layout plan` | The agenda that launches work: project bars, plan-my-week, click-to-open, planned vs. actual time. |
| **Gardens** | `gardens`, `garden`; `layout gardens` | The garden tree (owners → owned gardens) plus loose and archived gardens, and every garden act as a command. Health indicators come later. |
| **Graph** | `graph`; `layout graph` | A structure debugger for seeing plumbing (edits are commands). |
| **CLI / MCP** | — | Scriptable and agent access to the same core. MCP is a side door, not the product. |

Code still uses the old app names for packages and API namespaces
(`console/motion.py`, `api.moxie`); see `roadmap.md` → *Open questions*.

`core/` holds all logic. Every surface is a thin adapter over it, and
an adapter that holds logic of its own is a bug.

---

## Current architecture

- Python, with SQLite per garden.
- One pywebview window with a hand-written HTML/JS frontend (no framework).
- `core/` holds the domain: motes, relationships, species, rules,
  solvers (linear, Newton), units, providers, scenarios, sweeps, modoc,
  and sync.
- Adapters: `cli/`, `mcp_server/` and `console/` (the one tiled window).
- `sync_server/` provides optional sync between machines.

The project favors readability, maintainability, and correctness over
premature optimization.

---

## Design priorities

When making implementation decisions, prioritize in this order:

1. Correctness
2. Simplicity
3. Maintainability
4. Extensibility
5. Performance

Avoid unnecessary abstractions, frameworks, and dependencies.

---

## What agents should optimize for

Aim for a coherent, long-lived system whose architecture stays
understandable as it grows, not just for code that works.

- Reuse existing abstractions. Stay consistent with the architecture
  rather than minimizing lines.
- Keep everything a mote. Surfaces may hide plumbing but must never
  replace it.
- Don't duplicate data.
- Keep APIs predictable.
- Include tests, and update the docs with any architectural change.
- Don't redesign the architecture unless explicitly asked.
- When a requirement is ambiguous, ask instead of inventing behavior.

See `philosophy.md` for the principles, `rules.md` for the working
rules, and `roadmap.md` for what's planned.

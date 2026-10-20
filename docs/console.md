# The console

This is the plan for the UI half of the Mosaic pivot. It replaces the
separate windows with one tiled, keyboard-first window, the **console**.
`roadmap.md` → *Surfaces* says what each surface is *for*. This doc says
how the one window that hosts them is built, and in what order.

Everything under *Decided* was agreed with the user on 2026-09-29. The
rules in `rules.md` still apply: every step below is a phase, so it goes
through plan mode, questions and approval before any code. Anything under
*Open questions* is undecided. Ask before you assume.

## Visual reference

`console/frontend/tokens.css` and `shell.css` are the source of truth for
type, color, spacing and density. The mockups the console was drawn
from were removed from the repo (they stay in git history, under
`docs/design/mosaic-console/`). Where this doc and an old mockup
disagree, this doc wins: the console has no apps (views and built-in
layouts only), colors motes by species, keeps selection as an ordered set
with a primary, vendors its fonts, and never saved run history.

## Decided

### Architecture

- **One process, one window.** A single pywebview window replaces the
  per-app windows and ports. Its `js_api` is a root object that holds
  one namespace per app. pywebview 6.2 exposes nested objects, so it's
  `pywebview.api.motion.step(...)`, `pywebview.api.monolith.table(...)`,
  and there's no method-name collision to solve. The per-app `api.py`
  classes become those namespaces, and they must share **one garden
  connection** owned by the root. They can't keep a connection each.
  Shared concerns (inspector, command line, undo, garden switching) sit
  on the root once and aren't mixed into every app.
- **Vanilla JS, no framework, no build step.** The hard parts here are
  imperative (canvas graph, plots, playhead, keyboard routing, divider
  dragging), and a framework adds little to them. The price is
  discipline: every view follows the *view contract* below, and shared
  pieces (table, key/value grid, popup list, pane header) live in one
  components module. Views must not grow their own copies of these.
  Revisit the no-framework choice only if views start duplicating update
  logic that the contract should have absorbed, and ask before adding
  any dependency.
- **Layout on disk.** `console/` holds the root API (`api.py`), the
  shared garden session (`session.py`), layouts and history
  (`state.py`) and the launcher (`app.py`, run by `run_console.py`).
  `console/frontend/` holds the shell, the store, the components and
  one ES module per view under `js/views/`. There is no build step, and
  since U9 there are no other windows and no `frontend_shared/`.
- **No shims, no preset launchers.** The shell was built first and the
  apps ported into it one at a time, each old window deleted when its
  port landed. The console is the only window.
- **New UI only in the shell.** Once the shell and view contract exist
  (U1), every new surface is built as a pane there, including the
  worksheet editor, Mora calc macros UI, Home tree and Monastery agenda.
  Old windows only get bug fixes. A roadmap phase that needs UI before
  U1 lands builds its `core/` part first and waits for the shell.

### Panes are commands

A pane is a **command string plus its rendered output**, and nothing
else.

- A view id is shorthand for a canonical command. For example,
  `props` means `open @sel`, and `table` means `find`.
  The pane header shows the command, or the view id in tiled mode.
  Either way, `e` loads the command into the command line.
- A **layout** is a split tree of command strings plus sizes. Saving,
  restoring, history and editing all work the same for every pane
  because there is only one kind of pane.
- A view may keep transient UI state (scroll, column widths, camera),
  but anything that should survive a restart has to be expressible in
  the command (`graph bd --depth 2`, `plot … --range 4:24`).

### Command line and grammar

- **One line, one merged grammar.** The prompt is `$`, as in a pane
  header. The first token is one of two things:
  1. a **verb** (`find`, `open`, `graph`, `plot`, `fit`, `set`,
     `layout calc`, `layout save`, …).
  2. anything else, which falls through to the existing modeling
     statements in `core/command_bar.py` (`R1.x = 5 ohm`, `A -> B`,
     `R1 : Resistor`, `R1.x ?`, `delete R1`).

  Until 2026-10-02 a third form, `mo` and an app suffix (`mo tion`),
  opened an app; built-in layouts replaced it (*Polish and
  unification*).

  Parsing lives in `core/commands.py`, never in JS, so the CLI and MCP
  can run the same commands.
- **Verbs win, quoting escapes.** Verbs (`core/reserved.py` →
  `RESERVED_VERBS`) are reserved words. A line whose
  first token is one of them is that word. A mote with a reserved name
  is still reachable as `"find"` (quoted) or `#id`, and creating one
  from the command line warns about exactly that. Each port step adds
  its own verbs to `RESERVED_VERBS`.
- **Every app is ported** (U2-U9), and the old windows are gone. Since
  2026-10-02 there are no apps either: views, and the built-in layouts
  that group them (`core/commands.py::BUILTIN_LAYOUTS`).
- **`cd <garden>`** switches the workspace's garden.
- `find` query syntax (`type:task due:<7d`) builds on `core/query.py`,
  which Monolith already uses. Don't write a second query language.
- Completion and history popups, inline ghost text, the grammar hint
  header and the per-context history are in the shell (`js/commandline.js`).
- `@sel` can be used anywhere a mote id is expected (see *Selection*).
- Errors show in `--fault` on the right of the command line, and in the
  pane header's summary when a pane's command failed.
- Hover text is the console's own tooltip (`js/tooltip.js`): mono,
  `--bg-raised`, a `--line-strong` border, square corners. Views keep
  setting plain `title` attributes (or SVG `<title>` children); the
  shell shows them, and the native tooltip never appears.

### Views and built-in layouts

There are no apps (since 2026-10-02, *Polish and unification*): a pane
is a verb's view, and a built-in layout groups a few of them by task.
Python packages keep their old app names (see *Rename*).

| Verb | View | What it shows |
|---|---|---|
| `find …` | `table` | The general table. A project variables pane per the roadmap is still to come. |
| `open @sel\|#id\|‹name›` | `props` | One mote's properties and links; `@sel` follows the selection, `#id` is pinned. |
| `plot …`, `fit …` | `plot`, `fit` | Plots re-solved at every point; a model fitted to data. |
| `scenarios`, `sim …`, `rule …`, `why …` | `scenarios`, `sim`, `rule` | Scenarios, a run as a watch list over a timeline, the rules on a mote. |
| `notes`, `edit …`, `read …`, `worksheet` | `notes`, `edit`, `read`, `worksheet` | Notes, a note's source and its rendered document, the worksheet. `edit` and `worksheet` are text-editing panes (see *Keyboard*). |
| `agenda`, `week`, `month` | `agenda`, `week`, `month` | Dated motes and project bars, on the `--line` grid. |
| `graph …` | `graph` | The structure graph, a debugger. |
| `gardens`, `garden …` | `gardens`, `garden` | The ownership tree with loose and archived gardens; one garden's details. |
| `log` | `log` | Command and error log. |

The built-in layouts (`calc`, `docs`, `plan`, `graph`, `gardens`) are in
*Polish and unification*. Worksheet, checks and V&V views are added to
this table by the roadmap phase that builds them.

### Workspace context

- **One garden per workspace.** The whole window is on one garden. The
  top-bar breadcrumb shows its ownership path (`/ college-fall-2026 /
  hw4`, per `roadmap.md` → *Structure*).
- Switching garden (from the `gardens` pane, the breadcrumb, or a command) swaps
  in that garden's layouts, history and selection.
- Cross-garden search is a view whose results open a project by
  switching the context. It never shows two gardens live at once
  (`not-goals.md`).

### Selection

- There is one global selection per workspace. It is an **ordered set**,
  and its first item is the **primary**.
- Single-mote consumers (props, graph centre, `open @sel`) use the
  primary. Batch commands (`set @sel status done`) and Monolith's
  multi-row edits use the whole set.
- The top bar shows `sel bd.l` or `sel bd.l +3`, plus
  `linked in N panes`.
- Selection lives in the shell store. Views read it and request changes
  to it. A view never keeps its own copy.

### Refresh

- Panes whose command contains `@sel`, or whose view follows the
  selection, re-run when the selection changes.
- **Every** pane re-runs its command after any write made from the
  window (command line, props edit, a view's own edit).
- Writes from outside the window (MCP, CLI, sync) are picked up when
  the window regains focus, and by an explicit
  `refresh`.
- Re-running must be cheap enough for this. A view that's too slow to
  re-run gets a cache in its API namespace, not a stale-output mode.

### Keyboard and mouse

- **Pane-first.** Keys go to the focused pane, so tables can use
  `j`/`k` and single-key actions like `e` and `x`. `:` or `/` moves
  focus to the command line, and `esc` returns it to the pane.
- `^w` is the tiling prefix everywhere: `v`/`s` split, `h`/`j`/`k`/`l`
  focus, `<`/`>` resize, `o` only, `=` equalize, `x` swap. `1`–`9`
  switch layouts when no text field has focus, and `^1`–`^9` save one
  (see *Polish and unification*).
- **Text-editing panes** (Mora, worksheet, inline cell edits) capture
  every key until `esc`, except the `^w` prefix.
- The **key-hint bar** always reflects the current context: the focused
  view's own keys, then the shell's. Each view declares its hints (see
  the contract).
- **Mouse parity.** Every action can also be done with the mouse:
  - click a header to edit its command
  - click a pane to focus it
  - drag a divider to resize
  - drag the focused pane's header onto another pane to move it there
  - click a row or node to select it, with shift or ctrl to extend the
    selection
  - right-click for a context menu (`menu.js`)

  Keyboard focus still wins over hover.
- The existing `focus.js` rules (middle-click never pastes, click-away
  blurs) carry over unchanged. Undo stays global (`core/undo.py`,
  `undo.js`).

### Inspector

- **`props` is a pane** that follows `@sel`'s primary. It is the
  main place a mote is edited: title, key/value grid, `links N`, `→ relation target`.
- **Floating on demand.** `p` (settled in U3) pops the same editor
  as a floating panel over any layout that has no props pane, and it
  closes on `esc`.
- Pane and float are **one component**, so what you can do to a mote is
  the same in both, and the rule from `architecture.md` → *The
  inspector* still holds.
- The float is `--bg-raised`, 1px `--line-strong`, no shadow, no leader
  line.

### Settled for U3 (2026-09-30)

- **Floating inspector key: `p`.** A shell key, like `e` and `x`: it
  toggles the props float for the selection's primary, and `esc` closes
  it.
- **Props edits are commands.** Every edit in the props pane or float
  runs a line through the command line (`set #id key value`, `rename`,
  `A -> type -> B`, `unlink`, `template`, `open`), so it is in the
  history, the log and undo, exactly as the U2 table's edits are.
- **Command-line undo, per statement form.** Each write path in
  `core/command_bar.py` returns an undo Command on its result, which the
  console (and the old windows' `/` bar) push.
- **Species palette: 24 entries** (12 golden-angle hues at two tones in
  the design's band), as U2's `palette.js` has them.

### Settled for U4 (2026-09-30)

- **U4 is `plot` only.** `fit` (fitting a rule's chosen inputs to data
  series, least squares with numpy) is its own step after U4, with its
  own plan. `fit` waits for it.
- **The `plot` grammar.** Names are garden-scope names (`bd.l`).
  ```
  plot bd.sigma bd.tau vs bd.l 4:24 --steps 200
  plot bd.sigma vs bd.l 4:24 --set bd.E=200e9 --scenario hot --log
  plot bd.sigma vs bd.l 4:24 by bd.w 1:3     # surface
  plot sigma vs l --across                   # correlate: one dot per mote with both keys
  plot "Load curve"      plot #id            # a saved plot, re-solved
  plot save "Load curve"                     # the only write (undoable)
  plot export csv|png|svg|pdf|tex
  plot … --data I vs V [--from bench.csv]   # measured points over the curve
  ```
  The command parses in `core/` to the spec Saved Plot motes already
  hold (`core/sweep.py::PLOT_KEY`), so `\graph{…}` and existing saved
  plots are unchanged.
- **Series colours: six tokens**, `--s1`…`--s6` in `tokens.css`: Moxie's
  six colour-vision-checked hues retuned into the design's band, with
  `--s1`/`--s2` equal to `--c-note`/`--c-event`. A seventh series repeats
  hue 1 with a dash pattern.
- **Ported from the old window:** sweep, surface and correlate; knobs
  (`--set`, dragged in the pane to rewrite the command) and the
  sensitivity ranking at the cursor; `plot save`; CSV/PNG export.
  **Not ported:** baselines (`--baseline`) and the grid (tiling replaces
  it). The old window is deleted.

### Settled for `fit` and the U4 follow-ups (2026-09-30)

- **`fit` fits a model to measured points.**
  ```
  fit C1.current vs C1.voltage --data I vs V [--from "bench.csv"] --free C1.r_a C1.r_b [--scenario s]
  fit … --apply
  ```
  The data is every mote carrying both keys (`I`, `V`); `--from`
  narrows it to one CSV import's rows (`imported_from`). `--free` names
  the inputs the fit may move, starting from their current values.
  Levenberg–Marquardt over the existing sweep solve, numpy only
  (`core/fit.py`): each value with its σ, RMS, R², iterations and
  whether it converged; a point that fails to solve is reported, not
  dropped. Also `monoss fit` and an MCP tool.
- **`--apply` writes value, σ and source**, as one undo step: the
  fitted value, its σ into the reserved `uncertainty` map, and `source:
  {kind: fitted, note: ‹the fit line›}` (`fitted` is a new source kind).
- **Its own pane, `fit`**: data dots, the fitted curve, and a
  results column. Nothing is written without `--apply`.
- **Unitless series split by magnitude.** Series with no unit whose
  ranges differ by more than 100× take a second y axis, as two
  dimensions do (`core/plotview.py::_bands`).
- **Small plot-pane fixes first:** hide the rail in a narrow pane (its
  content moves to the status line, `r` toggles it); rail headings
  ellipsize; the y title ellipsizes; correlation labels avoid dots and
  hide when they can't fit; a truncated pane header shows its full
  command on hover; the garden is read under the lock and solved outside
  it, and a pane cancels its own stale runs; `plot save ‹name›` enters
  the history once; PNG export embeds Geist.

### Settled for U5, Motion (2026-09-30)

- **No board.** The node-card board is not ported; a graph of the model
  is the `graph` pane (U8). Linking stays a command.
- **Grammar:**
  ```
  sim [--ticks N] [--scenario s] [--vs s2] [--solver newton] [‹find conditions›]   sim
  rule ‹rule›|@sel                                                                 rule
  scenarios                                                                        scenarios
  solve [--live on|off]      one static solve, written, undoable
  why ‹m.k›                  explain a solved value
  step +N|-N|‹tick›, play, pause
  mo tion                    opens scenarios | sim | rule @sel
  ```
- **The run table is its own pane, a watch list for a run**, not a
  second mote table: one row per value (`C1.current`) with its state
  (given, solved, varied, pinned), its value at the tick, Δ/tick, a
  range bar over the run, and with `--vs` the other run's value and Δ.
  Same table component and find conditions as Monolith; nothing is
  edited in it. The Monolith table never knows about runs. Default
  rows: the values the run moves, the solver's freedoms and the givens
  the scenario varies; with find conditions, every numeric value of the
  matched motes.
- **Fast mode is a workspace `live` toggle**: `solve --live on|off`, per
  garden in the console sidecar, off by default. When on, every write
  made from the window is followed by a solve that writes, one undo step
  with the edit; the top bar says `live`.
- **One shared playhead** in the store, like the selection. Every Motion
  pane follows it; the top bar shows `tick 12/200 ▶`. Not saved in
  commands.
- **Comparing:** two sim panes under different `--scenario`s, synced by
  the playhead; `--vs` adds a Δ column, `d` toggles it against the
  garden as it stands.
- **Timeline markers:** constraint breaks and a failed tick in `--fault`,
  and the selected row's min/max over the run in `--dim`.
- **Ported extras:** scenario editing (`set --scenario hot C1.voltage 30`
  writes that scenario's override) and equation name hints in the rule
  pane. **Not ported:** value traces (lanes), the pre-flight role check
  and the impact view (both stay in the CLI/MCP).

### Built for U5 (2026-09-30)

What the build decided within the settled plan above. The user
confirmed these defaults on 2026-10-01.

- **Ticks.** `sim` runs 50 ticks unless `--ticks N` (1–2000). Tick `t`
  is the state after the solve whose `tick` variable read `t`, so ticks
  run 0 to N-1 and the top bar reads `tick 12/49`; a row's `start` (the
  garden, under the scenario) is what tick 0's Δ is measured from.
- **States.** `solved` (the solver determined it at some tick), `varied`
  (the scenario overrides it), `pinned` (a declared freedom nothing
  solved, so it keeps its value -- drawn `--warn`), `given`.
- **Rows** are ordered by name and capped at 200; the rest are counted.
- **`--vs garden`** names the garden as it stands; a scenario called
  "garden" is reached as `#id`. `d` toggles `--vs garden`.
- **`why ‹m.k›` is a pane**, the rule pane's second verb: `w` on a
  binding opens it there and `esc` goes back.
- **Playhead keys** in every Motion pane: `space` play/pause, `, .` one
  tick, `[ ]` ten. It plays at 12 ticks a second and stops at the last.
  `step` alone is `step +1`. With no sim pane open, `step`/`play` say so.
- **The owner** of the playhead is the sim pane that last ran (unless
  another holds it) or was focused; its run (`ticks`, `scenario`,
  `solver`) is what the rule pane asks its bindings' series under.
- **`mo tion` replaces the focused pane** with its three (^⏎: a new
  split's), focused on `sim` -- an app may now open a layout
  (`ConsoleApp.layout`, the `open_layout` effect).
- **Scenarios pane:** `⏎` runs the sim pane under the scenario (or opens
  a sim pane), `v` compares against it, `a`/`n` put `set --scenario …`
  on the command line (`ctx.prefill`), `d d` clears an override.
- **`set --scenario ‹s›`** makes the scenario mote when no mote has that
  name, and refuses a name that is a mote but not a scenario. `set` also
  takes `‹m.k› ‹value›` everywhere (`set C1.voltage 30`), unless `m.k`
  is itself a mote's whole name.
- **`solve`** refuses a garden whose rules use `prev()`, which only a run
  can answer, and so does a live solve (the edit stays, the message says
  why). One undo step (`why` is live, so there is no record to put back).
- **New token:** `--timeline-h` 72px, the sim pane's timeline.
- **The timeline shows only when the answer depends on time:** some
  watched value (or its `--vs` partner) changes over the run, or the run
  failed partway. It also hides when the pane is too short to keep six
  table rows above it (2026-10-19).

### Settled for U6, Mora (2026-10-01)

- **Two panes, not one with modes.** `edit ‹note›` is the source and
  `read ‹note›` the rendered document; tiling replaces the old
  Source/Split/Read switch. The read pane shows unsaved typing live,
  through a draft shared in the store. `mo ra` opens
  `notes | edit @sel | read @sel`.
- **Geist Mono everywhere**, but the read pane is its own surface: a
  dark gray paper (around L 0.21, softer off-white text, gray rules, no
  hue), so a rendered document never looks like the editor. Math is
  KaTeX, moved from Mora's vendor folder (not a new dependency).
- **Autosave, one undo step.** A note writes on a pause in typing; one
  editing session (`i` … `esc`) is one garden undo step and one log
  line, not a history entry. Inside the editor, Ctrl+Z is the text
  box's own undo.
- **Scope:** render every phase 3/7 macro (`\work`, the calc sections,
  tables, figures, numbered equations and `\ref`, `\rule`, `\include`,
  `\verification`), find/replace in the editors, and **the worksheet
  pane**. Not in U6: print to PDF (phase 6 does real PDFs) and the `@`
  mote picker (links are typed, `\mote{name}` or `\mote{#id}`).
- **The worksheet is a pane** (answers the open question): a second
  text-editing pane over `core/worksheet.py`, in a **full blueprint**
  scheme (deep blue ground, pale blue-white text, faint row grid, line
  kinds in tints of the same blue; status colours still mark errors and
  checks). Typing only parses; **`esc` applies** the whole sheet as one
  undo step. With `live` on, a solve follows.
- **New colours.** The paper and blueprint surfaces are the first
  tokens outside the neutral/status/species/series sets; the user asked
  for both.

### Built for U6 (2026-10-01)

What the build decided within the settled plan above.

- **Verbs:** `edit`, `read`, `notes [text]`, `worksheet`. `edit` on a
  name no mote has makes the note on its first save; on a mote with no
  note it opens a blank page. `mo ra` replaces the focused pane with
  `notes | edit @sel | read @sel`, focused on `edit`.
- **Modal editors** (`js/editor.js`, shared by `edit` and `worksheet`):
  `i` / `⏎` start, `esc` stops. `^f` find/replace inside the pane: `⏎`
  `⇧⏎` next/previous, `tab` between fields, `⏎` in replace replaces
  one, `^⏎` all -- through the textarea's own editing, so `^z` takes a
  replacement back. `tab` while typing inserts two spaces.
- **`edit @sel` never switches note while you type**; a selection change
  catches up on `esc`.
- **Saving:** 0.9 s after typing pauses. Every save of one session is
  pushed with the same coalescing key, so the session is one undo step.
  `esc` logs `saved ‹note›` and re-runs the panes.
- **Drafts** are store state (`store.setDraft`, the `draft` event), keyed
  by note id (or `new:‹name›`); a read pane re-renders 0.18 s after one.
- **Problems** come from rendering the draft and underline their lines in
  the editor (hover for the message).
- **Images** (2026-10-04): `^v` with an image on the clipboard stores it
  (`api.mora.paste_image` -> `core/images.py`, auto-named `shot-NNN`, one
  undo step of its own) and inserts `\image{shot-NNN}` on a line of its
  own; text still pastes natively. The read pane draws an `image` block
  as an `<img>` from the payload's data URI, with a caption and number
  inside a `figure`. The `image add ‹path› [name]` and `image rename
  ‹old› ‹new›` verbs are write verbs like `rename`; a rename rewrites
  every note that names the image and is one undo step.
- **Worksheet:** previews 0.15 s after typing; margin marks `=` `?` `ƒ`
  `≡` `✓` `!`; an unknown or equation shows the garden's value after it.
  After `esc`, a clean sheet is replaced by the garden's reading of it;
  one with problems keeps your text.
- **The inspector** in the old windows no longer opened notes; it
  summarized a `modoc` property read-only (until U8 deleted it).
- **New tokens:** `--paper-*` (the read pane), `--bp-*` (the worksheet),
  `--doc-measure` 72ch (the read pane's line length).

### Settled for U7, Monastery (2026-10-01)

- **Scope:** port the calendar (every binding, recurrence, move/resize
  with "this one or the series", new events) and add **project bars**.
  Plan-my-week and planned vs. actual (roadmap phase 5) were not in this
  pass; they are built (below, *Plan my week*).
- **Three panes, each its own command:** `agenda [from] [to]`, a dense
  table with a row per occurrence grouped by day (the keyboard home);
  `week [date]`, the hour grid; `month [date]`, the six-week grid.
  `mo nastery` opens `agenda | week`.
- **Bars** (start → due across the days, above the hour grid and in the
  agenda): any multi-day value (`2026-10-01/2026-10-09`), and any mote
  with both a start and a `due` (a `start`/`starts` value, else the day
  its `garden` project was started). Project owners (phase 4) appear on
  their own because they carry `due`.
- **⏎ selects, `o` follows:** a mote with a note opens `read` on it, a
  project owner `cd`s to its garden, anything else opens `open #id` in a
  new pane.
- **Editing is keys plus drag, and every write is a `set`:** `J`/`K`
  move the occurrence by an hour (a day when all-day), `H`/`L` by a day,
  `<`/`>` shorten or lengthen it; drag moves and edge-drag resizes in
  the week grid. A recurring event asks first: `t` this one, `s` the
  series. `n` on an empty slot prefills `‹name›.date = ‹the slot›` on the
  command line.
- **Filters live in the command:** `agenda --keys due,date --type task`,
  and find conditions (`agenda status!:done`). The default is every
  binding found; `b` cycles a key by rewriting the line.


### Plan my week (2026-10-05)

Roadmap phase 5's last two parts, as verbs over `core/planner.py` (the
calendar panes need nothing new: a block is an ordinary dated mote):

- **`plan [--days N] [--hours 9-17] [--block 2h] [--from ‹date›]
  [--apply]`.** Open work is a mote with an `estimate` (hours: `3`, `2h`,
  `90m`) and a future `due` whose `status` is not done or submitted. Its
  remaining hours (estimate, less logged and already-planned time) are cut
  into blocks of at most `--block` and placed earliest deadline first in the
  earliest free slot inside the work hours, never after the due. A mote with
  a `due` and no estimate is listed as skipped, one past due as overdue,
  and what does not fit as short by so many hours. Alone it only previews;
  `--apply` writes, as one undo step.
- **A block** is an instance of the species `Work block` (made on first
  use) with `when` (the slot), `kind: planned`, `status: planned` and a
  `for` relationship to the mote the time is for. `o` on a block follows
  `for`. Doing the work is `set` ing the block's `status` to `done`.
- **`worked ‹mote› ‹duration› [‹date›]`** logs an unplanned stretch as a
  `kind: actual` block (ending now, or at the work day's start on `date`).
- **`budget [‹mote›]`** is planned vs. actual: estimate, done (logged plus
  planned blocks marked done), planned ahead, left.
- Settings are flags, not stored: 9:00-17:00, 2 h blocks, 7 days.

### Built for U7 (2026-10-01)

What the build decided within the settled plan above.

- **Defaults:** `agenda` runs from today for 14 days; a week starts on
  Monday; a month is the six weeks around it. Dates also read as `find`
  reads them (`today`, `7d`, `-2w`).
- **Paging rewrites the line** (`week 2026-10-12`); `t` drops the date,
  so the pane follows today again.
- **`b` hides the key of the event under the cursor** (`--keys` lists the
  rest), and `B` shows every key -- the "cycle a key" of the plan.
- **Moves:** `J`/`K` an hour (a day when all-day), `H`/`L` a day, `<`/`>`
  half an hour (a day when all-day); drag snaps to a quarter hour.
- **"This one" is the `detach` verb** (`detach #id ‹key› ‹occurrence›
  ‹start› ‹end›`): the one edit a `set` can't say, one undo step.
  Deleting one occurrence is a `set` adding an `EXDATE`; deleting a
  whole mote puts `delete #id` on the command line to confirm (since
  the post-U9 fixes, `d d` runs it; see *Fixes after U9*).
- **`n`** puts `‹name›.date = ‹slot›` on the command line with the caret
  where the name goes (`ctx.prefill` takes a caret now). The slot is a
  clicked one in the week grid, else the cursor's day at 09:00.
- **Rows vs bars:** a multi-day all-day value is only a bar; a `due`
  stays a row on its day even when the same mote also has a due bar.
- **Old windows' launchers** offer *Mosaic* (the console) in place of
  Monastery; Gardens' stale Monolith tile is gone.
- **New tokens:** `--wk-gutter` 6ch, `--wk-hour` 36px.

### Settled for U8, Monarch (2026-10-01)

- **One line says what is drawn:** `graph [@sel|#id|‹name›] [--depth N]
  [find conditions]`. A root draws its neighbourhood (default depth 2),
  no root the whole garden, and a `@sel` root re-roots as the selection
  moves. `layout graph` opens `find | graph @sel --depth 2 | open @sel`.
- **Gestures become commands:** ctrl+drag node → node prefills `A ->
  ‹type› -> B` with the caret on the type; `n` prefills a new mote; `d d`
  runs `delete` (`unlink` on an edge; it prefilled until the post-U9 fixes). Not ported: copy/paste, the
  multi-select panel, renaming a relationship type.
- **Layout: minimal plus pins.** The species-clustered force layout as
  it was; dragging a node pins it, pins saved per garden in the console
  state; `u` unpins. Hiding is in the line: conditions, `--hide-rel`,
  `--view`. Not ported: the force sliders, colour by property, arranging
  species attractors.
- **Style: squares plus kind marks.** Squares stroked in species
  colour; a species is a diamond, a rule has a hollow inner square;
  small arrowheads; an edge's type shows only around the selection or
  the pointer.
- **Annotations are removed** (already decided in *Steps*): the code,
  the docs, and a migration that drops their stored data.

### Built for U8 (2026-10-01)

What the build decided within the settled plan above.

- **The grammar** (`core/graphview.py`): `--hide-rel a,b` and `--view
  ‹saved›` join the conditions. A hidden type is not hopped either, so
  hiding `instance_of` stops a species pulling in every instance. A word
  with `:` or `~` is a condition; any other bare word is the root (a name
  with a colon is rooted by `#id`). `[`/`]` rewrite `--depth`.
- **The cursor is not the selection.** `h j k l` and the arrows walk the
  cursor to the neighbour lying that way on screen (Monarch's arrow
  walk); `⏎` or a click selects. `o` and double-click open `open #id` in
  a new pane.
- **View keys:** `f` fit, `+`/`-` zoom, `0` 100% at the cursor, `u`/`U`
  unpin one/all. Drag on empty space pans (no box-select); the wheel
  zooms at the pointer.
- **Prefills:** a link is `A ->  -> B` with the caret between the
  arrows; `n` is `‹cursor› -> related_to -> `, so the new mote is born
  linked and shows up in the neighbourhood. A mote is named by its name
  when that is one plain word unique on screen, else `#id`.
- **Drawn in screen space:** nodes, labels and arrows keep their pixel
  size at any zoom (selected half-size 7, labelled 5, unlabelled 3.5);
  labels show everywhere up to 60 nodes, else around the focus or past
  110%. The loop stops when the layout settles.
- **An empty `@sel`** reads "nothing selected", dim, as the props pane
  does.
- **Pins** live in the console sidecar (`pins`, `{id: [x, y]}`), beside
  layouts and history.
- **Removed with Monarch:** `api_common`'s `InspectorApi`,
  `CommandBarApi`, `UndoableApi` and `AnnotatableApi`; every
  `frontend_shared/` file but `focus.js` (inspector, interval picker,
  command bar, menu, undo, launcher, annotations); `run_monarch.py` and
  its smoke test. `core/db.py` drops the `annotations` table on open.
  The Gardens window's *Open in* offers only Mosaic.
- **Lost with the old inspector:** the `url` property's *Open* button.
  The props pane has no way to open a link yet (open question below).

### Settled for U9, Home (2026-10-01)

- **Two panes:** `mo saic` opens `gardens | garden`. `gardens` is the
  ownership tree with loose gardens and archived ones folded at the
  bottom, each row showing the cover (name, due, status) and counts;
  `garden` follows the tree's cursor with path, counts, size, last
  changed, sync, archive reason and ownership problems.
- **`garden` subverbs:** `garden new|rename|rm|restore|export|import|
  archive|unarchive ‹name›…` and `garden sync ‹name› ‹url›|off|now|auto
  on|off`, beside `cd`. Import and export take a path, or ask with a
  native file dialog. *Start project* is `project start @sel [name]` /
  `project stop @sel`.
- **Removing:** a typed `garden rm` asks for a second ⏎ (the tree's `d d`
  doesn't, since the post-U9 fixes); `garden restore` brings it back. Not on `undo`, which belongs to the open garden.
- **Also in:** the top bar's breadcrumb is the ownership path, and a
  crumb `cd`s there.
- **Not in:** a default-garden setting. (Cross-garden search is
  `monoss search`; health marks came on 2026-10-05, below.)

### Home health (2026-10-05)

- Each `gardens` row has a **solve** mark (`ok`, or `!n` for n culprits
  and unit problems) and a **checks** mark (`passing/total`, red when one
  fails, amber when one could not be checked). A garden with no equations
  has neither. Both come from `core/home.py::health` (core/health.py and
  `calc.checks`): no solve and no note rendering, so Home opens as fast as
  counting motes.
- The `garden ‹name›` pane adds the sentences: what is under-determined,
  each unit problem, each failing or unchecked check.
- **Decided:** document health (unresolved macros) and the submitted
  revision are *not* marked; the revision is already on the cover.

### Built for U9 (2026-10-01)

What the build decided within the settled plan above.

- **The detail pane follows by rewriting its line:** the tree's cursor
  hands its garden over (`ctx.paneAction`), and the pane sets its own
  line to `garden ‹name›`, so a layout reopens on it. `garden` alone is
  the open garden; a garden named like a subverb is `garden show ‹name›`.
- **Tree keys:** `j k` move, `⏎` `cd`, `n` new, `r` rename, `d d` remove
  (runs `garden rm`), `a` archive/unarchive (runs),
  `s` sync, `e` export (runs, with a dialog), `i` import, `z` folds the
  archived by rewriting the line to `gardens --archived`.
- **The open garden** can't be removed (`cd` elsewhere first); renaming
  it reopens the console on the new name with a fresh undo stack.
- **Lifecycle sync now honours auto-sync.** The console's open, switch
  and close syncs used to run whenever a garden had a sync server,
  ignoring the per-garden auto flag; they now skip when it is off.
  `refresh` still syncs either way.
- **A file that isn't a garden** lists uncounted instead of breaking the
  tree (the ownership and archive scans in `core/projects.py` skip it).
- **Deleted:** `gardens_app/`, `run_gardens.py`, `api_common.py` (its
  last live piece, the background sync, is `console/session.py`'s now),
  the console's `legacy` app mechanism, and `frontend_shared/`
  (`focus.js` is a file of the console's own). `window_launcher.py`
  stays as the one window's launch boilerplate.

### Fixes after U9 (2026-10-01)

The user's first list of small fixes after the rework.

- **`^⏎` runs into a new pane** (was `⌥⏎`). No key has a macOS spelling
  or a Cmd alias any more: `^` means Ctrl only.
- **A new pane is blank and asks for an ordinary command** (replaces the
  `view>` picker): the command line takes the caret the first time the
  pane is focused, `⏎` runs into it, `esc` (there or in the pane) closes
  it, and typing in the pane goes to the command line (`js/blank.js`).
- **`d d` is the confirmation.** In every pane, the second `d` runs the
  delete (`delete`, `unlink`, `garden rm`, clearing an override) through
  `ctx.runCommand(line, {confirm: true})`; nothing is prefilled for a
  second ⏎. A recurring event still asks `t` / `s`. A typed `delete` or
  `garden rm` still asks for a second ⏎.
- **The focus ring is drawn over the pane's content** (`.pane.focused
  ::after`), so a view's grid lines pass under the pane's edge.
- **The week grid fills its pane:** an hour is the pane's height over 24,
  never below `--wk-hour` (then it scrolls).
- **The read pane centres its column** (`--doc-measure` wide).
- **Monarch's canvas repaints in the frame it is resized**, ignores
  no-op and zero-size resizes, and repaints after any layout change.
- **The worksheet reads in blocks** (`core/worksheet.py::read`): one per
  independent calculation (equations sharing a mote), a blank line
  between; in each, the givens and unknowns of its motes, then its
  equations in dependency order (a simultaneous system keeps creation
  order). The user chose this over blocks per mote and over `#` headings.

### Polish and unification (2026-10-02)

The user's second list of fixes, and the pass that makes the console
read as one tool rather than seven apps.

- **Header holds the result, footer holds the cursor.** A pane's header
  summary is what its run produced (counts, timing, problems, level); a
  status line under a plot or a sim holds only what is at the cursor or
  playhead (the x/y readout, a row's value with its min and max, knobs
  when the rail is hidden). Shell state (the tick, `live`) is only in the
  top bar. Nothing is said in two strips.
- **No filler text.** The blank pane draws nothing (the hint bar says `⏎`
  and `esc`). Empty states say the fact (`no scenarios`, `no gardens`,
  `nothing to verify`) and leave how-to to the hint bar and the manual. A
  pane whose header already says `nothing selected` draws an empty body.
- **`^⏎` and `⇧⏎` split as vim does.** `^⏎` runs the line into a new
  pane below (a split, as `^w s`), `⇧⏎` into one to the right (a vsplit,
  as `^w v`). A view's own "open in a new pane" (`o` in the graph, a
  month day) still opens to the right.
- **Layouts have slots.** A saved layout carries a `slot` (1–9) in the
  sidecar: `1`–`9` switch to a slot, `^1`–`^9` save the working tree into
  one, keeping the name of the layout there (an empty slot's is `N`).
  `layout save ‹name›` keeps a layout's slot, and a new name takes the
  lowest free one. A tenth layout has no slot and no tab, and is reached
  with `layout load`. Sidecars from before slots get them in save order
  on load.
- **A pane moves by its header.** Dragging the focused pane's header
  (past 4px; less is still a click that edits the command) onto another
  pane docks it against that pane's nearest edge when the pointer is in
  its outer quarter, and swaps the two in the middle. An outline in
  `--bright` shows where it lands; `esc`, its own pane or no pane cancel.
  The moved pane keeps its view (`tree.js` `move`, `swapLeaves`).
- **No apps: built-in layouts replace `mo`.** One window made the seven
  app brands pointless. `mo` is no longer a verb (a mote may be called
  `mo`), and `layout ‹name›` opens a layout in the focused pane's place
  (`^⏎` / `⇧⏎`: a new split's). Every garden has five built in, named by
  task:

  | Layout | Panes |
  |---|---|
  | `calc` | `scenarios \| sim \| rule @sel` |
  | `docs` | `notes \| edit @sel \| read @sel` |
  | `plan` | `agenda \| week` |
  | `graph` | `find \| graph @sel --depth 2 \| open @sel` |
  | `gardens` | `gardens \| garden` |

  `layout load ‹name›` is the same; a garden's own saved layout of that
  name wins over the built-in. `layout list` names the built-ins after the
  saved ones. The table and the plot are one verb each (`find`, `plot`).
- **One top bar.** Its left end is the `[mo]saic` wordmark and the
  ownership path, whatever is focused; the focused pane's app suffix is
  gone. The command line no longer draws a typed `mo ` as a box.
- **A view is named for its verb.** View ids lost their app prefixes:
  `table` (answers `find`), `props` (`open`), `rule` (also `why`), and
  every other view is its verb (`plot`, `sim`, `edit`, `week`, `graph`,
  `gardens`, …; `core/commands.py::view_for`). Each view's file is named
  after it (`views/plot.js`), and a view has no `app` field. Layouts store
  commands, not ids, so saved layouts are untouched. The sections above
  were updated to the new ids. The Python namespaces (`api.moxie`,
  `console/motion.py`, …) keep the old names until the code rename.
- **A key means one thing in every pane.** `j k` / `h l` move, `g G`
  top and end, `[ ]` step the pane's own dimension, `- +` less and more,
  `⏎` acts on the row, `o` opens or follows, `i` edits, `n` new, `r`
  rename, `a` add, `v` compare, `d d` delete, `space` toggles (plays in
  the sim panes), `esc` backs out. The clashes were remapped: in the table
  `^a` selects all and `S` groups (`g G` move to the ends); in the plot
  `space` shows or hides a series and `R` the side column; in the fit `A`
  applies; in the sim `v` toggles `--vs garden`; in the gardens tree `z`
  archives, `Z` shows the archived, and `E` / `I` export and import (so
  `e` is the shell's again). `s` is still sort in the table and sync in
  the gardens tree, and `t` / `s` answer the calendar's "this one or the
  series?". The sections above record the keys as they were built.

### Settled for `graph --flow` (2026-10-05)

A mode of the graph for maths-heavy gardens. It lays itself out, with
no pins, and draws what calculates what. Like Motion's old board, it
shows equations and values.

| Topic | Decision |
|---|---|
| Surface | `graph [@sel\|#id\|‹name›\|‹m.k›] --flow [--depth N] [conditions]`; `--hide-rel` is refused (flow draws no relationships) |
| Structure | the solve's journal (`core/simulation.py::trace`), not `incidence.blocks()`, which would merge a chain into one block. Each step the solve took is a **group**: one equation that determined one value, or a coupled set the solver found together |
| Columns | left to right: **givens**, one column per solve step (one right of the deepest step it reads), **answers** (solved values no other step reads; a check reading one does not count) |
| Bands | each independent calculation (steps joined by a value one computes and another reads) is its own band, with its own givens, steps and answers; bands stack. A shared constant joins nothing, and each band reading it shows its own stub |
| Direction | the current solve's, under the playhead run's scenario: flip which value is given and the arrows flip |
| Cards | rule cards (name, KaTeX equation via `core/formula_tex.py`, a coupled group's residual or a check's margin); mote stub cards, one per (mote, group), siblings linked; rows only for solve values (`+N more`), each with value, unit, ±, source mark, check mark |
| Checks | a check (a limit, a `?=`, an equation already satisfied) sits after what it reads and marks those rows pass/fail |
| Folding | a group of ≥4 equations starts folded to a header (vertical). A **sub-calculation** -- steps whose results are read only inside the set, so it has one way out -- folds sideways into one card, `S ← 3 steps`, starting open. Folding is done in `core/` (the folded set is contracted before ranking), so it really narrows the picture. Fold state is view state: sent with each run, not in the line |
| Root/depth | `--depth` counts solve steps, upstream and downstream of the root's groups (default 2); a mote root means all its solve values |
| Wires | right-angled, in the gaps between columns; a wire crossing columns rides a relay (one per value and column), and wires leaving one port share a trunk. The cursor's wires are full strength |
| Follows | the playhead (its run's scenario and series), the live solve (a write re-runs every pane), and health: a failed solve draws as far as it got, with the unsolved equations as fault groups in `solve_health`'s words |
| Keys | `h j k l` cursor, `⏎` select, `o` open, `e` prefill `set`, `z` fold under the cursor, `Z` all, `[ ]` depth, `f` fit; the playhead is `space , .` |
| Renderer | HTML cards with SVG wires underneath (`js/flow.js`); the force graph's canvas is untouched |
| Layout home | `core/flowview.py` (groups, ranks, folds, relays, barycenter order), tested; JS measures and draws wires. MCP `solve_flow` returns the same payload |

### Built for `graph --flow` (2026-10-05)

What the build decided within the settled plan above.

- **One verb, two views.** `views/graph.js` holds whichever instance the
  line asks for: `views/graph_force.js` (the U8 view, moved unchanged)
  or `views/flow.js`. A rewrite that adds or drops `--flow` swaps them.
  The bridge is `api.monarch.flow(command, selection, run, folds)`.
- **Fold ids are stable.** A fold's id is a hash of its values
  (`block:…`, `cone:…`), so the pane's fold state survives a re-run and
  an edit that leaves those values alone. `z` acts on the first of the
  group's `folds`: its own fold, else the sub-calculation it ends, else
  the smallest one it is inside. With nothing to fold, it says so.
- **Wires.** There is one relay per value and column, so every wire
  further right leaves from one trunk. Tracks are spread per gutter by
  source port, in the order the sources sit.
- **Cards say each thing once.**
  - An unnamed worksheet rule shows only its equation (its name *is*
    the equation).
  - `+N more` appears once per mote, on its leftmost stub, counting the
    numeric properties no stub shows.
  - A solved value with no declared unit shows the unit a solve
    infers, in the base unit it was solved in. Inferred units are never
    stored (`simulation.md` → *Inference*).
  - A fault states its own counts in `solve_health`'s words. Health's
    blocks would blame the steps that did solve.
- **What an equation reads is taken at the solved point**
  (`simulation._touch_at`). The incidence probe evaluates with 1.0 for
  every unknown, and when that fails (`arccos` out of its domain) it
  counts *every* unsolved value. That is a safe over-count for health,
  but it wired one rule to the whole garden. Re-read at the values the
  solve found, the record is exact; the probe stays as the fallback.
- **Relays are drawn** as the wire's run across the column they pass, so
  a long wire is one line, not segments ending at column edges. The run
  is an SVG path measured in the same pass as the elbows, and the pass
  re-runs whenever a group or the sheet resizes (fonts arriving, KaTeX
  setting a card). An HTML run measured once drifted a few pixels from
  its elbows.
- **Products**: a thin space between single-letter factors (`48 E I`), a
  `·` when either is a word or a dotted reference (`density · n.v`).
- **Limit: a rule that cannot be evaluated** (a name that resolves to
  nothing) fails the solve before its first step, so the whole picture
  is fault groups in one column. This is what `solve` itself reports.
- **Cost.** Every run re-solves in memory. Medium gardens are bound by
  the solve: an 84-equation, 81-step chain takes about 0.35 s.
- **Smoke test:** `graph C1 --flow` draws the givens, the rule and the
  answer, wired; `l` walks to the answer; `e` prefills `set`; `z` says
  there is nothing to fold.

### Settled for knobs (2026-10-06)

- **A knob is `--set m.k=value [unit]` on a pane's line**, on `plot`,
  `sim`, `fit` and `graph --flow`. It tries a value and writes nothing:
  the pane re-solves as if `m.k` held it. A pane is still its command, so
  a layout that restores the line restores the knobs, and a saved plot
  keeps its own. `core/knobs.py` is the one grammar; the solves take the
  values as `{mote_id: {key: value}}` overrides laid over the scenario's
  (`scenarios.overlay`).
- **`set` writes, a knob tries.** The value reads as `set` reads it, so
  `200 GPa` works; but a knob's unit is *converted* into the property's
  own, never relabelled, because a trial must not change what a number on
  the garden means. A unit on a property that declares none is refused.
- **Where they apply.** `sim`: the run itself, never its `--vs` partner,
  so `--vs garden` compares the knobbed run with the plain garden; the
  knobs also travel in the playhead's run, so the `rule` and flow panes
  following it read the same trial. `fit`: held fixed (a knob on a
  `--free` input is its starting value). `graph --flow`: its own line's
  knobs over the playhead's; ± is not shown under knobs.
- **`--keep` (key `W`) is the way out**, shaped like `fit --apply`: the
  pane's payload carries the line with `--keep` and the line without
  knobs; the key runs the first on the command line (history, log, one
  undo step, through `set`'s own writer), then the pane takes the second.
- **Only `plot` drags them.** The other panes show knobs in their header
  and take them by retyping the line (`e`).

### Settled for time on the plot (2026-10-06)

- **`plot ‹m.y›… vs t [lo:hi]`** draws the run `sim` walks: x is the tick,
  whole ticks from 0 (default `0:50`, as `sim`). It is a sweep in every
  other way, so `plot save`, `plot export`, `\graph{…}`, knobs and
  scenarios work unchanged; the spec's `x` is `{"time": true, "lo", "hi",
  "steps"}`. The sim and the plot share `simulation.iterate`, so one run is
  never described twice.
- **Zoom stays whole ticks** (a drag widens to the ticks it covers), the
  run is always solved from tick 0, and a failing tick ends it as a failed
  span. `fit` and `--data` over time are not done.

### Settled for live documents (2026-10-19)

- **Documents read a live solve** (`core/simulation.py::live_garden`;
  `docs/modoc.md`), so a note's values are never older than the last
  edit and need no `solve`. `sim` and `plot` still write nothing, and
  `solve` stays the one write.
- **A freedom that holds no number reads `not solved`** (`--warn`) in the
  props pane and in a find table's cell, instead of a blank or a missing
  row (`core/inspect.py::unsolved_keys`; a cell's `unsolved`). The props
  pane lists such a freedom even when no property holds it.
- **The sim header ends `preview — solve writes`.** There is no key that
  writes a tick.

### Visual system

- The tokens, type scale, spacing, 0px radius, inverted selection, and
  "no animation except cursor blink and playhead" rules all come from
  the original design. They live in `console/frontend/tokens.css`: put the tokens in one CSS file that every
  view uses, and don't hard-code a color anywhere else.
- **Color.**
  - Mote color comes from the species palette (`architecture.md` →
    *Species colour*), with its lightness and chroma constrained to the
    design's band (L ≈ 0.78–0.82, C ≈ 0.11–0.14). Hues still walk by
    the golden angle.
  - A species mote draws as the `--bright`-ward variant of its
    instances' color.
  - Status colors are fixed tokens, separate from species: `--ok`,
    `--warn`, `--fault`, `--idle`. They mean simulation state, solve
    health, checks (green/red), overdue, clamps and errors.
  - There are no other colors.
- **Font:** Geist Mono 400/500/600, vendored under the new frontend's
  `vendor/fonts/` with its license, and loaded from disk. It's the only
  family in the shell.
- **Dark only.** No light theme is planned.
- **Graph styling** (square nodes, dashed unselected edges, labels to
  the right) is applied to `graph.js`'s canvas renderer, and **plot
  styling** to `console/frontend/js/plot.js`.

### Persistence

- Layouts and per-context command history are **UI state, not
  knowledge**. They go in a sidecar next to the garden,
  `gardens/<name>.db.console.json` (`console/state.py`).
- They are not motes. They don't live in the `.db`, and they don't
  sync.
- `core/gardens.py` moves, trashes and restores the sidecar with its
  garden, so a rename carries it and a delete drops it, from the CLI
  too.
- `localStorage` is only for trivial per-view conveniences, such as a
  collapsed section, that are fine to lose.

### The view contract

Every view is one ES module that registers itself with the shell. The
exact shape is documented at the top of
`console/frontend/js/views/index.js`. It covers:

- **`id`, `app`, `describe`**: the id and one-line description that
  appear in the completion popup.
- **`commands`**: the verbs this view answers, and a completer for
  their arguments (driven by `core/` data, not hard-coded lists).
- **`create(ctx)`** returns one instance per pane, so two panes can
  show the same view.
- **`mount(el, command)` / `unmount()`**: render into the pane body,
  and on unmount remove every listener, timer and animation frame.
  Leaks are bugs.
- **`run(command)`**: re-run in place (for `⏎`, a selection change or a
  data change). It returns the header summary (`16 motes`, `0.4s`,
  `1 clamped`) and an ok/warn/fault level, plus an optional `detail`
  shown as the summary's tooltip (a view that counts problems lists
  them there).
- **`followsSel`**: true when the view re-runs on selection change even
  without `@sel` in its command.
- **`hints()`**: the key hints shown while it's focused.
- **`onKey(event)`**: the view's own keys. It returns whether the event
  was handled, so the shell can fall through to its own keys.

`ctx` gives the view the API namespace, the store (selection, garden),
the shared components and `ctx.navigate(moteId)`. A view talks to other
views only through the selection and the command line, never directly.

## Steps

The UI track runs in parallel with the roadmap phases. Each step is its
own plan-mode phase and ships something usable.

| # | Step | Proves |
|---|---|---|
| U1 | **Done.** **Shell.** Root API and shared connection; tokens and vendored font; top bar, workspace, command line, hint bar; split tree, `^w` keys, divider drag, view picker (since replaced by the blank pane); layouts and history file; merged grammar in `core/` with app-switch completion; store; components module; `log` view. | Two panes, split and resized from keyboard and mouse. A layout survives a restart. |
| U2 | **Done.** **Monolith → `mo nolith`.** `find` table with multi-row selection, batch edits via `@sel`. Delete Monolith's window. | The contract holds for a real view, and selection is shared. |
| U3 | **Done.** **Props → `mo te`.** Props pane plus floating on demand. Replaces `inspector.js` for the shell. | Editing a mote works identically everywhere. |
| U4 | **Done.** **Moxie → `mo xie`.** The `plot` pane on the restyled `plot.js` (see *Settled for U4*), then `fit` and the follow-up fixes (*Settled for `fit`*), also done. | Commands carry view state (ranges, series). |
| U5 | **Done.** **Motion → `mo tion`.** Scenarios, state table with timeline and playhead, rule pane. No runs list (see *Settled for U5*, *Built for U5*). | A live, ticking view inside the contract. |
| U6 | **Done.** **Mora → `mo ra`.** Document pane as a text-editing pane, plus the worksheet (see *Settled for U6*, *Built for U6*). | Keyboard capture and `esc` work for editors. |
| U7 | **Done.** **Monastery → `mo nastery`.** Agenda, week and month panes with bars (see *Settled for U7*, *Built for U7*); plan-my-week and planned vs. actual wait. Coordinate with roadmap phase 5, and build the agenda features here, not in the old window. | Date-shaped data in the grid style. |
| U8 | **Done.** **Monarch → `mo narch`.** Canvas graph only, restyled (see *Settled for U8*, *Built for U8*). Remove annotations (`core/annotations.py`, the annotation frontends, their docs) and ship a migration that drops their stored data. | The frozen debugger costs no more upkeep. |
| U9 | **Done.** **Home → `mo saic`** (see *Settled for U9*, *Built for U9*). Ownership tree, loose gardens, garden management and settings. Delete `gardens_app/`, `window_launcher.py`'s multi-window pieces and the `frontend_shared/` symlinks. | The window is the only surface. |

Momentum is removed by roadmap phase 1 and is never ported. A roadmap
phase that needs a new surface (worksheet, checks, V&V table) adds a
view after U1, and after U3 if it edits motes.

Each step's done-check is the same:

- The view works from both keyboard and mouse.
- It appears in completion.
- It survives a layout save and restart.
- `docs/ui-setup.md` and `architecture.md` describe it as it now is.
- The old window is deleted.

## Rename

- **User-facing only for now.** Window title, wordmark, app names,
  hints and docs say **Mosaic**.
- Python packages, entry points, the MCP server name, data directories
  and the repo stay `monoss` until a separate approved phase (see
  `roadmap.md` → *Open questions*).

## Rules for agents on this track

- Read this doc and `console/frontend/tokens.css` before any UI step.
- Don't add a color, font, size, radius, shadow or animation that isn't
  in the tokens. If a view needs one, raise it as a question.
- Don't put logic in a view or in an API namespace that belongs in
  `core/` (`architecture.md` → *What belongs on which surface*). This
  includes command parsing and query semantics.
- Every capability a view offers must also be a command. If it can't be
  typed, it isn't finished.
- Keep plumbing inspectable. Anything a view creates in a garden is
  ordinary motes and relationships.

## Open questions

- **Verb list and view ids.** The tables above are proposals. Confirm
  each app's canonical commands in its port step. U2 built `find …`
  (view `table`, `mo nolith` = `find`) and `set
  @sel|#id|‹name› ‹key› ‹value›|--clear`, where `set`'s `@sel` is the
  whole selection and everywhere else `@sel` is the primary. U3 added
  `open @sel|#id|‹name›` (view `props`, `mo te` = `open @sel`),
  `rename`, `unlink` and `template`, which also take the whole
  selection. Confirm
  those, and whether the old window's bulk link, relationship matrix,
  bulk delete and *save view* come back as commands (`link @sel …`,
  `delete @sel`, `view save ‹name›`) -- U2 did not port them.
- **Size tokens added in U1.** The shell took these from the design
  canvas (no longer in the repo). Confirm or replace them:
  `--popup-mo` 36px, `--popup-app` 90px,
  `--divider-hit` 3px (invisible grab margin), `--popup-rows` 12,
  `--prompt-gap` 6px, `--bar-gap` 8px, `--border` 1px. U4 added
  `--plot-rail` 240px (the plot pane's legend/knob column; the design
  draws 320px) and the series hues `--s5` (195°) and `--s6` (40°), which
  are not colour-vision-checked against the other four.
- **Plot keys.** The original design's keys were `f x z t e ?`. `x` and `e`
  are shell keys (close pane, edit command), so the plot pane uses `t`
  to show/hide a series, `z`/`Z` to zoom, `y` for log y, `[ ]` to pick
  a series, `j k` / `- +` for knobs. `f` waits for `fit`; export is
  `plot export`. Confirm.
- **Pane header style.** Every header uses 2a's filled 24px
  `$ command` bar, not 2b's quieter 22px tiled header. Is that right?
- **Result colours in `log`.** Successful results are `--fg`; only
  warn and fault use their colours. Should success be `--ok`?
- **Opening a `url`** (U8). The old shared inspector's *Open* button
  (`core/links.py::classify_link`, then the OS's opener) went with
  Monarch, and the props pane has no equivalent. Should `⏎` on a `url`
  row open it, and should it also be a command (`open --url @sel`)?
- **Unsaved layout on switch.** `1`–`9` quietly drops unsaved drift
  from the active layout. Should it ask first?

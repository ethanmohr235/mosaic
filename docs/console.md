# The console

This is the plan for the UI half of the Mosaic pivot. It replaces the
separate windows with one tiled, keyboard-first window, the **console**.
`roadmap.md` → *Surfaces* says what each surface is *for*. This doc says
how the one window that hosts them is built, and in what order.

Everything under *Decided* was agreed with the user on 2026-09-29. The
rules in `rules.md` still apply: every step below is a phase, so it goes
through plan mode, questions and approval before any code. Anything under
*Open questions* is undecided. Ask before you assume.

## Sources

- **Visual reference:** `docs/design/mosaic-console/`. Open
  `Design System Proposals.dc.html` in a browser (it needs `support.js`
  beside it). Follow **Turn 3 → 3c, 3e**, **Turn 2 → 2a, 2b**, and
  **Turn 1 → 1a** for Motion and Moxie details. Ignore 1b, 1c and 2c.
  The handoff `README.md` has the tokens, sizes and key bindings, and it
  is high-fidelity for type, color, spacing, density, shell structure
  and command-line behavior.
- **This doc wins** wherever the two disagree. The handoff was drawn
  against fake data and an older picture of the repo. The corrections
  are listed next.

### Corrections to the handoff

| Handoff says | Actually |
|---|---|
| Frontend in `ui/frontend/`, API in `ui/api.py` | Neither exists. Each app has `<app>/api.py` + `<app>/frontend/`, with shared widgets in `frontend_shared/` (symlinked). See *Architecture*. |
| Graph drawn with Cytoscape | Monarch's graph is a hand-written canvas (`monarch/frontend/graph.js`). Keep it, and apply the graph styling rules to it. |
| Apps: Observatory (`mo rph`), Motion, Moxie, Monastery, Mote Inspector, Launcher | See *App names* below. Observatory is Monarch (`mo narch`), and there is no `mo rph`. Mora, Monolith and Home are missing from the handoff. Momentum is being removed and is never ported. |
| Fixed mote-type colors: task, note, event, source | Mosaic privileges no type. Motes are colored by **species**, using the existing probed palette retuned into the design's band. See *Color*. |
| Motion *runs* list, `diff vs #0412`, Moxie `run #0414` | Saved run history and *commit tick* are cut (`roadmap.md`). Motion lists **scenarios**. A run is a curve you plot, not a record you diff. |
| `hil disconnected` hardware row | Not a feature. Ignore it. |
| The prompt is always `mo`; the first token is an app suffix or a command | The prompt is `$`. `mo` is the verb that opens an app (`mo tion`), drawn as the `[mo]` box once typed; a bare suffix is an ordinary word. The top bar keeps the `[mo]` box. |
| Selection is one mote id | Selection is an **ordered set with a primary**. See *Selection*. |
| Inspector is only the `mote:props` pane | The props pane plus a floating inspector on demand. See *Inspector*. |
| Google Fonts for Geist Mono | Vendored, like the current fonts. Nothing is fetched at runtime. |

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
  one ES module per view under `js/views/`. There is no build step. `frontend_shared/`'s symlink scheme goes away once every window
  is gone, because one window needs no symlinks.
- **Strangler migration.** Build the shell first, then port the apps
  one at a time. An app's old window (`run_<app>.py`, `<app>/app.py`,
  `<app>/frontend/`) keeps working until its app is ported. After that
  it is deleted, with no shim and no preset launcher.
- **New UI only in the shell.** Once the shell and view contract exist
  (U1), every new surface is built as a pane there, including the
  worksheet editor, Mora calc macros UI, Home tree and Monastery agenda.
  Old windows only get bug fixes. A roadmap phase that needs UI before
  U1 lands builds its `core/` part first and waits for the shell.

### Panes are commands

A pane is a **command string plus its rendered output**, and nothing
else.

- A view id is shorthand for a canonical command. For example,
  `mote:props` means `open @sel`, and `monolith:table` means `find`.
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
  header. The first token is one of three things:
  1. **`mo` and an app suffix**, which opens that app's default view
     (`mo tion`, `mo xie`). `mo` is the verb; once `mo ` is typed, the
     command line draws those two letters as the `[mo]` box, so the line
     reads as the app's lockup (`[mo]tion`). Decided 2026-09-30, replacing
     the handoff's always-visible `[mo]` prompt.
  2. a **verb** (`find`, `open`, `graph`, `plot`, `fit`, `new`, `link`,
     `set`, `layout save`, …).
  3. anything else, which falls through to the existing modeling
     statements in `core/command_bar.py` (`R1.x = 5 ohm`, `A -> B`,
     `R1 : Resistor`, `R1.x ?`, `delete R1`).

  Parsing lives in `core/commands.py`, never in JS, so the CLI and MCP
  can run the same commands.
- **Verbs win, quoting escapes.** Verbs (`core/reserved.py` →
  `RESERVED_VERBS`, `mo` among them) are reserved words; an app suffix
  is not, since it is only ever `mo`'s argument. A line whose
  first token is one of them is that word. A mote with a reserved name
  is still reachable as `"find"` (quoted) or `#id`, and creating one
  from the command line warns about exactly that. Each port step adds
  its own verbs to `RESERVED_VERBS`.
- **An unported app** opens its old window (`api_common.open_app`)
  until its port step gives it a default view (`CONSOLE_APPS` in
  `core/commands.py` is the single record of what's ported).
- **`cd <garden>`** switches the workspace's garden.
- `find` query syntax (`type:task due:<7d`) builds on `core/query.py`,
  which Monolith already uses. Don't write a second query language.
- Completion and history popups, inline ghost text, the grammar hint
  header and the per-context history follow the handoff exactly.
- `@sel` can be used anywhere a mote id is expected (see *Selection*).
- Errors show in `--fault` on the right of the command line, and in the
  pane header's summary when a pane's command failed.

### App names

An app is opened with the verb `mo` and its suffix, `mo tion`, which the
command line draws as the lockup `[mo]tion`; the top bar shows the
focused pane's app the same way.
These are user-facing names. Python packages keep their current names
(see *Rename*).

| Suffix | App | Views (proposed ids) | Notes |
|---|---|---|---|
| `mo nolith` | Monolith | `monolith:table`, `monolith:vars` | General table. Project variables pane per roadmap. |
| `mo te` | Inspector | `mote:props` | `open @sel` follows the selection; `open #id` is pinned. |
| `mo xie` | Moxie | `moxie:plot` (`plot …`), `moxie:fit` (`fit …`) | |
| `mo tion` | Motion | `motion:scenarios`, `motion:state`, `motion:rule` | Timeline sits under state. No runs list. |
| `mo ra` | Mora | `mora:doc` | Text-editing pane (see *Keyboard*). |
| `mo nastery` | Monastery | `monastery:agenda` | A table or grid pane on the `--line` grid. |
| `mo narch` | Monarch | `monarch:graph` | Frozen structure debugger. Graph only. |
| `mo saic` | Home + settings | `mosaic:home`, `mosaic:gardens` | Ownership tree, loose gardens, garden management. Replaces the Gardens window and the launcher. |
| — | shell | `log` | Command and error log. |

Worksheet, checks and V&V views are added to this table by the roadmap
phase that builds them.

### Workspace context

- **One garden per workspace.** The whole window is on one garden. The
  top-bar breadcrumb shows its ownership path (`/ college-fall-2026 /
  hw4`, per `roadmap.md` → *Structure*).
- Switching garden (from `mo saic`, the breadcrumb, or a command) swaps
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
  the window regains focus, as the old windows do, and by an explicit
  `refresh`.
- Re-running must be cheap enough for this. A view that's too slow to
  re-run gets a cache in its API namespace, not a stale-output mode.

### Keyboard and mouse

- **Pane-first.** Keys go to the focused pane, so tables can use
  `j`/`k` and single-key actions like `e` and `x`. `:` or `/` moves
  focus to the command line, and `esc` returns it to the pane.
- `^w` is the tiling prefix everywhere: `v`/`s` split, `h`/`j`/`k`/`l`
  focus, `<`/`>` resize, `o` only, `=` equalize, `x` swap. `1`–`9`
  switch layouts when no text field has focus.
- **Text-editing panes** (Mora, worksheet, inline cell edits) capture
  every key until `esc`, except the `^w` prefix.
- The **key-hint bar** always reflects the current context: the focused
  view's own keys, then the shell's. Each view declares its hints (see
  the contract).
- **Mouse parity.** Every action can also be done with the mouse:
  - click a header to edit its command
  - click a pane to focus it
  - drag a divider to resize
  - click a row or node to select it, with shift or ctrl to extend the
    selection
  - right-click for a context menu (`menu.js`)

  Keyboard focus still wins over hover.
- The existing `focus.js` rules (middle-click never pastes, click-away
  blurs) carry over unchanged. Undo stays global (`core/undo.py`,
  `undo.js`).

### Inspector

- **`mote:props` is a pane** that follows `@sel`'s primary. It is the
  main place a mote is edited, and it follows the handoff's props
  layout: title, key/value grid, `links N`, `→ relation target`.
- **Floating on demand.** `p` (settled in U3) pops the same editor
  as a floating panel over any layout that has no props pane, and it
  closes on `esc`.
- Pane and float are **one component**, so what you can do to a mote is
  the same in both, and the rule from `architecture.md` → *The
  inspector* still holds.
- The float follows the handoff's popup style: `--bg-raised`, 1px
  `--line-strong`, no shadow, no leader line.

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
  own plan. `moxie:fit` waits for it.
- **The `plot` grammar.** Names are garden-scope names (`bd.l`).
  ```
  plot bd.sigma bd.tau vs bd.l 4:24 --steps 200
  plot bd.sigma vs bd.l 4:24 --set bd.E=200e9 --scenario hot --log
  plot bd.sigma vs bd.l 4:24 by bd.w 1:3     # surface
  plot sigma vs l --across                   # correlate: one dot per mote with both keys
  plot "Load curve"      plot #id            # a saved plot, re-solved
  plot save "Load curve"                     # the only write (undoable)
  plot export csv|png
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
- **Its own pane, `moxie:fit`**: data dots, the fitted curve, and a
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
  comes back with Monarch's canvas graph in U8. Linking stays a command.
- **Grammar:**
  ```
  sim [--ticks N] [--scenario s] [--vs s2] [--solver newton] [‹find conditions›]   motion:state
  rule ‹rule›|@sel                                                                 motion:rule
  scenarios                                                                        motion:scenarios
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

### Visual system

- The tokens, type scale, spacing, 0px radius, inverted selection, and
  "no animation except cursor blink and playhead" rules all come from
  the handoff `README.md`. Put the tokens in one CSS file that every
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
  styling** to `frontend_shared/plot.js`.

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
  appear in the view picker and the completion popup.
- **`commands`**: the verbs this view answers, and a completer for
  their arguments (driven by `core/` data, not hard-coded lists).
- **`create(ctx)`** returns one instance per pane, so two panes can
  show the same view.
- **`mount(el, command)` / `unmount()`**: render into the pane body,
  and on unmount remove every listener, timer and animation frame.
  Leaks are bugs.
- **`run(command)`**: re-run in place (for `⏎`, a selection change or a
  data change). It returns the header summary (`16 motes`, `0.4s`,
  `1 clamped`) and an ok/warn/fault level.
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
| U1 | **Done.** **Shell.** Root API and shared connection; tokens and vendored font; top bar, workspace, command line, hint bar; split tree, `^w` keys, divider drag, view picker; layouts and history file; merged grammar in `core/` with app-switch completion; store; components module; `log` view. | Two panes, split and resized from keyboard and mouse. A layout survives a restart. |
| U2 | **Done.** **Monolith → `mo nolith`.** `find` table with multi-row selection, batch edits via `@sel`. Delete Monolith's window. | The contract holds for a real view, and selection is shared. |
| U3 | **Done.** **Props → `mo te`.** Props pane plus floating on demand. Replaces `inspector.js` for the shell. | Editing a mote works identically everywhere. |
| U4 | **Done.** **Moxie → `mo xie`.** The `plot` pane on the restyled `plot.js` (see *Settled for U4*), then `fit` and the follow-up fixes (*Settled for `fit`*), also done. | Commands carry view state (ranges, series). |
| U5 | **Motion → `mo tion`.** Scenarios, state table with timeline and playhead, rule pane. No runs list. | A live, ticking view inside the contract. |
| U6 | **Mora → `mo ra`.** Document pane as a text-editing pane. | Keyboard capture and `esc` work for editors. |
| U7 | **Monastery → `mo nastery`.** Agenda pane. Coordinate with roadmap phase 5, and build the agenda features here, not in the old window. | Date-shaped data in the grid style. |
| U8 | **Monarch → `mo narch`.** Canvas graph only, restyled. Remove annotations (`core/annotations.py`, the annotation frontends, their docs) and ship a migration that drops their stored data. | The frozen debugger costs no more upkeep. |
| U9 | **Home → `mo saic`.** Ownership tree, loose gardens, garden management and settings. Delete `gardens_app/`, `window_launcher.py`'s multi-window pieces and the `frontend_shared/` symlinks. | The window is the only surface. |

Momentum is removed by roadmap phase 1 and is never ported. A roadmap
phase that needs a new surface (worksheet, checks, V&V table) adds a
view after U1, and after U3 if it edits motes.

Each step's done-check is the same:

- The view works from both keyboard and mouse.
- It appears in the view picker and completion.
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

- Read this doc and the handoff `README.md` before any UI step. Open
  the design canvas to check spacing and states, but don't copy its
  code: it's a reference, not production.
- Don't add a color, font, size, radius, shadow or animation that isn't
  in the tokens. If a view needs one, raise it as a question.
- Don't build UI in an old window once U1 has landed.
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
  (view `monolith:table`, `mo nolith` = `find`) and `set
  @sel|#id|‹name› ‹key› ‹value›|--clear`, where `set`'s `@sel` is the
  whole selection and everywhere else `@sel` is the primary. U3 added
  `open @sel|#id|‹name›` (view `mote:props`, `mo te` = `open @sel`),
  `rename`, `unlink` and `template`, which also take the whole
  selection. Confirm
  those, and whether the old window's bulk link, relationship matrix,
  bulk delete and *save view* come back as commands (`link @sel …`,
  `delete @sel`, `view save ‹name›`) -- U2 did not port them.
- **Worksheet and command line.** Worksheet lines (`bd.x = 80 in`) are
  the same statements the command line accepts. Is the worksheet a
  pane that's just a persistent, editable list of them? Settle this in
  roadmap phase 2.
- **Size tokens added in U1.** The shell took these from the design
  canvas rather than the handoff README. Confirm or replace them:
  `--popup-mo` 36px, `--popup-app` 90px, `--picker-key` 150px,
  `--divider-hit` 3px (invisible grab margin), `--popup-rows` 12,
  `--prompt-gap` 6px, `--bar-gap` 8px, `--border` 1px. U4 added
  `--plot-rail` 240px (the plot pane's legend/knob column; the design
  draws 320px) and the series hues `--s5` (195°) and `--s6` (40°), which
  are not colour-vision-checked against the other four.
- **Plot keys.** The handoff's Moxie keys were `f x z t e ?`. `x` and `e`
  are shell keys (close pane, edit command), so the plot pane uses `t`
  to show/hide a series, `z`/`Z` to zoom, `y` for log y, `[ ]` to pick
  a series, `j k` / `- +` for knobs. `f` waits for `fit`; export is
  `plot export`. Confirm.
- **Pane header style.** Every header uses 2a's filled 24px
  `$ command` bar, not 2b's quieter 22px tiled header. Is that right?
- **Result colours in `log`.** Successful results are `--fg`; only
  warn and fault use their colours. Should success be `--ok`?
- **Unsaved layout on switch.** `1`–`9` quietly drops unsaved drift
  from the active layout. Should it ask first?

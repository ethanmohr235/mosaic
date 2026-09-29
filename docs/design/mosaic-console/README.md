# Handoff: Mosaic Console design system

## Overview
This is a redesign of the MonOSS app suite (repo `ethanmohr235/monoss`, frontend in `ui/frontend/`) into **Mosaic**: a dark, all-monospace, keyboard-first engineering workspace. It combines three explorations from the design file:

- **3c Prompt wordmark.** "mo" sits in an inverted box, and each app is named by the rest of a word: `mo rph`, `mo tion`, `mo xie`, `mo nastery`, `mo te`, `mo saic`. The same box is used as the logo and as the command-line prompt.
- **2a Command-first panes.** Every pane is the output of a command, and the pane's header shows that command. The command line is the main way to use the app, with inline completion and history.
- **2b Tiling workspace.** Views from any app tile into one workspace. Panes are split, focused and resized from the keyboard, selection is shared between panes, and layouts are saved by name.

Guiding rules: simple, utilitarian and dense. Use monochrome, and add color only where it carries meaning (mote type or status). Show hierarchy through brightness and weight, not size. There is no decoration.

## About the design files
The bundled `Design System Proposals.dc.html` is a **design reference built in HTML**. It shows the intended look and behavior and is not production code. Rebuild these designs inside the existing MonOSS frontend using its current patterns (currently vanilla JS in `ui/frontend/` with Cytoscape for the graph). If you move to a framework, pick one that fits the existing Python API (`ui/api.py`). The sections to follow are **Turn 3 → 3c and 3e**, **Turn 2 → 2a and 2b**, and **Turn 1 → 1a** for Motion and Moxie details. Ignore 1b, 1c and 2c.

Open the file directly in a browser (it needs `support.js` next to it). The sample data is fake.

## Fidelity
**High-fidelity** for type, color, spacing, density, shell structure and the command-line behavior. The graph and plot drawings are rough stand-ins: keep the styling rules described below, but draw them with the real renderers (Cytoscape and whatever plotting code is in use).

---

## Design tokens

Use the `oklch()` values directly; all modern browsers support them. The hex values are approximate equivalents for greys.

### Neutrals (no chroma)
| Token | oklch | ≈ hex | Use |
|---|---|---|---|
| `--bg` | `oklch(0.135 0 0)` | `#080808` | App background, pane background |
| `--bg-raised` | `oklch(0.16 0 0)` | `#0d0d0d` | Popups (completion, history, view picker) |
| `--bg-bar` | `oklch(0.18 0 0)` | `#121212` | Pane command headers, key-hint bar |
| `--track` | `oklch(0.22 0 0)` | `#1b1b1b` | Bar-chart tracks, plot grid, empty tile |
| `--line` | `oklch(0.30 0 0)` | `#2e2e2e` | All 1px borders and pane dividers |
| `--line-strong` | `oklch(0.42 0 0)` | `#4d4d4d` | Popup border |
| `--ghost` | `oklch(0.45 0 0)` | `#555555` | Inline completion ghost text |
| `--dim` | `oklch(0.58 0 0)` | `#7a7a7a` | Labels, keys in key/value rows, hints, counts |
| `--fg` | `oklch(0.88 0 0)` | `#d7d7d7` | Body text, values; selection background |
| `--bright` | `oklch(0.97 0 0)` | `#f5f5f5` | Titles, prompt box, key glyphs in hint bar, playhead |

**Selection is inverted**: background `--fg` with text `--bg`. Everything inside a selected row, including semantic-colored cells, switches to `--bg` so it stays readable.

### Semantic colors (all L 0.78–0.82, C 0.11–0.17)
| Token | oklch | Meaning |
|---|---|---|
| `--c-task` / `--ok` | `oklch(0.80 0.13 150)` | Task mote type; running/ok status |
| `--c-note` | `oklch(0.80 0.11 240)` | Note mote type; plot series 1 |
| `--c-event` / `--warn` | `oklch(0.82 0.12 85)` | Event mote type; warn/blocked status; plot series 2; "unsaved" |
| `--c-source` | `oklch(0.78 0.13 320)` | Source mote type |
| `--fault` | `oklch(0.70 0.17 25)` | Fault, clamp, overdue, error |
| `--idle` | = `--dim` | Idle status |

There are no other colors: no brand accent and no gradients. If a new mote type needs a color, keep L ≈ 0.8, C ≈ 0.12 and pick a new hue.

### Typography
- **One family:** `Geist Mono` (Google Fonts, weights 400/500/600). Fallback: `ui-monospace, "SF Mono", Menlo, monospace`.
- **Body:** 12px / 18px line-height, weight 400. Nearly everything uses this.
- **Title:** 13px / 600, color `--bright` (the selected mote's name in the props pane).
- **App name in the top bar:** 12px / 600, color `--bright`.
- **Emphasis:** weight 600 plus `--bright`. Never use larger sizes for emphasis inside the app.
- Always use tabular figures (monospace provides them). Numbers are right-aligned in tables.

### Spacing and shape
- **Radius: 0px everywhere.**
- Horizontal pane padding: **10px**. Vertical gap between groups inside a pane: **14–16px**.
- Row height = line-height (18px), with **no padding between rows**. Density comes from this.
- Key/value grid: `grid-template-columns: 84px 1fr` (use 104px when keys are relation names like "blocked by"; keys never wrap).
- Borders: `1px solid var(--line)`. Tiled panes are separated by a 1px gap over a `--line` background.
- No shadows, except that the focused tile gets `box-shadow: inset 0 0 0 1px var(--fg)`.

---

## Wordmark: 3c Prompt

- **Mark:** the text `mo` in an inverted box (background `--bright`, text `--bg`, weight 600), followed by `saic` in `--bright` with a gap of about 0.28em. At display size (64px) the box padding is `4px 10px 10px`.
- **Icon / favicon:** a square box in `--bright` with `mo` centered in `--bg`, weight 600. Text is about 42% of the box size.
- **App lockups:** the `mo` box followed by the app suffix: `[mo]rph`, `[mo]tion`, `[mo]xie`, `[mo]nastery`, `[mo]te`.
- **In the top bar:** the `mo` box is flush with the left edge, fills the full bar height (25px), and has 7px horizontal padding. The app suffix (`rph`) follows 5px later, in `--bright` 600, then the context breadcrumb in `--dim` (`/ atlas-migration`).
- **At the command line:** the same box, flush left and full height (25px), is the prompt. It is fixed text; the user types only what comes after it.

### App naming
| Old name | Command | Purpose |
|---|---|---|
| Observatory | `mo rph` | Graph of motes and relations |
| Motion | `mo tion` | Simulation: scenarios, ticks, rules |
| Moxie | `mo xie` | Plotting and fitting rule outputs |
| Monastery | `mo nastery` | Calendar of date-bound motes |
| Mote Inspector | `mo te` | Single-mote properties |
| Launcher / settings | `mo saic` | Suite settings (the launcher is replaced by the command line) |

---

## Shell layout (every app)

The screen is a vertical stack filling the viewport. Reference size 1080×640; everything flexes.

1. **Top bar**: height 26px, 1px bottom border.
   Left: `[mo]` box, app suffix, `/ context`. Right, in `--dim`: workspace summary (`3 panes · selection linked`), or the current layout name.
   In tiling mode this bar shows **layout tabs** instead: `1:triage  2:loop-review  3:resonance`. The active tab is inverted (padding `0 6px`) and inactive tabs are `--dim`. An unsaved layout shows `*` after its name and `unsaved layout` in `--warn` on the right.
2. **Workspace**: `flex:1`. A tiled grid of panes (see Tiling).
3. **Command line**: height 26px, 1px top border. The `[mo]` prompt box, then input text in `--fg`, then a block cursor (7×15px, `--fg`), then inline ghost completion in `--ghost`. Right-aligned in `--dim`: match count or context (`4 matches`, `12 results`, `diff vs #0412`).
4. **Key-hint bar**: height 22px, background `--bg-bar`, text `--dim`, gap 14px. Each hint is a key glyph in `--bright` followed by a label, e.g. `⏎ run  ⌥⏎ run in new pane  e edit pane command  x close pane  ↑ history`. The hints change with context (focused pane, open popup, or mode).

---

## Panes (from 2a)

A pane is **a command plus its rendered output**.

- **Pane header**: height 24px, background `--bg-bar`, padding `0 10px`, gap 6px. It shows `$` in `--dim`, then the command in `--fg` (ellipsized if too long), then a right-aligned summary in `--dim` (`8`, `16 motes`, `m-0192`, `0.4s`, or `1 clamped` in `--fault`).
  - Examples: `$ find type:task status:open due:<7d`, `$ graph @sel --depth 2`, `$ open @sel`, `$ plot sigma u238-6.67 u238-20.9 --range 4:24 --steps 200`, `$ fit bw --series all`, `$ rule show r-bw`.
- In tiling mode the header can be shortened to a view id (`motion:state`, `moxie:plot`, `mote:props`) with a context string on the right. **The focused pane's header is inverted** (background `--fg`, text `--bg`, name in weight 600) and the pane gets the inset 1px `--fg` outline.
- **Editing:** `e` on a focused pane, or a click on its header, loads its command into the command line. `⏎` re-runs it in place.
- **`@sel`** stands for the current selection. Panes whose command contains `@sel` update whenever the selection changes. This is how "linked selection" works.
- `⏎` runs a command in the focused pane, replacing its content. `⌥⏎` runs it in a new pane (auto-split). `x` closes the focused pane.

### Pane content types
- **Table** (for example the find results or motion state): a header row in `--dim` (padding-top 4px), then rows with no padding. Numeric columns are right-aligned, and the selected row is inverted. The status column uses semantic colors (`blocked` in `--warn`, `fault` in `--fault`, `running` in `--ok`).
  - Motion state columns: `mote | type | state | value | Δ/s | bar` (fractions 1.3 / 0.8 / 0.8 / 1 / 0.7 / 1, column-gap 10px). The bar is 6px tall: a `--track` background with a fill in the state color.
- **Props** (mote inspector): the name as a title (13/600, `--bright`), then a key/value grid (`type, status, due, owner, estimate, garden, created`), then `links N` with rows of `→ relation  target`.
- **Graph**: see Graph styling.
- **Plot**: a 1px `--line` left and bottom axis, and a `--track` grid (4 vertical and 3 horizontal lines). Series are drawn as 1.5px lines in `--c-note` and `--c-event`. A threshold or clamp is a dashed (4 4) line in `--fault` with a label at the top right (`clamp 900 mb`). Axis tick labels are `--dim`: y labels right-aligned in a 32px column, x labels spread evenly.
- **Rule / code**: a 1px `--line` box with padding `6px 8px`. Keywords (`when`, `and`, `then`, `emit`) are `--dim` and the rest is `--fg`, with whitespace preserved.
- **Timeline** (Motion): tick labels `0 … 3000` in `--dim`, rule-fire markers (2px wide, 12px tall, in the status color) on a baseline, and a 1px `--bright` playhead.

### Graph styling
- Nodes are **squares, not circles**. Unselected nodes have a `--bg` fill and a 1.3px stroke in their type color. The selected node is filled with its type color and has a 1px `--bright` outline 4px outside it.
- Node sizes: selected half-size 7, labeled 5, unlabeled 3.5 (in SVG units at a 600×440 viewBox; scale for Cytoscape).
- Edges from the selected node are 1.2px `--dim` solid. All other edges are 1px `--line`-ish (`oklch(0.34 0 0)`) and dashed `2 3`.
- Labels are 11px mono, `--dim`, placed to the right of the node. The selected node's label is `--bright`.
- Corner annotations in `--dim`: top-left `layout force · depth 2`, bottom-right `100% · 16/184 shown`.

---

## Command line behavior

### Grammar
- The prompt is always `mo`. The first token after it is either an **app suffix** or a **command** in the current app.
  - App switch: `mo rph`, `mo tion --scenario pump-p2-fault`, and so on. `⏎` opens the app in the current workspace (replacing the focused pane's view); `⌥⏎` opens it in a new pane.
  - Commands: `find <query>`, `graph <target> --depth N`, `open <mote>`, `link <a> <relation> <b>`, `new <type> "<name>" [--prop value]`, `step +N`, `plot …`, `fit …`, `rule show <id>`, `threshold set <value>`, `diff <run> <run>`, `layout save <name>`.
  - Query syntax: `key:value` filters with comparison operators (`due:<7d`, `status:open`, `type:task`).
- `@sel` can be used anywhere a mote id is expected.

### Completion popup
- It is anchored above the command line at its left edge, 460–560px wide, with background `--bg-raised` and a 1px `--line-strong` border.
- It has three parts, divided by 1px `--line` borders:
  1. A header in `--dim` with the grammar hint: `link schema-freeze ‹relation› ‹target›` or `mo ‹app›`.
  2. Rows in two columns (value 90–110px, then a description in `--dim`). The highlighted row is inverted, and its description turns to `oklch(0.30 0 0)`. Descriptions preview the effect, e.g. `schema-freeze → blocks → ‹target›`.
  3. A footer in `--dim` with keys: `tab complete · ⏎ run · ↑ history · @sel = schema-freeze`.
- The best match is also shown inline as ghost text after the cursor.
- **App-switch completion (3e):** typing `t` after `mo` lists `tion` (highlighted), then `te`. Non-matching apps stay visible in `--dim`, and the current app is labeled `· current`.

### History
- `↑` on an empty line opens the history popup (same style): rows of `HH:MM` in `--dim` (48px) followed by the command. The header reads `history · <context>` with a count on the right (`3 / 48`). Footer: `⏎ re-run · e edit · ⌥⏎ to new pane · type to filter`.
- `↑`/`↓` move through the list, and the command line previews the highlighted entry. `^r` searches the history.
- History is stored per context (garden or project).

---

## Tiling (from 2b)

- The workspace is a grid of panes with 1px gaps over `--line`. Any view from any app can go in any pane (a Morph graph beside a Motion state table beside a Moxie plot).
- **Keys** (vim/tmux style, prefix `^w`):
  - `^w v` split vertically, `^w s` split horizontally
  - `^w h/j/k/l` move focus
  - `^w < / >` resize (while resizing, a `◀ ─── 50% ─── ▶` guide appears on the divider, with the percentage in `--bright`)
  - `^w o` make the focused pane the only pane, `^w =` equalize, `^w x` swap
  - `1`–`9` switch saved layouts; `:layout save <name>` saves one
- **Splitting a pane** opens an empty pane (focused, header `new pane`) containing a `view>` prompt with a filter and a list of views (`observatory:graph`, `observatory:table`, `motion:state`, `moxie:plot`, `moxie:fit`, `mote:props`, `log`), each with a short description in `--dim`. `⏎` places the view and `esc` cancels the split.
- **Linked selection:** there is one global selection. Panes whose command includes `@sel`, or whose view "follows sel" (`mote:props`), update when it changes. The top bar shows `sel loop.b.temp · linked in 3 panes`.
- A layout stores the list of panes (command or view, plus split tree and sizes) and a name.

---

## Screens to implement
1. **mo rph** (Observatory): three panes: find-results table (290px) | graph (flex) | props (290px). See 2a Observatory and 3e.
2. **mo tion** (Motion): a scenarios/runs list (180px) | motion-state table with the timeline below it | rule and inspector (300px). The top bar shows `t=12.84s  tick 1284/3000 · dt 10ms · ▶ running` with `▶ running` in `--ok`. See 1a Motion; restyle its panes with 2a headers.
3. **mo xie** (Moxie): plot (flex) | fit results and bindings (320px). See 2a Moxie.
4. **Tiled workspace**: a 2×2 mix of the views above. See 2b.
5. **mo nastery** and **mo te**: not mocked. Apply the same shell and pane rules: the calendar becomes a table or grid pane using the `--line` grid; the inspector is the props pane at full width.

## State
- `workspace`: `{ layouts: {name → splitTree}, activeLayout, dirty }`
- `pane`: `{ id, command | view, result, focused }`
- `selection`: mote id (global)
- `commandLine`: `{ text, cursor, completions[], highlighted, popup: 'completion' | 'history' | 'view' | null }`
- `history`: array of `{ time, command, context }`, stored per context
- A per-app model (motes, relations, simulation tick, series) comes from the existing API (`ui/api.py`, `core/motes.py`, `core/relationships.py`).

## Interactions and motion
- No animation beyond instant state changes. Popups appear without transitions. The only exceptions are the cursor blink (1s step) and the Motion playhead moving while running.
- Hover: rows get a `--bg-bar` background. Keyboard focus always wins over hover.
- Errors: a message in `--fault` on the right side of the command line, and in the pane header summary if a pane's command failed.

## Assets
- Font: Geist Mono (Google Fonts).
- No icons. Use text glyphs only: `→ ▶ ⏎ ⌥ ↑ ◀ ▶ › $ @`.

## Files
- `Design System Proposals.dc.html`: the full design canvas (open it in a browser, with `support.js` alongside). The relevant sections are Turn 3 (3c, 3e), Turn 2 (2a, 2b) and Turn 1 (1a).

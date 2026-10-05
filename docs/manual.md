# Mosaic console — quick reference

`.venv/bin/python run_console.py [--garden NAME]`. One window, one garden,
every pane is a command. Detail lives in `docs/ui-setup.md`.

**Keep this current:** every new command, flag or key is added here in
the same change.

## Shell keys (a pane focused)

| Key | Does |
|---|---|
| `:` or `/` | Go to the command line |
| `e` / click a pane header | Load the pane's command into the command line |
| `x` | Close the pane |
| `p` | Props float for the selection (`p`/`esc` closes) |
| `1`–`9` | Switch to the layout in slot N |
| `^1`–`^9` | Save the layout into slot N (keeps the slot's name; an empty slot's layout is named `N`) |
| `^z`, `^⇧z` / `^y` | Undo, redo |
| `^w v` / `^w s` | Split side by side / stacked |
| `^w h j k l` | Focus the pane in that direction |
| `^w <` / `^w >` | Shrink / grow the pane |
| `^w o`, `^w =`, `^w x` | Only this pane, equalize, swap |

Mouse: click a pane to focus it; drag a divider to resize; drag the focused pane's header onto another pane to dock it at that edge (outer quarter) or swap the two (middle).
Tables: drag the right edge of a column header to resize that column; double-click the edge to reset. Widths last until the console closes.

## Command line

| Key | Does |
|---|---|
| `⏎` | Run in the focused pane (a typed `delete` asks for a second `⏎`) |
| `^⏎` | Run in a new pane below |
| `⇧⏎` | Run in a new pane to the right |
| `tab` | Take the completion |
| `↑` `↓` | Move through completions; `↑` on an empty line opens history |
| `^r` | Search history |
| `esc` | Close the popup, then back to the pane (closes a new, empty pane) |

## Commands

`@sel` = the selected mote (for `set`, `rename`, `unlink`: the whole
selection). A mote named like a command is reached as `"find"` or `#id`.

| Command | Does |
|---|---|
| `layout ‹name›` | Open a layout in the focused pane's place. Built in: `calc` (`scenarios \| sim \| rule @sel`), `docs` (`notes \| edit @sel \| read @sel`), `plan` (`agenda \| week`), `graph` (`find \| graph @sel --depth 2 \| open @sel`), `gardens` (`gardens \| garden`); a saved layout of the same name wins |
| `find ‹conditions› [--sort k] [--group k] [--cols a,b] [--view name]` | Table of motes |
| `open @sel\|#id\|name` | A mote's properties and links (`@sel` follows the selection) |
| `set ‹target› ‹key› ‹value›` / `… --clear` | Set / remove a property (`5 mm` records the unit); `set C1.voltage 30` also works |
| `set --scenario ‹s› ‹m.k› ‹value›` / `… --clear` | Write / clear a scenario's override (makes the scenario if new); the garden is untouched |
| `rename ‹target› ‹old› ‹new›` | Rename a property key |
| `unlink ‹target› ‹type› ‹other›` / `unlink ‹target› #rel` | Remove a relationship |
| `template ‹mote› ‹species›` | Make a mote its species' template |
| `plot ‹m.y›… vs ‹m.x› [lo:hi]` | Plot, re-solving at each point (nothing written) |
| `plot ‹m.z› vs ‹m.x› lo:hi by ‹m.y› lo:hi` | Surface |
| `plot ‹key› vs ‹key› --across` | One dot per mote with both keys |
| `plot "‹saved›"` / `plot #id` | Open a saved plot |
| `plot save ‹name›` | Save the focused plot as a mote |
| `plot … --data ‹ykey› vs ‹xkey› [--from ‹import›]` | Measured points over the curve (error bars from their `uncertainty`; drawn by the publication export) |
| `plot export csv\|png\|svg\|pdf\|tex [path]` | Export the focused plot. `svg`/`pdf`/`tex` are publication figures: unit-labelled axes, log axes, a ± band where a given has `uncertainty`, the `--data` points. `pdf` needs tectonic (see `export pdf`) |
| `fit ‹m.y› vs ‹m.x› --data ‹Y› vs ‹X› --free ‹m.k›…` | Fit inputs to measured points (every mote with `Y` and `X`); shows, never writes |
| `fit … --apply` | Write the fitted values, their σ and `source: fitted` (undoable) |
| `sim [--ticks N] [--scenario s] [--vs s\|garden] [--solver name] [conditions]` | Run N ticks (default 50) in memory and watch the values; conditions pick motes, as `find` |
| `rule @sel\|‹rule›` | A rule's equation and bound values (a non-rule mote: every rule touching it) |
| `why ‹m.k›` | How the last written solve got a value |
| `scenarios` | Scenarios and their overrides |
| `solve` | Solve once and write it (undoable; refused if a rule uses `prev()`) |
| `solve --live on\|off` | Solve after every edit made here (top bar: `live`) |
| `step +N\|-N\|‹tick›`, `play`, `pause` | Move the shared playhead (top bar: `tick 12/49 ▶`) |
| `edit @sel\|#id\|‹name›` | Write a note (a new name makes it on the first save) |
| `read @sel\|#id\|‹name›` | A note rendered, following unsaved typing |
| `notes [text]` | Every note, or those containing the text |
| `worksheet` | The garden's calculation as text; `esc` applies it (one undo step) |
| `agenda [from] [to]` | Dated motes, a row per occurrence by day (default: today + 14 days) |
| `week [date]`, `month [date]` | Hour grid; six-week grid |
| `graph [@sel\|#id\|‹name›] [--depth N]` | A mote's neighbourhood (default depth 2; `@sel` re-roots with the selection); no root: the whole garden |
| `graph … --hide-rel a,b`, `--view ‹saved›`, `‹conditions›` | Leave out relationship types (not hopped either); keep what a saved view / conditions match |
| `detach #id ‹key› ‹occurrence› ‹start› ‹end›` | Move one occurrence out of its series (what `t` runs) |
| `cd ‹garden›` | Switch garden (or click a crumb in the top bar's path) |
| `gardens [--archived]` | Every garden: the ownership tree, loose, archived (folded) |
| `garden [‹name›]` | A garden's details (none: the open one) |
| `garden new ‹name›`, `garden rename ‹old› ‹new›` | Make / rename a garden (renaming the open one follows it) |
| `garden rm ‹name›` / `garden restore ‹name›` | Move aside (a typed one asks for a second ⏎; not the open one) / bring back |
| `garden archive\|unarchive ‹name›` | Archive with everything it owns / undo that |
| `garden export ‹name› [path]`, `garden import [path] ‹new›` | JSON out / into a new garden (no path: a file dialog) |
| `garden sync ‹name› ‹url›\|off\|now\|auto on\|off` | Sync server on / off, sync once, automatic syncs |
| `image add ‹path› [‹name›]`, `image rename ‹old› ‹new›` | Store a PNG/SVG for notes (`\image{name}`; auto-named `shot-NNN`), or rename one and every note that shows it (undoable) |
| `export tex\|pdf [‹path›] [--note ‹name›]` | Write the project's document as `.tex` (+ `images/`) or a PDF: a draft, nothing frozen |
| `submit [--force] [--note ‹name›]` | Freeze the document as the next revision `r1`, `r2`…; the owner becomes `submitted` (undoable; refused if a check fails or a value is unresolved) |
| `revisions`, `revisions diff ‹a› ‹b›`, `revisions export ‹rN› [‹path›] [--tex]` | List frozen revisions / values that changed / write one's PDF (or `.tex`) |
| `project start @sel [‹garden›]`, `project stop @sel` | Give a mote its own garden / drop its claim (undoable) |
| `layout save\|load\|rm ‹name›`, `layout list` | Pane layouts (a new one takes the lowest free slot 1–9) |
| `refresh` | Re-read the garden (MCP/CLI/sync edits) |
| `undo`, `redo` | Undo / redo the last write |
| `log` | Command and error log |
| anything else | Statement: `R1.x = 5 ohm`, `A -> B`, `A -> type -> B`, `R1 : Resistor`, `R1.x ?`, `delete R1` |

Calendar flags: `--keys a,b` · `--type ‹species›` · find conditions. Dates:
ISO, `today`, `7d`, `-2w`.

Fit flags: `--from ‹import›` (one CSV import's rows) · `--scenario name`.

Plot flags: `--steps N` · `--set m.k=v` (knob, never written) ·
`--scenario name` · `--log` · `--hide m.k`.

Find conditions: `status:todo` is · `name~truss` contains ·
`due:>2026-10-01`, `due:<7d` after/before (`7d`, `2w`, `today`) ·
`status!:done` is not · `due:?` / `due!:?` empty / not empty ·
`type:task` species · `->depends_on:…` / `<-owns:…` relationship ·
`a:1 or b:2` OR · `--sort -due` descending.

## Pane keys

Across panes a key means one thing:

| Key | Means |
|---|---|
| `j` `k` / `h` `l` | Move down/up, left/right |
| `g` `G` | Top / end |
| `[` `]` | Step the pane's own dimension (depth, series, page, ten ticks) |
| `-` `+` | Less / more |
| `⏎` | Act on the row (select; `cd` in gardens; run under in scenarios) |
| `o` | Open / follow in a new pane |
| `i` | Edit |
| `n` | New |
| `r` | Rename |
| `a` | Add |
| `v` | Compare |
| `d` `d` | Delete |
| `space` | Toggle (play in the sim panes) |
| `esc` | Back / clear |

**Table** (`find`)

| Key | Does |
|---|---|
| `j` `k` / `h` `l`, `g` `G` | Row / column, first / last row |
| `space`, `V`, `^a`, `esc` | Toggle row, select range, select all, clear |
| `s` / header click | Sort by column (again: reverse) |
| `S` | Group by column (again: ungroup) |
| `i` `⏎` / double-click | Edit cell (`⏎` save, empty clears, `esc` cancel) |

Click selects, shift-click a range, ctrl-click toggles.

**Props** (`open`, and the `p` float)

| Key | Does |
|---|---|
| `j` `k`, `g` `G` | Move |
| `i` / double-click | Edit value (empty clears) |
| `⏎` | Edit; follow a link; flip a boolean; add a hint |
| `a` | Add property: `key value` |
| `r` | Rename key |
| `d` `d` | Delete property or link |
| `l` | Link: `type target` |

**Plot** (`plot`)

| Key | Does |
|---|---|
| `h` `l` / `H` `L` / click | Cursor one point / ten; status line reads the values |
| `z` then `z` / drag | Zoom to the marked range |
| `Z` / double-click | Zoom out ×2 |
| `y` | Log y |
| `[` `]`, `space` / legend click | Pick series, show/hide it |
| `j` `k`, `-` `+` / drag knob bar | Pick knob, move it 1/20 of its range |
| `R` | Show/hide the side column (hidden automatically in a narrow pane; knobs then show in the status line) |
| `esc` | Clear cursor |

**Fit** (`fit`)

| Key | Does |
|---|---|
| `A` / click *apply* | Apply the fit (runs the line with `--apply`) |
| `h` `l` / click | Step through the points; status line shows point vs fit |
| `esc` | Clear cursor |

**Sim** (`sim`, `rule`, `why`, `scenarios`; `layout calc`) — every one of these panes

| Key | Does |
|---|---|
| `space` | Play / pause the playhead |
| `,` `.` | One tick back / on |
| `[` `]` | Ten ticks back / on |

Sim pane: `j` `k` `g` `G` row (the timeline draws it) · `⏎` / click
select its mote · `v` toggle `--vs garden` · click/drag the timeline to
scrub.

Rule pane: `j` `k` binding · `⏎` select its mote · `w` why (`esc`
back) · `i` edit the equation (prefills `set`).

Scenarios pane: `j` `k` move · `⏎` run sim under it · `v` compare sim
against it · `a` / `n` add override / new scenario (prefills `set
--scenario`) · `d` `d` clear the override.

**Editors** (`edit`, `worksheet`)

| Key | Does |
|---|---|
| `i` / `⏎` | Start typing (then every key but `^w` is the editor's) |
| `esc` | Stop: a note saves (one undo step per session), the worksheet applies |
| `^f` | Find / replace: `⏎` `⇧⏎` next/prev · `tab` field · `⏎` in replace: one · `^⏎` all · `esc` close |
| `^z` | Undo typing (the text box's own) |
| `^v` | In a note: paste a clipboard image as `\image{shot-NNN}` on its own line (one undo step; rename it with `image rename`) |
| `j` `k`, `g` `G` | Scroll (not typing) |

Read pane: `j` `k` `g` `G` `space` scroll · click a link to select its mote.
Notes pane: `j` `k` move · `⏎` / click select (the `@sel` panes follow) ·
`n` new note (prefills `edit `).

**Calendar** (`agenda`, `week`, `month`; `layout plan`)

| Key | Does |
|---|---|
| `[` `]`, `t` | Page back / on, today |
| `j` `k` | Next / previous event |
| `⏎` / click | Select its mote |
| `o` | Follow: note → `read`, project → `cd`, else `open` |
| `J` `K`, `H` `L` | Move ± an hour (all-day: a day), ± a day |
| `<` `>` | Shorter / longer |
| `n` | New event (prefills `‹name›.date = ‹slot›`) |
| `d` `d` | Delete (a recurring one asks `t` / `s`) |
| `b`, `B` | Hide this event's key, show every key |
| recurring: `t`, `s` | This occurrence (`detach`), the series |

Week grid: drag to move, drag the bottom edge to resize, click a slot
for `n`, double-click to create. Month: `+N more` opens that week.

**Graph** (`graph`; `layout graph`)

| Key | Does |
|---|---|
| `h` `j` `k` `l`, arrows | Walk to the neighbour that way |
| `⏎` / click | Select |
| `o` / double-click | Open props in a new pane |
| `[` `]` | Depth −1 / +1 |
| `f`, `+` `-`, `0` | Fit, zoom, 100% at the cursor |
| `u`, `U` | Unpin this node, every node |
| `n` | New linked mote (prefills `‹cursor› -> related_to -> `) |
| `d` `d` | Delete the cursor, or `unlink` a clicked edge |
| `esc` | Clear the edge / the armed delete |

Mouse: drag a node to move and pin it · drag empty space to pan · wheel
zooms · ctrl+drag node → node prefills `A ->  -> B`.

**Gardens** (`gardens`; `layout gardens`)

| Key | Does |
|---|---|
| `j` `k` / click | Move; the `garden` pane follows |
| `⏎` | `cd` there |
| `n`, `r` | New, rename (prefills `garden …`) |
| `d` `d` | Remove (`garden rm`; `garden restore` brings it back) |
| `z` | Archive / unarchive |
| `s`, `E`, `I` | Sync (prefills), export (file dialog), import (prefills) |
| `Z` | Show / fold archived |

`garden` pane: `⏎` or a click on a garden named there `cd`s.

**Log**: `j` `k` scroll, `g` `G` top/bottom, click a mote to select it.
**New pane**: the command line takes the caret; `⏎` runs into the pane, `esc` closes it.

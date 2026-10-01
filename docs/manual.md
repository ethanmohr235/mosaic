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
| `1`–`9` | Switch to saved layout N |
| `^z`, `^⇧z` / `^y` | Undo, redo |
| `^w v` / `^w s` | Split side by side / stacked |
| `^w h j k l` | Focus the pane in that direction |
| `^w <` / `^w >` | Shrink / grow the pane |
| `^w o`, `^w =`, `^w x` | Only this pane, equalize, swap |

Mouse: click a pane to focus it; drag a divider to resize.

## Command line

| Key | Does |
|---|---|
| `⏎` | Run in the focused pane (a second `⏎` confirms a delete) |
| `⌥⏎` | Run in a new pane |
| `tab` | Take the completion |
| `↑` `↓` | Move through completions; `↑` on an empty line opens history |
| `^r` | Search history |
| `esc` | Close the popup, then back to the pane |

## Commands

`@sel` = the selected mote (for `set`, `rename`, `unlink`: the whole
selection). A mote named like a command is reached as `"find"` or `#id`.

| Command | Does |
|---|---|
| `mo ‹app›` | Open an app: `mo nolith`, `mo te`, `mo xie`, `mo tion` (three panes: `scenarios \| sim \| rule @sel`), `mo ra` (`notes \| edit @sel \| read @sel`), `mo nastery` (`agenda \| week`), `mo narch` (`find \| graph @sel --depth 2 \| open @sel`); `mo saic` still opens the Gardens window |
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
| `plot export csv\|png [path]` | Export the focused plot |
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
| `cd ‹garden›` | Switch garden |
| `layout save\|load\|rm ‹name›`, `layout list` | Pane layouts |
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

**Table** (`find`, `mo nolith`)

| Key | Does |
|---|---|
| `j` `k` / `h` `l` | Row / column |
| `space`, `V`, `a`, `esc` | Toggle row, select range, select all, clear |
| `s` / header click | Sort by column (again: reverse) |
| `g` | Group by column (again: ungroup) |
| `i` `⏎` / double-click | Edit cell (`⏎` save, empty clears, `esc` cancel) |

Click selects, shift-click a range, ctrl-click toggles.

**Props** (`open`, `mo te`, and the `p` float)

| Key | Does |
|---|---|
| `j` `k`, `g` `G` | Move |
| `i` / double-click | Edit value (empty clears) |
| `⏎` | Edit; follow a link; flip a boolean; add a hint |
| `a` | Add property: `key value` |
| `r` | Rename key |
| `d` `d` | Delete property or link |
| `l` | Link: `type target` |

**Plot** (`plot`, `mo xie`)

| Key | Does |
|---|---|
| `h` `l` / `H` `L` / click | Cursor one point / ten; status line reads the values |
| `z` then `z` / drag | Zoom to the marked range |
| `Z` / double-click | Zoom out ×2 |
| `y` | Log y |
| `[` `]`, `t` / legend click | Pick series, show/hide it |
| `j` `k`, `-` `+` / drag knob bar | Pick knob, move it 1/20 of its range |
| `r` | Show/hide the side column (hidden automatically in a narrow pane; knobs then show in the status line) |
| `esc` | Clear cursor |

**Fit** (`fit`)

| Key | Does |
|---|---|
| `a` / click *apply* | Apply the fit (runs the line with `--apply`) |
| `h` `l` / click | Step through the points; status line shows point vs fit |
| `esc` | Clear cursor |

**Motion** (`sim`, `rule`, `why`, `scenarios`, `mo tion`) — every Motion pane

| Key | Does |
|---|---|
| `space` | Play / pause the playhead |
| `,` `.` | One tick back / on |
| `[` `]` | Ten ticks back / on |

Sim pane: `j` `k` `g` `G` row (the timeline draws it) · `⏎` / click
select its mote · `d` toggle `--vs garden` · click/drag the timeline to
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
| `j` `k`, `g` `G` | Scroll (not typing) |

Read pane: `j` `k` `g` `G` `space` scroll · click a link to select its mote.
Notes pane: `j` `k` move · `⏎` / click select (the `@sel` panes follow) ·
`n` new note (prefills `edit `).

**Calendar** (`agenda`, `week`, `month`, `mo nastery`)

| Key | Does |
|---|---|
| `[` `]`, `t` | Page back / on, today |
| `j` `k` | Next / previous event |
| `⏎` / click | Select its mote |
| `o` | Follow: note → `read`, project → `cd`, else `open` |
| `J` `K`, `H` `L` | Move ± an hour (all-day: a day), ± a day |
| `<` `>` | Shorter / longer |
| `n` | New event (prefills `‹name›.date = ‹slot›`) |
| `d` `d` | Delete (prefills `delete #id`) |
| `b`, `B` | Hide this event's key, show every key |
| recurring: `t`, `s` | This occurrence (`detach`), the series |

Week grid: drag to move, drag the bottom edge to resize, click a slot
for `n`, double-click to create. Month: `+N more` opens that week.

**Graph** (`graph`, `mo narch`)

| Key | Does |
|---|---|
| `h` `j` `k` `l`, arrows | Walk to the neighbour that way |
| `⏎` / click | Select |
| `o` / double-click | Open props in a new pane |
| `[` `]` | Depth −1 / +1 |
| `f`, `+` `-`, `0` | Fit, zoom, 100% at the cursor |
| `u`, `U` | Unpin this node, every node |
| `n` | New linked mote (prefills `‹cursor› -> related_to -> `) |
| `d` `d` | Delete (prefills `delete ‹cursor›`, or `unlink …` on a clicked edge) |
| `esc` | Clear the edge / the armed delete |

Mouse: drag a node to move and pin it · drag empty space to pan · wheel
zooms · ctrl+drag node → node prefills `A ->  -> B`.

**Log**: `j` `k` scroll, `g` `G` top/bottom, click a mote to select it.
**New pane** (`view>`): `↑` `↓` choose, `⏎` place, `esc` cancel.

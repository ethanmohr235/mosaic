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
| `mo ‹app›` | Open an app: `mo nolith`, `mo te`, `mo xie`; `mo tion`, `mo ra`, `mo nastery`, `mo narch`, `mo saic` still open old windows |
| `find ‹conditions› [--sort k] [--group k] [--cols a,b] [--view name]` | Table of motes |
| `open @sel\|#id\|name` | A mote's properties and links (`@sel` follows the selection) |
| `set ‹target› ‹key› ‹value›` / `… --clear` | Set / remove a property (`5 mm` records the unit) |
| `rename ‹target› ‹old› ‹new›` | Rename a property key |
| `unlink ‹target› ‹type› ‹other›` / `unlink ‹target› #rel` | Remove a relationship |
| `template ‹mote› ‹species›` | Make a mote its species' template |
| `plot ‹m.y›… vs ‹m.x› [lo:hi]` | Plot, re-solving at each point (nothing written) |
| `plot ‹m.z› vs ‹m.x› lo:hi by ‹m.y› lo:hi` | Surface |
| `plot ‹key› vs ‹key› --across` | One dot per mote with both keys |
| `plot "‹saved›"` / `plot #id` | Open a saved plot |
| `plot save ‹name›` | Save the focused plot as a mote |
| `plot export csv\|png [path]` | Export the focused plot |
| `cd ‹garden›` | Switch garden |
| `layout save\|load\|rm ‹name›`, `layout list` | Pane layouts |
| `refresh` | Re-read the garden (MCP/CLI/sync edits) |
| `undo`, `redo` | Undo / redo the last write |
| `log` | Command and error log |
| anything else | Statement: `R1.x = 5 ohm`, `A -> B`, `A -> type -> B`, `R1 : Resistor`, `R1.x ?`, `delete R1` |

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

**Log**: `j` `k` scroll, `g` `G` top/bottom, click a mote to select it.
**New pane** (`view>`): `↑` `↓` choose, `⏎` place, `esc` cancel.

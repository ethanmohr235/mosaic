# Running the desktop UI

One tiled window, **the console** (`run_console.py`, window title
*Mosaic*, via `pywebview`: a local HTML/CSS/JS frontend, no browser tab,
no local web server). Every app has moved into it -- Monolith, the
table; Moxie, the plots; Motion, the simulator; Mora, the notes;
Monastery, the calendar; Monarch, the graph; and Home, where the
gardens themselves are made, renamed, removed, imported, exported and
synced -- and their old windows are deleted (`docs/console.md`, steps
U1–U9).

---

# The console -- one tiled window

```sh
.venv/bin/python run_console.py --garden FEA-Homework-4
```

One window, one garden at a time, every pane the output of a command.
Its design, grammar and view contract are in `docs/console.md`; every
command and key on one page is `docs/manual.md`. This
section covers what it does today (steps U1–U4): the shell, the `log`
view, the `find` table, the props pane and plots.

### The shell

- **Top bar:** the `[mo]` box, the focused pane's app, and `/ garden`.
  On the right, saved layouts as tabs (`1:two`) with `*` and
  *unsaved layout* once the panes drift from the active one, then the
  selection (`sel bd +2 · linked in N panes`) or, with nothing selected,
  the pane count.
- **Command line** (`$` prompt; a typed `mo ` shows as the `[mo]` box): `:` or `/` focuses it and `esc`
  leaves. `⏎` runs in the focused pane, `^⏎` in a new pane below and `⇧⏎` in one to the right. `tab`
  completes, and the best match shows as ghost text. `↑` on an empty
  line opens this garden's history (`^r` searches it). Errors show in
  red on the right. A delete asks for a second `⏎`.
- **Key-hint bar:** always shows the focused view's keys, then the
  shell's.

### What you can type

| Line | Does |
|---|---|
| `mo tion`, `mo ra`, `mo xie`, `mo nolith`, … | Open that app (`mo` then `tab` lists them). `mo tion` and `mo ra` open three panes in the focused one's place. Unported apps open their old window. |
| `find <conditions> [--sort k] [--group k] [--cols a,b]` | The table (below). `mo nolith` is `find` with no conditions. |
| `open @sel\|#id\|<name>` | A mote's properties and links (below). `mo te` is `open @sel`. |
| `rename <target> <old> <new>` | Rename a property, keeping its value and unit. Refuses to overwrite a key that exists. |
| `unlink <target> <type> <other>` or `unlink <target> #<rel>` | Remove a relationship (either direction). |
| `template <mote> <species>` | Make a mote the one its species' new instances start from. |
| `plot <m.y>… vs <m.x> [lo:hi] …` | A plot, re-solving the garden at each point (below). `mo xie` is `plot` with nothing to plot yet. |
| `plot save <name>`, `plot export csv\|png [path]` | Save the focused plot as a mote (the only write, undoable), or export it. |
| `fit <m.y> vs <m.x> --data <Y> vs <X> --free <m.k>…` | Fit inputs to measured points (below). `--apply` writes the result. |
| `edit @sel\|<note>`, `read @sel\|<note>`, `notes [text]` | Write a note, read it rendered, list the notes (below). |
| `worksheet` | The garden's calculation as lines of text; `esc` applies it (below). |
| `sim [--ticks N] [--scenario s] [--vs s\|garden] [<conditions>]` | Run the garden tick by tick and watch its values (below). Writes nothing. |
| `rule @sel\|<rule>`, `why <m.k>`, `scenarios` | A rule and what it binds; how the last solve got a value; the scenarios and their overrides (below). |
| `solve`, `solve --live on\|off` | Solve once and write it (one undo step); or solve after every edit made here. |
| `step +N\|-N\|<tick>`, `play`, `pause` | Move the shared playhead. |
| `set @sel\|#id\|<name> <key> <value>` | Set a property. `@sel` is the **whole** selection, as one undo step. `--clear` in place of the value removes the key. The value reads like a statement's, so `5 mm` records the unit. `set C1.voltage 30` is the same as `set C1 voltage 30`; `set --scenario hot C1.voltage 30` writes the scenario's override instead (making the scenario if there is none by that name). |
| `log` | The command and error log for this session. |
| `cd <garden>` | Switch the workspace to another garden, with its own layouts and history. |
| `layout save\|load\|list\|rm <name>` | Save or restore the pane layout. |
| `refresh` | Re-read the garden (picks up MCP, CLI and sync edits). |
| `undo`, `redo` | Same as Ctrl+Z / Ctrl+Shift+Z. Every write undoes as one step, a typed statement included. |
| anything else | A modeling statement, as in the old `/` bar: `R1.x = 5 ohm`, `A -> B`, `R1 : Resistor`, `R1.x ?`, `delete R1`. |

`@sel` stands for the selected mote (the primary) anywhere a mote is
expected, except after `set`, where it is the whole selection. A mote
named like a verb (`log`, `find`) is reached as `"log"` or `#id`.

### Props (`open`, `mo te`, and `p`)

One mote: its name as the title with its species, a key/value grid with
a typed value per property (a number shows its unit, a reference is a
link, an enum shows its choices while you edit), the keys its species'
template says it usually has, then `links N` with a `→ type  target`
row per relationship (`←` for one pointing in; `instance_of` is how a
species shows). `open @sel` follows the selection; `open #id` stays on
one mote, and walking a link from it re-points the pane.

| Key | Does |
|---|---|
| `j` `k`, `g` `G` | Move the cursor |
| `i` / double-click | Edit the value (empty clears it) |
| `⏎` | Edit; on a link or reference, walk there; on a boolean, flip it; on a hint, add it |
| `a` | Add a property: `key value` |
| `r` | Rename the key |
| `d` `d` | Delete the property or link (the first `d` arms it) |
| `l` | Link: `type target` |

Every edit is a command (`set`, `rename`, `unlink`, `A -> type -> B`,
`template`) run through the command line, so it is in the history, the
log and undo. With several motes selected the pane shows the first and
says so; `set @sel …` writes them all.

**`p`** opens the same view floating over a layout that has no props
pane (with one, it focuses that pane). It follows the selection, has
the keys while open, and `p` or `esc` closes it.

Not in the console yet: the calendar picker for a date (dates edit as
`2026-10-02` text until U7). A note is written with `edit ‹note›` and a
solved value explained with `why ‹m.k›`.

### The table (`find`, `mo nolith`)

Every mote the line asks for, one row each. The query language is
`core/findtext.py` over `core/query.py`:

| Write | Means |
|---|---|
| `status:todo` | is (includes, on a list column) |
| `name~truss` | contains |
| `due:>2026-10-01`, `due:<7d` | after / before (greater / less on numbers); `7d`, `2w`, `today` are relative |
| `status!:done` | is not |
| `due:?`, `due!:?` | is empty / is not empty |
| `type:task` | an instance of the species named `task` |
| `->depends_on:…`, `<-owns:…` | a relationship, out of or into the mote |
| `a:1 or b:2` | OR instead of AND |
| `--sort due`, `--sort -due` | ascending / descending |
| `--group status` | rows under a heading per value |
| `--cols name,type,status` | these columns (default: the garden's commonest) |
| `--view <name>` | start from a saved view's query |

The whole line is the pane's command, so a sorted, grouped table
survives a layout save and a restart.

| Key | Does |
|---|---|
| `j` `k` / `h` `l` | Move the cursor by row / column |
| `space` | Toggle the row in the selection |
| `V` | Select from the anchor to the cursor |
| `a` | Select every row |
| `esc` | Clear the selection |
| `s` / a header click | Sort by the cursor's column; again to reverse |
| `g` | Group by the cursor's column; again to ungroup |
| `i` / `⏎` / double-click | Edit the cell |

Click selects a row, shift-click a range, ctrl-click toggles one. A
group heading's *select all* selects its rows. Editing a cell in a
selected row runs `set @sel …` and writes every selected mote; in an
unselected row it runs `set #id …` and writes that one. Either way it is
a command: in the history, the log and undo. An empty value clears the
key.

### Plots (`plot`, `mo xie`)

A value against another, with the whole garden re-solved at every point
of the range, in memory. **Nothing is written** -- not a value, not a
run -- which is what makes it safe to redraw on every drag of a knob.
Either side can be any numeric property of any mote, given or solved;
sweeping one the solver would determine is allowed, and the pane header
says **x pinned**, because the answer is then "what if this were so".

| Line | Draws |
|---|---|
| `plot bd.sigma bd.tau vs bd.l 4:24` | Both against `bd.l` from 4 to 24. Without `lo:hi` the range is half to double the current value. |
| `… --steps 200` | Points across the range (default 200; 40 per axis for a surface). |
| `… --set bd.E=200e9` | A **knob**: that value for every point, never written. |
| `… --scenario hot` | Solve under a garden scenario (`core/scenarios.py`). |
| `… --log`, `… --hide bd.tau` | Log y; keep a series in the legend without drawing it. |
| `plot bd.sigma vs bd.l 4:24 by bd.w 1:3` | A **surface**: one value over two axes, dark to light. |
| `plot sigma vs l --across` | A **correlation**: one dot per mote carrying both keys, after one solve. |
| `plot "Load curve"`, `plot #id` | A saved plot, re-solved as the garden stands now. |

The line is the pane's command, so every gesture rewrites it (a drag,
a zoom, a knob move), and a layout restores exactly the plot you left.
A plot pane draws a coarse pass first and then the full one. It reads the
garden under the session lock and solves outside it, so a long sweep
never holds up another pane, and a newer request from the same pane
cancels the older one between points.

| Key | Does |
|---|---|
| `h` `l`, `H` `L` / click | Move the cursor one point / ten; the status line reads every series there |
| `z` then `z` / drag across the plot | Zoom to the range between the two ends |
| `Z` / double-click | Zoom out twice as wide |
| `y` | Log y |
| `[` `]`, `t` / a legend click | Pick a series; show or hide it |
| `j` `k`, `-` `+` / drag a knob's bar | Pick a knob; move it a twentieth of its range |
| `r` | Show or hide the side column. It hides by itself when the pane is narrower than 640px, and the knobs and the strongest sensitivity move to the status line under the plot |
| `esc` | Clear the cursor |

With knobs set, the rail ranks them by **sensitivity** at the cursor:
each is nudged by 5% and the fractional change in the first series is
divided by that 5%, one extra solve per knob.

What the plot could not do shows as such: a point that failed to solve
leaves a **gap** (hatched, with the solver's message on hover), never a
line across it, and where the answer breaks one of the garden's own
constraints the span is shaded and edged in `--fault` dashes. Series
that measure the same dimension share a y axis, a second dimension takes
the right one (so do unitless series more than 100× apart in size) (`core/units.py` decides commensurability). Series colours
are `--s1`…`--s6`, by the series' place in the command so hiding one
never repaints the others; a seventh repeats the first, dashed.

**`plot save <name>`** writes the focused pane's line as a saved plot:
an ordinary mote with a `plot` property holding the *specification*
(what to solve, never the points), an instance of `Saved Plot`. Saving
a name again updates that plot. `\graph{name}` in a note draws it,
re-solved at every render (`docs/modoc.md`). **`plot export csv`** writes
the full-resolution points and **`plot export png`** the pane's picture,
to `path` or through a save dialog.

### Fitting (`fit`)

```
fit C1.current vs C1.voltage --data I vs V --free C1.r_a [--from bench.csv] [--scenario hot]
```

The model is `C1.current` as the garden solves it with `C1.voltage` set
to each point's x; the points are every mote carrying `I` and `V` (with
`--from`, only that CSV import's rows), converted into the model's units.
The fit moves the `--free` inputs by least squares (`core/fit.py`,
`docs/simulation.md` → *Fitting*). The pane draws the points as rings and
the fitted curve through them; the side column gives each value with its
σ (hover: where it started), RMS, R² and the points used, and says so
when a point wouldn't solve or the data can't tell two inputs apart.

**Nothing is written** until you apply it: `a`, a click on *apply*, or
`fit … --apply` typed. That writes each value, its σ into
`uncertainty`, and `source: {kind: fitted, note: ‹the line›}`, as one
undo step.

| Key | Does |
|---|---|
| `a` / click *apply* | Apply the fit |
| `h` `l` / click | Step through the points; the status line shows the point and the fit there |
| `esc` | Clear the cursor |

Not ported from the old window: baselines (a scenario's curve drawn
dashed beside each series) and the plot grid, which tiling replaces.
`fit` is the step after U4.

### Motion (`sim`, `rule`, `scenarios`, `mo tion`)

`mo tion` puts three panes where the focused one was:
`scenarios | sim | rule @sel`. Each is also a command on its own.

```
sim [--ticks N] [--scenario hot] [--vs garden|cold] [--solver newton] [type:Divider …]
```

**The sim pane is a run as a watch list** (`core/motion.py`). It runs
the garden N ticks in memory (default 50; a tick is one re-solve, which
is what `prev()` and `tick` read) and shows one row per value: its
state, its value at the playhead, Δ/tick, a bar for where that value
sits in its range over the run, and with `--vs` the other run's value
and the difference. Nothing is written.

| State | Means |
|---|---|
| `solved` | the solver determined it at some tick |
| `varied` | the scenario overrides it |
| `pinned` | a declared freedom nothing solved, so it keeps its value |
| `given` | everything else |

With no conditions the rows are what matter to the run: every solved
value, every freedom and every given a scenario varies. Find conditions
(the same as `find`'s) watch every number on the matched motes instead.
Under the table, the timeline draws the cursor row's series with its
min and max, constraint breaks and a failed tick in red, and the
playhead; click or drag it to scrub.

**One playhead for the window.** The sim pane that last ran or was
focused owns it; every Motion pane reads it, and the top bar shows
`tick 12/49 ‖` (`▶` while playing). Two sim panes under different
scenarios scrub together.

| Key (any Motion pane) | Does |
|---|---|
| `space` | Play / pause |
| `,` `.` | One tick back / on |
| `[` `]` | Ten ticks back / on |

| Key (sim) | Does |
|---|---|
| `j` `k`, `g` `G` | Move the row cursor (the timeline draws its row) |
| `⏎` / click | Select the row's mote (`rule @sel` follows) |
| `d` | Toggle `--vs garden` |

**The rule pane** (`rule @sel`, `rule series`) shows the rule's equation
and every value it binds, at the playhead's tick when a sim pane owns
it. A mote that isn't a rule shows every rule touching it. Names the
equation can't settle on one mote come back as hints (link the one you
mean). `why C1.current` shows how the last written solve got a value,
down through its inputs.

| Key (rule) | Does |
|---|---|
| `j` `k` | Move between bindings |
| `⏎` | Select the binding's mote |
| `w` | `why` for the binding, in this pane (`esc` goes back) |
| `i` | Put `set ‹rule› equation ‹it›` on the command line to edit |

**The scenarios pane** lists `(garden)` and every scenario with its
overrides beside the garden's value.

| Key (scenarios) | Does |
|---|---|
| `⏎` | Run the sim pane under it (a new sim pane if there is none) |
| `v` | Compare the sim pane against it (`--vs`) |
| `a` / `n` | Put `set --scenario ‹it› ` / `set --scenario ` on the command line |
| `d` `d` | Clear the override at the cursor |

**Solving.** `solve` writes one static solve and its provenance as one
undo step; it refuses a garden whose rules use `prev()` (only a run can
answer those). `solve --live on` is the old Fast mode: every write made
from the window is followed by a solve, folded into the same undo step,
and the top bar says `live`. It is kept per garden in the console's
sidecar, off by default.

Not ported from the old window: the node board (a graph of the model
is the `graph` pane, U8), value lanes, the pre-flight role check
and the impact view (both stay in the CLI and MCP).

### Notes (`edit`, `read`, `notes`, `mo ra`)

`mo ra` puts three panes where the focused one was:
`notes | edit @sel | read @sel`. Each is also a command on its own.

| Line | Pane |
|---|---|
| `notes [text]` | every note (name, words, problems), or those whose name or prose contains the text; `⏎` selects one, so `@sel` panes follow; `n` puts `edit ` on the command line |
| `edit @sel\|#id\|<name>` | the note's `.modoc` source. A mote with no note opens a blank page; a name no mote has makes the note on its first save |
| `read @sel\|#id\|<name>` | the note rendered, on the gray paper surface, from what the `edit` pane is typing (saved or not) |

**Editing is modal**, like every text-editing pane: `i` or `⏎` starts
typing, and from then on the editor has every key but `^w` until `esc`.
The source is highlighted (commands the parser doesn't know in --fault),
and a line with a problem is underlined (hover for why).

| Key (editing) | Does |
|---|---|
| `esc` | Stop editing (saves) |
| `^f` | Find / replace: `⏎` `⇧⏎` next / previous, `tab` to the replace field, `⏎` there replaces one, `^⏎` replaces all, `esc` closes |
| `^z` | The text box's own undo, typing and replacements alike |

**Saving** happens on every pause in typing. One editing session (`i` …
`esc`) is one undo step in the garden, however many pauses it saved on,
and `esc` logs it (`saved Homework 4`).

**The read pane** draws every macro (`docs/modoc.md`): links (a click
selects the mote), `\val`, numbered equations and `\ref`, `\work`, the
calc sections, `\checks` with ✓ / ✗, `\verification`, tables and figures
with captions, cited rules, `\include`d notes, and the note's problems at
the end. Maths is KaTeX; a `\graph` draws with the console's plot
renderer, re-solved (and remembered until the garden changes). `j` `k`
scroll, `g` `G` top and end.

Not ported from the old window: the `@` mote picker (type `\mote{name}`
or `\mote{#id}`), print to PDF (phase 6 makes real PDFs), and the
inspector's door into a note.

### The worksheet (`worksheet`)

The garden's calculation as lines of text (`docs/simulation.md` → *The
worksheet*), on the blueprint surface. Editing is modal as in a note.
While you type each line is only read: the margin marks it (`=` given,
`?` unknown, `ƒ` equation, `≡` relation, `✓` check, `!` a line that
can't be written, struck through, with why on hover), and an unknown or
an equation shows the garden's current value after it, faded. **`esc`
applies the whole sheet** as one undo step and logs what it made and
deleted; with `live` on, a solve follows. A sheet that applied cleanly
then shows the garden's own reading of it; one with problems keeps your
text so you can fix it.

The garden's reading is in **blocks**, one per independent calculation
(equations that share a mote), a blank line between them: each block's
givens and unknowns, then its equations, each after the ones that work
out what it reads.

### Calendar (`agenda`, `week`, `month`, `mo nastery`)

`mo nastery` puts `agenda | week` where the focused pane was.

| Line | Pane |
|---|---|
| `agenda [from] [to]` | a row per occurrence, grouped by day: time, name, which key placed it, `↻` repeats, `✎` has a note, `▣` owns a project. Today through two weeks by default |
| `week [date]` | the hour grid of the week holding `date`: all-day items and bars across the top, timed events below, side by side where they overlap |
| `month [date]` | six weeks of day cells, three items each and `+N more` (opens that week) |

Dates are ISO days or what `find` reads as one (`today`, `7d`, `-2w`).
All three take `--keys a,b` (which date keys), `--type ‹species›`, and
find conditions (`agenda status!:done`).

**Bars** run across the days they cover: any multi-day value
(`2026-10-01/2026-10-09`), and any mote with both a start and a `due` --
its `start`/`starts` value, else the day its project was started -- drawn
dashed. A project's owner shows on its own because it carries `due`.

| Key | Does |
|---|---|
| `[` `]`, `t` | Page back / on (rewrites the date in the line), today |
| `j` `k` | Move between events |
| `⏎` / click | Select the mote (props and `read @sel` follow) |
| `o` | Follow: a note → `read`, a project owner → `cd` to its garden, else `open #id` |
| `J` `K`, `H` `L` | Move an hour (a day when all-day), a day |
| `<` `>` | Shorter / longer (half an hour, or a day when all-day) |
| `n` | New event: `‹name›.date = ‹slot›` on the command line, caret at the name |
| `d` `d` | Delete (runs; a recurring event asks `t` / `s` first) |
| `b` / `B` | Hide the key under the cursor / show every key |

In the week grid, drag an event to move it (a quarter hour at a time,
across days too) and drag its bottom edge to resize; click an empty slot
to point `n` at it, double-click to start an event there.

**Every change is a command.** A move or resize runs the `set` that
writes the new value into the key that placed the event, in the shape it
was written. A **recurring** event asks first: `t` this one -- `detach
#id ‹key› ‹occurrence› ‹start› ‹end›`, which excepts that occurrence from
the series and copies it to its new time as a mote of its own (one undo
step) -- or `s` the series. Deleting one occurrence is a `set` that adds
an `EXDATE`.

#### Which dates

There is no "calendar entry" type, because MonOSS does not have types.
The calendar reads **every** property whose value parses as a date and
shows each such key -- so a garden where meetings say `when`, tasks say
`due` and events say `starts` shows all three at once, with no migration
and nothing declared. `--keys due,date` narrows a pane to some of them,
and `b` / `B` hide the key under the cursor / show them all again.

This is a read-side heuristic only. A new event from `n` writes `date`:
reading is general because a garden is, but a new date has one right
home.

A value is an **ISO 8601 interval** and carries its own span and
repetition -- there is no separate `duration` or `recurrence` property,
and no reserved name involved anywhere:

| Value | Placed as |
|---|---|
| `2026-08-24` | all-day, one day |
| `2026-08-24/2026-08-27` | all-day, the 24th to the 26th — **the end is exclusive** |
| `2026-08-24T09:15`, or `2026-08-24 09:15` | timed, the default 60 minutes |
| `2026-08-24T09:15/PT20M` | timed, 20 minutes |
| `2026-08-24T09:15/2026-08-24T11:00` | timed, an explicit end |
| an `RRULE:…` line beneath any of them | expanded into one occurrence per repetition inside the visible range |
| an `EXDATE:…` line beneath that | one occurrence excepted out of the series |

Durations are days, hours, minutes and seconds (`P2D`, `PT1H30M`) —
years and months have no fixed length, so an event lasting "one month"
has no single answer.

Gardens written the old way — `date` plus a separate `duration`, `ends`
or `recurrence` — are folded into this on open, keeping whatever key each
mote already used. The one thing it costs: typing a bare `duration`
beside a date no longer does anything on its own.

A recurring mote is still **one mote**: every occurrence it draws opens
the same detail panel -- one mote → one event → the whole series. A rule
that doesn't parse
places the mote on its own date rather than hiding it -- the same
permissiveness a broken note gets in the read pane.

Templates and scenario overrides are never placed, even when dated: they
hold replacement values rather than real data.

Not ported from the old window: the mini calendar, the draft popover's
species picker (type `R1 : task` after creating), and polling -- the
console re-reads on focus and after every write, like every pane.

### The graph (`graph`, `mo narch`)

`mo narch` puts `find | graph @sel --depth 2 | open @sel` where the
focused pane was.

| Line | Pane |
|---|---|
| `graph` | the whole garden |
| `graph @sel\|#id\|‹name› [--depth N]` | that mote's neighbourhood: every mote within N hops (default 2), relationships counted both ways; `@sel` re-roots as the selection moves |
| `… --hide-rel a,b` | leave out those relationship types -- not drawn, and not hopped through |
| `… --view ‹saved›`, `… ‹conditions›` | keep only what a saved view or find conditions match (the root stays) |

Motes are squares stroked in their species colour, a species a diamond,
a rule a square with a hollow one inside; the selection is filled.
Instances cluster on their species' spoke. Edges touching the selection
are solid and say their type; the rest are dashed. The corners say the
layout and depth, the zoom and how much of the garden is shown, and what
is under the pointer or the cursor.

| Key / mouse | Does |
|---|---|
| `h j k l`, arrows | Walk the cursor to the neighbour that way |
| `⏎` / click | Select the mote (the graph re-roots on it under `@sel`) |
| `o` / double-click | Open its props in a new pane |
| `[` `]` | Depth −1 / +1 (rewrites the line) |
| `f`, `+` `-`, `0`, wheel | Fit, zoom, 100% at the cursor, zoom at the pointer |
| drag a node | Move and pin it (pins are kept beside the garden) |
| `u` / `U` | Unpin the cursor's node / every node |
| drag empty space | Pan |
| ctrl+drag node → node | `A ->  -> B` on the command line, caret on the type |
| `n` | `‹cursor› -> related_to -> ` on the command line, to name a new mote |
| click an edge, `d` `d` | `unlink …` the edge, else `delete ‹cursor›` (runs) |

Monarch's sticky notes, group boxes and pen strokes are gone, and the
first open of a garden drops their stored data (`docs/architecture.md`
-> *The graph*).

### Home (`gardens`, `garden`, `mo saic`)

`mo saic` puts `gardens | garden` where the focused pane was. The top
bar's breadcrumb is the open garden's ownership path
(`/ college-fall-2026 / hw4`); click a crumb to `cd` there.

| Line | Pane |
|---|---|
| `gardens [--archived]` | every garden: owned ones under their owner's garden, with the cover's name, due and status and the mote count; then loose gardens; then archived ones, folded unless `--archived`. `●` is the open garden, `⇅` syncs, `!` has an ownership problem (listed under the tree) |
| `garden [‹name›]` | one garden (none: the open one): its path, owner and cover, what it owns, counts, size and file, last change, sync, why it is archived, and its problems. It follows the tree's cursor |

| Key | Does |
|---|---|
| `j` `k` / click | Move (the `garden` pane follows) |
| `⏎` | `cd` there |
| `n`, `r` | `garden new …`, `garden rename ‹garden› …` on the command line |
| `d` `d` | `garden rm ‹garden›` (runs; `garden restore` brings it back) |
| `a` | Archive / unarchive |
| `s` | `garden sync ‹garden› …` on the command line |
| `e`, `i` | Export (asks where), `garden import …` |
| `z` | Show / fold the archived gardens |

**Garden commands.** None is on `undo` -- they act on gardens, not on
the open garden's motes.

| Command | Notes |
|---|---|
| `garden new ‹name›` | Name checked against the rule `core/db.py` enforces |
| `garden rename ‹old› ‹new›` | Moves the `.db` and its sidecars (layouts and history too). Refused while the garden syncs -- the sync server keys its history by name. Renaming the open garden reopens the console on the new name |
| `garden rm ‹name›` | Typed, asks for a second ⏎ (`d d` in the tree doesn't), then moves the file aside; `garden restore ‹name›` brings it back. The open garden can't be removed |
| `garden archive\|unarchive ‹name›` | Archives it and everything it owns (`archived: true` on its owner, or a marker mote in a loose garden) |
| `garden export ‹name› [path]`, `garden import [path] ‹new name›` | JSON, `core/garden_io.py`'s format; without a path, a file dialog. Import always makes a new garden, and removes it again if the file fails |
| `garden sync ‹name› ‹url›\|off\|now\|auto on\|off` | Point it at a sync server, stop (the oplog stays), sync once (errors shown), or gate the automatic syncs on open, `cd` and close |
| `project start @sel [‹garden›]`, `project stop @sel` | Give a mote a garden of its own (made, or a loose one adopted) / drop its claim. One undo each; neither deletes a garden |

### Tiling

`^w` is the prefix: `v`/`s` split side by side or stacked, `h j k l`
move focus, `<`/`>` resize, `o` only this pane, `=` equalize, `x`
swap. `1`–`9` switch saved layouts, `e` (or a click on a header)
loads a pane's command into the command line, and `x` closes the
pane. A new pane is blank and asks for an ordinary command: the
command line takes the caret, `⏎` runs the line into the new pane, and
`esc` closes it. Dividers drag with the mouse.

Layouts and history are UI state, not knowledge: they live in
`gardens/<name>.db.console.json`, never in the garden or in sync, and
they follow the garden through rename, delete and restore.

### Ports

The console asks for port **42009**, with WebKit storage in
`.webview_data_console/`. A fixed port keeps the page's origin, and so
its browser storage, the same between launches; a second console falls
back to a free port (`window_launcher.choose_port`).

### Smoke test

```sh
.venv/bin/python scripts/console_smoke_test.py
```

Boots on a seeded temp garden and checks the following:
- `^w v` splits, and `log` typed into the blank pane places it
- a divider drag and `^w >` resize
- a statement lands in the log and selects its mote
- `layout save` works and drift marks the layout unsaved
- the layout and history survive both a reload and a relaunch on a fresh `Api`
- `cd` swaps gardens
- no JavaScript errors
- `find type:chore` shows a table; click, `j j`, shift-click selects three rows
- `set @sel` writes all three and one `undo` reverts them
- a header click rewrites the pane's command with `--sort`
- an inline cell edit writes through `set`
- a sorted `find` pane survives a relaunch
- `open @sel` follows a table click; add, rename (and one `undo`), link,
  `d d` delete and `⏎` on a link from the props pane
- `p` opens the float on the selection and `esc` closes it
- an `open @sel` pane survives a relaunch
- `mo xie` opens a plot pane; a `plot` line draws the solved curve and the
  cursor reads it; zoom, `y` and `t` rewrite the line; a knob moves the
  curve and never writes; `plot save` writes a saved plot that one `undo`
  removes; `plot export csv` and `png` write files
- a `fit` pane shows the four measured points, the fitted curve and
  `r_a = 50` without writing; `a` applies it with `source: fitted` and one
  `undo` takes it back
- a plot pane survives a relaunch
- `mo narch` opens find | graph | props and the graph draws the
  selection's neighbourhood; a walk, `⏎`, and `[` `]` rewriting the
  depth; ctrl+drag prefills a link and `d d` deletes; dragging pins a
  node beside the garden, `U` unpins and `o` opens
- `mo saic` opens gardens | garden and the detail follows the cursor;
  `garden new`, `garden rm` (a second ⏎) and `garden restore`; `project
  start @sel` makes a garden, and the breadcrumb shows and climbs its path

---

# Installing pywebview

## This machine (Arch Linux)

`webkit2gtk-4.1`, `gtk3`, and `python-gobject` are already installed system-
wide, and Arch ships a matching package for pywebview itself:

```
sudo pacman -S python-pywebview
```

This is built against the system GTK/WebKit stack already present, so there's
nothing else to install and no `venv` considerations if you're running with
the system Python.

## Portable / pip path (other machines, or a project venv)

pywebview's GTK backend needs `PyGObject` (the `gi` module), which pip
struggles to build from source reliably. If you're using a venv, create it
with access to the system-installed `PyGObject` instead of trying to build a
fresh copy:

```
python3 -m venv --system-site-packages .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Avoid `pip install pywebview[gtk]` — that extra tries to compile
`PyGObject`/`pycairo` from source, which is unnecessary (and often fails)
when a working system install already exists.

## Smoke test

```
python3 -c "import gi, webview; from importlib.metadata import version; print(version('pywebview'))"
```

If that prints a version with no errors, `python3 run_console.py` should open the
Mosaic window.

Note: if your `python3` on `PATH` resolves to a non-system interpreter (e.g.
a conda/miniforge install), `--system-site-packages` won't see the system
`gi` package — it only reaches the site-packages of the interpreter the venv
was built from. Point the venv at the real system interpreter explicitly,
e.g. `/usr/bin/python3 -m venv --system-site-packages .venv`.

## Desktop launcher

A user-level launcher puts the console in the regular application menu
instead of needing a terminal. It is machine-specific (it points at this
checkout's `.venv`), so a script writes it rather than the repo
tracking it:

```
scripts/install_desktop_entry.sh
```

That writes `~/.local/share/applications/monoss.desktop` -- the file
Monarch's launcher used, so an old Monarch entry becomes Mosaic -- and
runs `update-desktop-database`. Run it again after moving the checkout.
`StartupWMClass=Mosaic` has to match the window's own title (the `name`
`console/app.py` passes to `launch_window`), or the desktop environment
won't associate the launcher with the running window. From a terminal,
`./run_console.py [--garden name]` works as `./run_monarch.py` did.

`assets/icon.svg` predates the console's palette and is still in blue;
nothing here changes it.

## Regression testing

`pytest` covers `core/` and the bridges in isolation, but doesn't touch
the actual GTK/WebKit window. `scripts/console_smoke_test.py` drives the
real console (see *Smoke test* under the console above) against a
throwaway temp `GARDENS_DIR` and webview storage dir -- never touching
real data. Run it by hand after UI changes. It prints PASS/FAIL per
check and exits non-zero if anything failed.

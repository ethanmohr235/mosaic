# Running the desktop UI

Six native windows (via `pywebview`), each wrapping a local HTML/CSS/JS
frontend — no browser tab, no local web server: **Monarch**, the graph
(`run_monarch.py`); **Mora**, the note editor (`run_mora.py`);
**Monastery**, the calendar (`run_monastery.py`); **Momentum**, the
task board and timer (`run_momentum.py`); **Monolith**, the table
(`run_monolith.py`); and **Motion**, the simulator (`run_motion.py`).
They share one palette and one set of vendored fonts, so the six read as
one product.

A seventh window sits beside rather than among them: **Gardens**
(`run_gardens.py`), where the gardens themselves are made, renamed,
deleted, imported, exported and synced. Any of the six opens it from its
own garden dropdown ("Manage gardens…"); see below.

---

# Monarch -- the graph

`./run_monarch.py`, or `.venv/bin/python run_monarch.py`.

```
./run_monarch.py                       # the last garden opened
./run_monarch.py --garden reactor      # a specific garden
```

The graph is drawn on a `<canvas>` by `monarch/frontend/graph.js`, which owns the
force layout, camera and pointer gestures. There is no graph library: motes
are clustered into radial sectors by species, and the camera pans to a mote
on selection rather than re-fitting the whole view. Fonts are vendored under
`monarch/frontend/vendor/fonts/`, so nothing is fetched at runtime.

### The inspector

Selecting a mote -- here, or in any of the other five windows -- opens
the **inspector**: a floating panel on the right that is literally the
same panel everywhere (`frontend_shared/inspector.js`, symlinked into
each app's `frontend/`; see "The inspector" in `docs/architecture.md`
for why). It is where a mote is edited, and what it can do does not
change from window to window.

| Gesture | Action |
|---|---|
| Left-click a mote | Open it, replacing the active tab |
| Right-click a mote → Inspect in new tab | Open it in an additional tab |
| Click a relationship, or a property that holds a mote id | Walk to that mote (Ctrl/Cmd-click for a new tab) |
| Drag the header or tab strip | Move the panel |
| Drag the bottom-right grip | Resize it |
| Esc | Close it (or, while typing in a field, just leave the field) |

Up to six tabs; the oldest drops off, and with only one open the tab
ribbon hides entirely. Position and size are remembered per window, and
the panel opens in the same place in each one.

"In a new tab" is a right-click item rather than Ctrl-click because
Ctrl+drag from a mote already draws a relationship in both windows with
a canvas — deciding which of the two you meant from how far the mouse
travelled is how you get a relationship you didn't ask for. Inside the
panel, where nothing drags, Ctrl-click on a relationship or a mote-id
property still opens it in a new tab.

In it:

* **The name**, as the title. Editing it renames the mote.
* **Properties**, one row each, with an editor chosen from the value --
  a dropdown where the species' other instances agree on a small set of
  values, a full calendar for anything interval-shaped (see below), a
  number box for a number, a toggle for a boolean, and a button that
  walks there for a value that holds another mote's id. Click a key to
  rename it in place. `×` removes it.
* **An add row**, and -- if the mote's species has a template -- chips
  for the keys that template has and this mote doesn't ("Task usually
  has owner, due").
* **Relationships**, both directions, each removable, plus a picker that
  searches the garden and accepts a relationship type that doesn't exist
  yet. `instance_of` is in this list like anything else — a species *is*
  a relationship, and giving a mote one means adding that relationship
  here.
* **Advanced** (the footer toggle, remembered): the mote's id with a
  copy button, a `name` marker under the title (the title is that
  property, in a bigger font), whether this mote is its species'
  template, the door to its note in Mora, the raw JSON, and -- in Motion
  only -- a `why` button per property that reads the run log for where a
  solved value came from.

Everything autosaves as you type, debounced, with a `saved` flash in the
header.

Deliberately not in it: deleting a mote, and editing a selection of
several. Both stay in each window's own chrome -- the inspector is about
one mote.


### Dates

Any property whose value is an ISO 8601 interval — a plain `2026-09-22`
counts — is edited with a calendar rather than a text box, in every
window. By value shape, not by key name: `when`, `due`, `starts` and
whatever else this garden calls it all get the same editor.

Click the summary line to open it. Inside: a month grid with Today /
Tomorrow / Next week shortcuts, a start time and an optional end time
(shown with the duration it works out to), and a repeat editor —
frequency, how many of that unit, which weekdays for a weekly rule, and
whether it ends never, after N times, or on a date. Done closes it;
Clear removes the property's value entirely.

A rule the editor can't represent — two RRULE lines, a `BYMONTHDAY`, an
`EXDATE` left behind by detaching one occurrence in Monastery — is shown
as editable RRULE text instead, and anything it doesn't understand is
preserved rather than dropped.

### Right-click

Every window has a context menu on a mote now, and the first two items
are the same everywhere: **Inspect** and **Inspect in new tab**. What
follows is that window's own — Monarch offers copy/pin/delete on the
whole selection, Monastery offers removing the one occurrence you
clicked, Motion offers connecting or editing a rule.

### Graph interactions

| Gesture | Action |
|---|---|
| Left-click a mote | Select it, and open it in the inspector |
| Right-click a mote | Context menu, including "Inspect in new tab" |
| Left-click an edge | Select the relationship it represents |
| Shift+Left-click an edge | Add/remove that relationship from the selection |
| Left-click empty canvas | Deselect |
| Left-drag a mote | Move + pin it (drags the whole selection together if it's part of one) |
| Left-drag empty canvas | Box-select motes |
| Shift+Left-drag empty canvas | Box-select relationships (distinct highlight color; mote and relationship selection stay mutually exclusive) |
| Ctrl+Left-drag from a mote | Connect it to another mote |
| Middle-drag | Pan |
| Right-click a mote | Context menu: Copy, Pin/Unpin, Delete (acts on the whole selection if the mote is part of one) |
| Right-click empty canvas | Context menu: New mote here, Paste |
| Ctrl/Cmd+C / Ctrl/Cmd+V | Copy / paste the current selection (Ctrl+V pastes at the viewport center) |
| Ctrl/Cmd+Z | Undo the most recent delete, while its toast is still up |
| Arrow keys | Select whichever visible neighbour sits closest to that screen direction |
| Delete / Backspace | Delete the current selection |
| Esc | Close the frontmost menu/panel, or deselect if nothing else is open |
| Scroll | Zoom |
| Left-drag an attractor (attractor mode only) | Move that species' cluster |

Motes/relationships highlight live as a box-select drag grows over them,
not only once the mouse is released. Copying more than one mote preserves
their relative layout on paste. The topbar's garden button opens a
switcher over the gardens that exist; its "Manage gardens…" item (also
reachable from the File menu) opens the Gardens window, which is where
creating, renaming, deleting, importing, exporting and syncing live —
for every window, not just this one.

Shift+clicking an edge, or shift-dragging a box, builds up a relationship
selection the same way plain box-select does for motes. Selecting
relationships and selecting motes are mutually exclusive, same as a
single relationship and a single mote already were: picking one kind
clears the other. The detail panel's bulk delete button (and
Delete/Backspace) act on whichever kind is currently selected.

The search palette (Ctrl/Cmd+K) also accepts a `key = value` query (e.g.
`priority = high`) instead of the usual substring match across every
mote's label and properties -- an exact, case-insensitive match on one
named property. Combine it with the species/relationship filter chips for
something like "every Task with priority = high." Anything more involved
than that is expected to live in external BI software instead, not here.

Deleting motes/relationships surfaces a toast with an "Undo"
action (also triggerable with Ctrl/Cmd+Z) for a few seconds afterward.
It's single-step -- only the most recent delete can be undone, and the
window closes once the toast disappears. A restored mote/relationship
keeps its original id, so anything else that was pointing at it (other
relationships) doesn't need to be reconnected by hand. Garden delete
works the same way at the file level: the garden's `.db` file is moved
aside rather than removed, so `restore_garden` can move it back; only the
most recently trashed copy of a given name is kept.

### Topbar menus

The topbar is brand · garden dropdown · File · View · Tools · search, each
of File/View/Tools an anchored dropdown reusing the same `.menu`/
`.menu-item` styling the right-click context menu already established.
Only one of these (plus the garden dropdown, plus the context menu) is
ever open at a time -- opening any one closes the rest, and Esc or a
click outside the open one closes it too.

Most items are real, existing actions relocated rather than rebuilt:
File hosts "Manage gardens…" (the same Gardens window the garden
dropdown opens) and "Export to Calendar"; View hosts "Reset
view", a "Hide/Show controls" toggle for the always-docked
top-right panel (species legend, relationship filters, depth, reset
view) -- the first way to reclaim that screen space, persisted across
restarts -- and "Advanced view settings…" (below); Tools hosts the
simulation step count, the **solver** and **scenario** pickers, the
Simulate…/Exit simulation trigger for the same non-blocking
preview/scrub/commit bar described above, "Check roles…", and
"Notes (Mora)…", which opens the note editor on the current garden (see
Mora, below), and "Sync…", which opens the Gardens window straight at
this garden's sync settings. A handful of items (Import motes, Export as CSV/JSON,
Print graph, Timeline view, Group by type, Automations, Webhooks, API
access) are inert placeholders with a small "soon" badge, staged for
future expansion -- clicking one just toasts an explanation rather than
doing nothing silently. A stub is only allowed to describe something
that genuinely doesn't exist yet: if the app can already do it
somewhere, the menu item points at that instead.

### Explaining a value

Every **numeric** property in the detail panel carries a `?` button that
opens a block underneath it answering two different questions from two
different sources:

- **How it was found** comes from the run log (`why`), so it needs a run
  to have happened -- a discarded preview leaves nothing to read. It
  names the equations that produced the value, the known values they
  read, and expands recursively into any of those this run also derived.
  A value found together with others in a coupled solve says so rather
  than pretending to a single cause. Numbers here are in **base SI
  units**, which is what the solver worked in, so the numbers beside an
  equation multiply out -- unlike the property rows above, which show
  each mote's own declared unit.
- **What it needs** and **what it could move** come from the rules
  themselves, so they answer before anything has ever been solved.
  Missing prerequisites are marked in red; anything the value could move
  is clickable and selects that mote.

The `?` is offered on every numeric property rather than only on solved
ones, because which of the two a property is happens to be exactly what
the panel answers.

### Scenarios

The Tools menu's **scenario** picker lists every mote that varies
something (see `docs/simulation.md`), defaulting to "Garden as it
stands". Running under a scenario previews the garden as that scenario
would have it, and the sim bar says so.

A scenario run is never written to the graph, so **Commit is hidden**
while one is open -- the engine refuses it outright. "Save simulation"
still applies, and is how a hypothetical is kept for comparison against
the real run.

### Warnings and constraints

Below the sim bar's error line (which only appears when a run actually
stopped) is an advisory area for things that did not stop it: dimensional
warnings about the *model*, and unsatisfied comparison relations about
the *answer*. They are labelled apart because they are different kinds
of problem, and neither prevents committing.

### Advanced view settings

Everything the graph's layout and colouring used to hardcode is
adjustable per garden, from a panel opened by the control panel's
"⚙ Advanced view settings" button or the View menu's matching item. It
docks on the left (sharing that corner with the run-history panel, so
opening one closes the other) rather than opening as a modal: every
control in it is judged by what it does to the graph, so the graph has to
stay visible while you drag a slider.

**Forces.** Three global sliders -- repulsion, species pull (how hard a
mote is drawn to its species' attractor), and edge length -- plus one
attraction slider per relationship type in the garden. A type left alone
reads "auto" and keeps the species-aware default (`0.05` within a
species, `0.006` across one, since species grouping is what carries the
layout); moving its slider replaces that with one explicit number for
every edge of that type. Each slider has a ↺ back to its default.

**Colours.** By default a mote's colour is its species', hashed from the
species id, and each species gets a picker to override that with any
colour. The alternative modes colour by something other than membership:
by a chosen property's value (a hue per distinct value), by connection
count, or by a chosen numeric property (both on a dim→bright ramp, with
motes that have no usable value left grey). While a non-species mode is
active the legend's dots hollow out -- the rows still filter by species,
they just aren't a colour key any more. Colour-by-last-modified is
deliberately absent: nothing in the schema records when a mote changed.

**Layout.** "Arrange attractors…" enters a read-only mode where the point
each species' cluster is pulled toward is drawn as a labelled ring and can
be dragged. Only panning and zooming still work there -- selecting,
dragging, connecting and the context menu are all off, and any edit
attempted from elsewhere toasts instead, the same way it does mid-
simulation-preview. Esc (or "Done arranging") leaves. A pinned mote
ignores its anchor entirely, so a hand-arranged garden can make a moved
attractor look broken; "Unpin all motes" is the way out of that.

Settings live in `localStorage` under `monoss:viewSettings:<garden>`,
beside the existing hidden-species and pinned-position keys, and only
what's actually been changed is stored -- a garden nobody has adjusted has
no key at all and renders on `graph.js`'s own defaults. This is view
state, not garden data: it stays on this machine and is not part of a
garden export.

### Linking out to files and websites

`url` is a reserved property name: give any mote a `url` property (a
web address or a local file path) and its inspector row gets an
"Open" (↗) button that launches it via the OS's default handler. In all
six windows -- the button moved into the shared inspector along with
everything else about editing one mote, so following a link is no longer
something only the graph window could do.
`core/links.py::classify_link` decides which: a web address goes to the
stdlib `webbrowser` module, while a local file is instead handed to the
OS's native "open with the registered default app" mechanism
(`api_common.py::open_with_os_default`), not `webbrowser.open()`, since
that always launches the web browser itself and would show a `.py`
file as plain text rather than opening it in, say, VSCode. On Linux
that's `gio open` (falling back to `xdg-open` only if `gio` isn't
installed) rather than `xdg-open` directly -- `gio`'s content-type
detection is the same extension-aware logic GTK file managers use,
whereas `xdg-open`'s own MIME-sniffing can land on a different, unregistered
alias for the same file (observed for `.py`: `text/x-script.python`
instead of `text/x-python`) and silently fall back to something else
entirely instead of erroring. On macOS it's `open`; on Windows,
`os.startfile`. A bare domain like
`youtube.com` (no `http(s)://`) is assumed to be a URL missing its
scheme and gets `https://` added automatically; local paths need to be
absolute or start with `~`/`./`/`../` to be recognized as local rather
than a URL, since there's no other way to tell "example.com/page" and
"notes/todo.txt" apart. This is purely a naming convention, not a
schema change -- the property is still a
normal, editable/deletable string like any other. There's no drag-and-
drop yet; the path/URL has to be pasted in by hand.

### Reserved names

`docs/architecture.md` lists every property key and relationship type
MonOSS recognizes by convention (`url`, `date`, `duration`, `equation`,
`freedoms`, `roles`, `instance_of`, `overrides`, `varies`, ...). Any property or relationship using one of these
is accented in the inspector rather than drawn as ordinary data --
hover it for a tooltip. A reserved property key is also not renameable
in place, since its name is what gives it meaning. This is
purely a visual cue; the name still works exactly like any other
property/relationship, editable and deletable like anything else.

### Look and feel

All three windows are dark-only and share one palette, one type pairing
(IBM Plex Serif for names and titles, IBM Plex Sans for body, a mono for
labels and ids) and one set of vendored fonts. The shared tokens are
declared at the top of `monarch/frontend/style.css` and copied verbatim
into `mora/frontend/style.css` and `monastery/frontend/style.css` --
pywebview's http server roots at the directory of the page it serves, so
a stylesheet one directory over is not reachable, and three copies is
the price of three windows that look like one product. Change a shared
token in one and change it in all three.

The recurring pieces, so a new surface doesn't invent its own: a 46px
topbar carrying a two-line brand block (the window's name over `MonOSS`
in mono); section heads as a small, widely-tracked uppercase mono
eyebrow; serif for a mote's name, a garden's name and a panel title;
mono for ids, counts, keys and hints; and a 30px status bar along the
bottom. Monarch's reports which garden is open, how much of it the
current depth put on screen, and what is selected.

Colours are OKLCH, which this WebKitGTK build handles in both CSS and
canvas `fillStyle` -- which is why `graph.js` reads `--graph-bg` and the
selection tokens straight off `:root` rather than keeping its own copies.

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

If that prints a version with no errors, `python3 run_monarch.py` should open the
Monarch window.

Note: if your `python3` on `PATH` resolves to a non-system interpreter (e.g.
a conda/miniforge install), `--system-site-packages` won't see the system
`gi` package — it only reaches the site-packages of the interpreter the venv
was built from. Point the venv at the real system interpreter explicitly,
e.g. `/usr/bin/python3 -m venv --system-site-packages .venv`.

## Desktop launcher

Each window has its own user-level launcher (none tracked in this repo --
they're machine-specific, pointing at an absolute `.venv` path) so it shows
up in the regular application menu instead of needing a terminal:
`~/.local/share/applications/monoss.desktop` for Monarch,
`mora.desktop` for Mora, `monastery.desktop` for Monastery. In every case
`StartupWMClass` has to match that window's own title (the string passed
to `webview.create_window(...)` in its `app.py`), not the product name, or
the desktop environment won't associate the launcher with the running
window. Regenerate them with:

```
cat > ~/.local/share/applications/monoss.desktop <<EOF
[Desktop Entry]
Type=Application
Name=Monarch
Comment=Personal knowledge-graph organizer
Exec=$(pwd)/.venv/bin/python $(pwd)/run_monarch.py
Icon=$(pwd)/assets/icon.svg
Terminal=false
Categories=Utility;
StartupWMClass=Monarch
EOF

cat > ~/.local/share/applications/mora.desktop <<EOF
[Desktop Entry]
Type=Application
Name=Mora
Comment=Note editor for a MonOSS garden
Exec=$(pwd)/.venv/bin/python $(pwd)/run_mora.py
Icon=$(pwd)/assets/icon-mora.svg
Terminal=false
Categories=Utility;
StartupWMClass=Mora
EOF

cat > ~/.local/share/applications/monastery.desktop <<EOF
[Desktop Entry]
Type=Application
Name=Monastery
Comment=Calendar over a MonOSS garden's dated motes
Exec=$(pwd)/.venv/bin/python $(pwd)/run_monastery.py
Icon=$(pwd)/assets/icon-monastery.svg
Terminal=false
Categories=Utility;
StartupWMClass=Monastery
EOF

update-desktop-database ~/.local/share/applications/
```

Mora's and Monastery's icons (`assets/icon-mora.svg`, `assets/icon-monastery.svg`)
are the same ringed-planet mark both windows already draw in their own
topbar brand block (`mora/frontend/index.html`, `monastery/frontend/index.html`)
-- the two are visually identical today because that mark is, not a
mistake introduced here. Monarch's `assets/icon.svg` predates the amber
accent theme and is still in blue; nothing here changes it.

## Regression testing

`pytest` covers `core/`/`monarch/api.py` in isolation, but doesn't touch the
actual GTK/WebKit window. `scripts/monarch_smoke_test.py` drives the real running
app via pywebview's `evaluate_js` (mote/relationship CRUD, label rendering,
layout position stability, search/browse/keyboard-nav, garden-switch
isolation, species picking and templating, filters, depth, live box-select, middle-drag pan, the context menu's
copy/paste/pin, multi-select copy/paste, Ctrl+C/Ctrl+V, bulk delete,
Esc/Del, arrow-key navigation, the gardens modal's list/switch/
create/delete, undoing a delete via the toast button and Ctrl+Z, the
url property's Open button, dragging/pinning a whole multi-selection
together, reserved-name badges, the resizable detail panel, selecting
and inspecting a relationship by clicking its edge, shift+click and
shift+drag both building a multi-relationship selection (live, and with
the distinct box/edge color) and bulk-deleting it, a plain drag still
box-selecting only motes, the "New mote here" form opening near the
click, creating a mote by pressing Enter in that form instead of clicking
Create, that form closing on an outside click, the search palette's
`key = value` exact-match query, the topbar's File/View/Tools/garden
dropdowns opening one at a time, closing on Esc or an outside click, the
garden dropdown listing/switching gardens, the View menu's Hide/Show
controls toggle and its persistence, a "soon" stub toasting
instead of doing nothing, a hidden species' instances actually dropping
out of the physics loop instead of just not being drawn, the advanced
view settings panel (a per-relationship-type force slider reaching the
layout and persisting, a custom species colour surviving a garden switch,
colour-by-property-value giving different values different colours and no
value grey, and "Reset every view setting" restoring every default),
attractor mode (entering it, dragging an attractor so the species' motes
follow, clicking a mote *not* opening the panel, and leaving)) against a
throwaway temp `GARDENS_DIR` and
webview storage dir — never touches real data. Run it by hand after UI
changes:

```
.venv/bin/python scripts/monarch_smoke_test.py
```

Prints PASS/FAIL per check and exits non-zero if anything failed. Its
own waits are polling, not flat sleeps sized for the worst case
(`js_async`'s `wait=` and `wait_until()`'s `timeout=` are both ceilings),
so it finishes well under the naive fixed-sleep total in the common case.

One check samples the canvas pixels directly. That is not paranoia: the
graph's render loop was once entirely dead while every other check passed,
because hit-testing works off node coordinates rather than what was drawn —
clicking, dragging and connecting all succeeded against a blank canvas.

---

# Mora -- the note editor

A second window, over the same gardens: `./run_mora.py`, or
`.venv/bin/python run_mora.py`. Same stack as the main app -- pywebview
over a local HTML/CSS/JS frontend, no browser tab, no build step -- but a
separate process holding its own garden connection, so it opens and
closes independently of Monarch.

```
./run_mora.py                       # the default garden
./run_mora.py --garden reactor      # a specific garden
./run_mora.py --note <mote id>      # straight to one mote's note
```

### Opening it from the graph

Three ways, all of them the same launch with different arguments:

| Where | Opens |
|---|---|
| Tools → **Notes (Mora)…** | Mora on the current garden |
| Detail panel → **Write a note… / Open note…** | Mora on the selected mote |
| Detail panel → the `modoc` row's ↗ | the same, from the property itself |

`monarch/api.py::open_mora` spawns it detached with `sys.executable` -- the
virtualenv the app is already running in -- so it is a sibling process,
not a child window, and closing the graph does not close it. Its output
is *not* discarded: a Mora that fails to start prints its traceback on
whatever terminal Monarch was launched from.

The two apps must not want the same HTTP port. pywebview pins its
built-in server to 42001 whenever `private_mode` is off and no port is
given, which Monarch takes; Mora asks for **42002**, falling
back to whatever the OS hands out if that is busy (a second Mora
window). This is not a tidiness point -- the process that loses that
race still points its window at the winner's address, so it comes up
showing the other app's page against its own bridge. Mora keeps its own
WebKit storage directory (`.webview_data_mora/`) for the same reason:
Monarch's is not something two processes should share, least of
all when each clears its own cache on startup.

Two consequences worth knowing. The two processes hold separate
connections to the same garden file, so neither sees the other's writes
until it refetches -- refocus the graph after saving in Mora. Nothing is
lost either way: both write one property at a time, so editing a mote in
the panel never clobbers a note saved next door.

A note is a property like any other, so it also appears in the detail
panel's Properties list -- as a `N lines — edit in Mora` summary rather
than an editable field, because a document is not a thing to edit through
a one-line box in a side panel. Deleting it is an ordinary property
delete, because that is all it is.

With neither flag it opens the first mote in the garden that already
carries a note, and otherwise the most recent mote -- as an empty page to
start writing on, not an error. `docs/modoc.md` is the language; this is
the window.

| Pane | |
|---|---|
| Source | a textarea over a syntax-highlight layer. `@` opens a mote picker that inserts `\mote{<id>}{<name>}` |
| Rendered | the note with every macro resolved. Clicking a `\mote` link moves the data panel to it |
| Data | the anchor mote's properties and relationships, everything linked from the text with reference counts, and any Problems |

`Source` / `Split` / `Read` choose which panes are shown; `Data` toggles
the right one.

| Control | Action |
|---|---|
| (autosave) | About a second after typing stops -- and on leaving the source pane, switching note or garden, or closing the window -- the source is written back on its own. The pill reads `edited`, `saving…`, `synced` |
| Ctrl/Cmd+S, or **Save** | Write now, without waiting for the pause. Same write: the mote's `modoc` property and nothing else |
| `t+N` stepper | Resolve the whole note against a simulation preview N steps out. The label flips **Real** to **Projected**; nothing is written |
| **Apply t+N…** | Commit that projection to the graph. Asks first, and is disabled at t+0 |
| Esc | Close the note menu or the `@` picker |

Save and Apply are two buttons on purpose: saving a note must never be a
way to write simulation results.

### Smoke test

`scripts/mora_smoke_test.py` is Mora's equivalent of the UI smoke test
above, and exists for the same reason -- the half of the app pytest
cannot reach. It drives a real window against a seeded throwaway garden
and checks that the note renders, that `\val` and `\mote` resolve (and
visibly fail when they can't), that both `\graph` forms draw, that a link
moves the data panel and back returns, that the stepper projects without
writing, that Save writes only the note, and that no uncaught JS errors
were logged. The entry points *into* Mora belong to the main window, so
they are checked by `scripts/monarch_smoke_test.py` instead, with `open_mora`
stubbed -- an automated suite has no business spawning a second app.

Run the two one at a time. Both drive real windows with synthetic
gestures, and the timing-sensitive checks flake when they compete.

```
.venv/bin/python scripts/mora_smoke_test.py
```

Prints PASS/FAIL per check and exits non-zero if anything failed. Never
touches real data: `GARDENS_DIR` and the webview storage dir are both a
fresh temp directory.

---

# Monastery -- the calendar

A third window, over the same gardens: `./run_monastery.py`, or
`.venv/bin/python run_monastery.py`. Same stack and the same terms as
Mora -- pywebview over a local HTML/CSS/JS frontend, its own process,
its own garden connection, opening and closing independently of the
graph window.

```
./run_monastery.py                      # the default garden
./run_monastery.py --garden reactor     # a specific garden
./run_monastery.py --focus <mote id>    # open with one mote selected
```

### Date bindings

Monastery does not have a "calendar entry" type, because MonOSS does not
have types. It reads **every** property whose value parses as a date and
offers each such key as a **binding** -- so a garden where meetings say
`when`, tasks say `due` and events say `starts` shows all three at once,
with no migration and nothing declared. The sidebar lists every binding
it found with a count, and toggling one off empties the grid of the motes
it placed without touching them.

This is a read-side heuristic only. Creating a mote from the calendar
always writes `date`: reading is general because a garden is, but a new
date has one right home.

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
permissiveness a broken note gets in Mora.

Templates and scenario overrides are never placed, even when dated: they
hold replacement values rather than real data.

### The window

| Area | |
|---|---|
| Week | a 24-hour grid; overlapping events are packed into side-by-side lanes, all-day ones pin to a row above it |
| Month | six weeks of day cells, three chips each and a `+N more` that opens that week |
| Bindings rail | the mini calendar, the date-binding toggles, and a species filter |
| Detail panel | which property placed the mote, its properties (any holding an interval marked), and its relationships |

| Control | Action |
|---|---|
| `‹` / `›`, or ← / → | Previous / next week or month |
| **Today**, or `T` | Back to the current week |
| `W` / `M` | Week or month view |
| Click any empty slot | Open the draft popover there |
| Enter, in the draft's name field | Create the mote |
| Esc | Close the draft, else the detail panel |

The draft popover's type picker lists the garden's own **species** --
whatever motes are pointed at by `instance_of` -- rather than a fixed set
of kinds, and picking one adds that relationship to the new mote.

### Live updates

Nothing in MonOSS pushes: Monarch, the CLI and the MCP server
all write the same file and none of them announces it. So Monastery
**polls**, every few seconds, and only while its window has focus -- an
edit made anywhere else appears on the next tick without a refresh. A
poll never runs while the draft popover is open, so it cannot reload the
inputs out from under someone typing into them.

### Ports

The third window means a third port. pywebview pins its server to 42001
unless told otherwise (Monarch), Mora asks for 42002, and
Monastery asks for **42003**, each falling back to an ephemeral port if
theirs is taken. This is not tidiness: the process that loses that race
still points its window at the winner's address, so it comes up showing
another app's page against its own bridge -- which is exactly what
happened to Mora once. `.webview_data_monastery/` is its own WebKit
storage for the same reason.

### Smoke test

`scripts/monastery_smoke_test.py`, the same instrument as Mora's and for
the same reason. It drives a real window against a seeded throwaway
garden -- seeded relative to *today*, since the week view draws whatever
week it opens on -- and checks that motes place under every binding, that
an all-day span pins across each of its days, that a recurrence expands,
that overlapping events share the column, that a dated template stays
off the grid, that the toggles filter, that clicking an event opens the
panel, that the draft popover writes a mote dated with the reserved key,
and that an edit from another process arrives on the next poll. It holds
42001 and 42002 for the whole run, because a Monastery that asks for the
wrong port must fail visibly rather than load another app's page.

Run the smoke tests one at a time. All five drive real windows with
synthetic gestures, and the timing-sensitive checks flake when they
compete.

```
.venv/bin/python scripts/monastery_smoke_test.py
```

Prints PASS/FAIL per check and exits non-zero if anything failed. Never
touches real data: `GARDENS_DIR` and the webview storage dir are both a
fresh temp directory.

---

# Momentum -- tasks

A fourth window, over the same gardens: `./run_momentum.py`, or
`.venv/bin/python run_momentum.py`. Same stack and the same terms as the
other two -- pywebview over a local HTML/CSS/JS frontend, its own
process, its own garden connection, opening and closing independently.

```
./run_momentum.py                      # the default garden
./run_momentum.py --garden reactor     # a specific garden
./run_momentum.py --focus <mote id>    # open with one mote selected
```

Three tabs: **Board** (cards in columns by status), **Days** (the week,
with an undated pool beside it) and **Focus** (a timer, today's queue,
and the session log).

### It reserves no property names

Momentum has no "task" type, because MonOSS has no types -- and unlike
the calendar's `date`/`duration`, it adds no reserved names either. It
*discovers* which of a garden's own properties mean what, the same way
Monastery discovers its date bindings:

| Role | Recognized by |
|---|---|
| status, priority | a key holding a short label out of a small repeating set |
| estimate | a key holding a duration -- `5h`, `90m`, `1d`, or bare minutes |
| when | any key whose value parses as a date (Monastery's bindings) |

The sidebar lists every candidate with a count and marks the one in use;
clicking another switches to it. A garden that calls its status `stage`
and its values `icebox`/`shipped` gets a board with `icebox` and
`shipped` columns and needs no migration to get it. Your choice is
remembered by the window (localStorage), never written to the garden --
so it survives a restart without becoming data anyone else has to
respect. Switching gardens forgets it, since another garden's properties
are another question.

A `1d` estimate means **eight hours** -- a day of work, not of clock.

### Scheduling against the calendar

Drag a card from the pool onto a day. Momentum asks
`core/calendar.py` for the earliest gap that day with room for the
task's estimate, reading the very occurrences Monastery draws, and writes
that time to the date property the mote already uses -- or, for an
unscheduled one, to the key the garden already organizes itself by.

So the two windows are the same data seen twice. A task scheduled here is
an event there, immediately, with nothing to sync. All-day motes (a `due`
date) mark a day without blocking it, so a deadline never makes a day
unschedulable. A day with no room left still accepts the task, at the
start of the working day, and says so -- refusing to schedule something
is not the window's decision to make.

Drop a card back on the pool to unschedule it. The working day defaults
to 09:00–17:00 and the capacity bar to 6h/day; a mote named `Momentum`
carrying `work_start`, `work_end` or `capacity_hours` overrides them, and
being an ordinary mote it is editable from any window.

### The timer

Pick something from today's queue, press Begin (or space). Nothing is
written while it runs -- the countdown is the window's own state, the
same preview/commit split simulation keeps. Finishing it (or **Log &
finish**) writes one ordinary mote: an instance of a `Work Session`
species carrying `date` and `duration`, pointing at the task with
`about`. It logs the time actually elapsed, not the nominal length.

Because that mote is ordinary, the session shows up in Monastery and
Monarch without either knowing Momentum exists. The streak and heatmap
are computed by reading those motes back, so deleting one anywhere is
simply a session that no longer happened.

### Keys

| Key | Does |
|---|---|
| B / D / F | Board, Days, Focus |
| Space | Start or pause the timer (Focus) |
| ← / → | Previous or next week (Days) |
| T | Back to this week (Days) |
| Esc | Close the inline input, else the detail panel |

### Ports

A fourth window means a fourth port: `momentum/app.py` asks for
**42004** (42001 Monarch, 42002 Mora, 42003 Monastery), falling back to
an ephemeral one if taken -- the same collision the others guard against,
for the same reason. `.webview_data_momentum/` is its own WebKit storage.

### Smoke test

`scripts/momentum_smoke_test.py`, the same instrument as the others. It
drives a real window against a seeded throwaway garden -- seeded relative
to *today*, since the week and the queue are both today's -- and checks
that the columns are the garden's own statuses, that the sidebar offers
every candidate key, that dragging a card rewrites the status property,
that dropping one on a day lands it in the free slot *after* the
standup, that an estimate becomes a duration, that the pool unschedules,
and that the timer writes a session only when it finishes. It holds
42001, 42002 and 42003 for the whole run.

```
.venv/bin/python scripts/momentum_smoke_test.py
```

---

# Monolith -- the table

A fifth window, over the same gardens: `./run_monolith.py`, or
`.venv/bin/python run_monolith.py`. Same stack and the same terms as the
other three -- pywebview over a local HTML/CSS/JS frontend, its own
process, its own garden connection, opening and closing independently.

```
./run_monolith.py                      # the default garden
./run_monolith.py --garden reactor     # a specific garden
./run_monolith.py --view <mote id>     # open on a saved view
```

Every mote is a row. It is the window for the two things the other four
are bad at: editing a lot of motes at once, and asking a question of the
whole garden rather than of one mote.

### The columns are your garden's own

Nothing is declared. Monolith reads the garden and offers, in the rail:

| Column kind | Where it comes from |
|---|---|
| a property key | every key any mote carries, commonest first, with its fill count |
| `→ type` / `← type` | every relationship type anyone has drawn, in each direction |
| Name, ID, Species, Links, Updated | things every mote has |
| Instance of, Kinds, Related by | what the graph makes the mote (`core/kinds.py`) |

Click a column to add it to the table; click a header to sort by it.
Clicking a cell opens the editor, and its options are that column's own
distinct values -- so a `status` column offers `todo`/`doing`/`blocked`
without anyone having declared a status type, and a column of forty
different values just gives you a text field. What you type is read as
JSON where it parses (`api_common.coerce`), so `5` is stored as a number.

### Asking the graph, not just the properties

The Where bar builds a query out of conditions joined by AND or OR. A
condition can name a property (`estimate > 3`), a builtin (`Updated
after 2026-08-01`), a relationship (`→ depends_on includes "Schema
translation"`) or the graph itself (`Kinds includes species`, `Related
by is empty` -- which is how you list orphans). The language is
`core/query.py`, not the window's; the window only edits it.

### Editing many motes at once

Tick rows (shift-click for a range, ⌘A for everything the query
matched). Then **editing any cell inside the selection writes every
selected mote** -- the footer says so while a selection is up. The bulk
bar adds: set a property, clear one, link, and delete.

Clicking a cell *outside* the selection edits that one mote and leaves
the selection alone. That is deliberate: the alternative silently writes
forty motes because a click missed.

### Writing relationships in bulk

**Link…** relates the whole selection at once: chained in row order, all
pointing at the first row, or all pointing at one mote you search for.
**relations** opens the matrix -- the visible rows on both axes, one
click per edge, painting whichever type is selected above it. Clicking a
linked cell erases that edge. It covers the first 24 rows and says so.

### Saved views

A view is an ordinary mote carrying the query as its `query` property --
the only reserved name this window adds. So it syncs, it shows up in
Monarch, and a Mora note can link to it. The rail lists them with a
one-line summary of what each asks. The query you are editing right now
is *not* a mote: it lives in `localStorage`, per garden, so the window
reopens where you left it.

Deleting rows asks for a typed confirmation. Nothing else here does,
because everything else is an edit you can make back.

### Keys

| Key | Does |
|---|---|
| ⌘A | Select every row the query matched |
| Shift-click | Select a range of rows |
| Double-click a row | Inspect it: every property and every relationship |
| R | Show or hide the relationship matrix |
| Esc | Close the popover, else clear the selection |

### Ports

A fifth window means a fifth port: `monolith/app.py` asks for **42005**
(42001 Monarch, 42002 Mora, 42003 Monastery, 42004 Momentum, 42006
Motion), falling
back to an ephemeral one if taken -- the same collision the others guard
against, for the same reason. `.webview_data_monolith/` is its own
WebKit storage.

### Smoke test

`scripts/monolith_smoke_test.py`, the same instrument as the others. It
drives a real window against a seeded throwaway garden and checks that
the discovered keys really become columns, that a condition built by
clicking the Where bar filters, that editing one cell inside a
three-row selection writes all three motes, that the bulk bar chains a
selection into relationships, that a matrix click writes an edge and a
second one erases it, and that saving a view leaves a mote behind. It
holds 42001-42004 for the whole run.

```
.venv/bin/python scripts/monolith_smoke_test.py
```

---

# Motion -- the simulator

A sixth window, over the same gardens: `./run_motion.py`, or
`.venv/bin/python run_motion.py`. Same stack and the same terms as the
other five.

```
./run_motion.py                        # the default garden
./run_motion.py --garden reactor       # a specific garden
./run_motion.py --focus <mote id>      # open with one mote selected
```

This is where simulation lives. It used to be Monarch's — a settings
popover, a bottom scrub bar and a runs panel — and it is gone from there
entirely: Monarch is a graph editor, and a window that has to be
"committed or discarded" before you can edit anything is not one.

### The three modes

| Mode | What it is for |
|---|---|
| **Build** | placing motes, writing rules, drawing `applies_to` |
| **Run** | one scenario, played and scrubbed tick by tick |
| **Compare** | two scenarios side by side, with the delta shaded between their traces |

The left rail holds scenarios and saved runs; the dock at the bottom is
the timeline; the right panel is Rules or Inspector. Everything except
the run itself is local — dragging the playhead never re-solves, because
the whole run came back in one payload.

### What a node is, and where it sits

Nothing is declared. A mote carries no coordinates and no simulation
role, so `motion/payload.py` derives four kinds from what core already
answers:

| Kind | What makes it one |
|---|---|
| **rule** | an `equation` plus an `applies_to` (`core/kinds.py`) |
| **free** | it declares `freedoms` — the solver may determine something on it |
| **varied** | the active scenario overrides it |
| **fixed** | everything else |

`varied` depends on which scenario is selected, so the same mote changes
colour when you pick one. Template and override motes are never drawn:
`core/simulation.py` leaves both out of a solve, so a node for one would
be a node no run can move. Layout is yours — drag a node and it stays,
per garden, in `localStorage`.

### Ticks, not seconds

The axis counts **ticks**, and a tick is one re-solve of the equation
set. `core/simulation.py` has no timestep, no seconds and no
integration, so the dock says `ticks` out loud rather than implying a
clock it doesn't have. The solver list comes from
`core/solver_registry.py` — today `linear` and `newton`.

Two things the design this window was built from shows, and which are
deliberately **not** here: a hardware-in-the-loop rail, and a menu of ODE
integrators (RK45, BDF2, `dt` in milliseconds, a tolerance). Neither has
a backend — there is no device layer at all — and a live-looking readout
that is really a sine wave is worse than an absent one. Both attach at
named seams when they exist: `motion/payload.py::tick_axis` for a real
time axis, the solver registry for an integrator.

### Committing

`Commit tick N` re-runs N ticks and writes the result. It never trusts
the numbers the preview handed the frontend — the only values that reach
the database are ones Python recomputed. A scenario run can't be
committed at all (a scenario answers what *would* happen); `Save` logs
one for later review without touching the graph.

A sixth window means a sixth port: `motion/app.py` asks for **42006**,
falling back to an ephemeral one if taken. `.webview_data_motion/` is
its own WebKit storage. A commit here is not seen by an open Monarch
until it refetches — the same rule every adapter lives under.

### Smoke test

`scripts/motion_smoke_test.py`, the same instrument as the others. It
drives a real window against a seeded throwaway garden and checks that
Run really draws lanes, that stepping the dock really moves the numbers
on the node cards, that a preview writes nothing and Commit writes
exactly the tick scrubbed to, that picking a scenario re-runs and
refuses to commit, that Compare draws two boards, and that editing an
equation in the panel writes the rule mote. It holds 42001-42005 for the
whole run.

```
.venv/bin/python scripts/motion_smoke_test.py
```

Run it alone — the pywebview window suites interfere with each other,
and two at once produce failures that are about the display rather than
the code.

---

# Gardens -- the garden manager

`./run_gardens.py`, or `.venv/bin/python run_gardens.py`. Also reachable
from every one of the six windows: the garden dropdown's "Manage
gardens…" footer, plus Monarch's File menu and its Tools → Sync…, which
opens it straight at the selected garden's sync settings.

```
./run_gardens.py                        # the list, nothing selected
./run_gardens.py --garden reactor       # opens with that row selected
./run_gardens.py --garden reactor --sync
```

Garden management used to be Monarch's alone — a modal that only that
window had — so making a garden from Motion meant opening Monarch first.
This is that modal pulled out into a window all seven processes share,
and Monarch's copy is gone rather than duplicated.

### Why a separate window, and a separate process

A manager living inside an app would be managing, among others, the
garden that app is holding open. That is why Monarch's modal had to
refuse to delete the current garden, and refuse to delete the last one:
it *was* a garden window, and both cases would have left it standing in
nothing. This window holds no garden connection at all (every read opens
a short-lived one and closes it), so both are ordinary operations here.

The cost of being its own process is that it cannot switch the garden of
the app that opened it. It doesn't try: each row's **Open in** launches
any of the six windows already on that garden, and an app that is
already running finds a newly created garden in its own switcher, which
reads the directory every time it opens.

### What it does

| Action | Notes |
|---|---|
| New garden | Name validated in the field against the same rule `core/db.py` enforces |
| Rename | Moves the `.db` and its sidecars, and follows the default-garden pointer. Refused while the garden syncs — `core/sync.py` keys the shared folder by garden name, so renaming would point this device at an empty op stream while other devices kept writing to the old one. Stop syncing, rename, set it up again |
| Delete | The file is moved aside, not erased, so the toast's **Undo** is real (`restore_garden`). A window already open on that garden keeps working off its own file handle until it closes |
| Export as JSON | `core/garden_io.py`'s export document, through a native save dialog |
| Import | Always lands in a *new* garden — `garden_io` refuses to merge into one already holding any of the incoming mote ids — so the name is asked for before the file. A failed import removes the garden it just made, rather than leaving an empty one under the name you wanted |
| Sync | The folder-sync controls that used to be Monarch's Tools → Sync…, now per garden: choose the shared folder, the auto-sync flag that gates each app's lifecycle sync points (open, garden switch, close), a manual **Sync now**, and **Stop syncing**, which keeps the oplog so turning it back on resumes |

### Ports

42007, after the six windows' 42001-42006, falling back to an ephemeral
one if taken. `.webview_data_gardens/` is its own WebKit storage. Opening
it twice is allowed rather than guarded: pywebview cannot raise and focus
a window belonging to another process, so a menu item that silently did
nothing because the first one was buried would be the worse failure.

### Smoke test

`scripts/gardens_smoke_test.py`, the same instrument as the others. It
drives a real window against a throwaway gardens directory and checks
that the list renders every garden with its counts, that selecting one
fills the detail pane, that the name modal catches an invalid or
duplicate name before Python sees it, that rename really moves the
garden, that delete offers a working Undo, and that sync can be turned
on and off per garden. Opening an app and opening a native file dialog
are stubbed in the page — one would spawn a process the run can't reap,
the other would block on a dialog nobody will answer.

```
.venv/bin/python scripts/gardens_smoke_test.py
```

Run it alone — the pywebview window suites interfere with each other,
and two at once produce failures that are about the display rather than
the code.

# Running the desktop UI

The UI is a native window (via `pywebview`) wrapping a local HTML/CSS/JS
frontend — no browser tab, no local web server. Entry point: `run_ui.py`.

The graph is drawn on a `<canvas>` by `ui/frontend/graph.js`, which owns the
force layout, camera and pointer gestures. There is no graph library: motes
are clustered into radial sectors by species, and the camera pans to a mote
on selection rather than re-fitting the whole view. Fonts are vendored under
`ui/frontend/vendor/fonts/`, so nothing is fetched at runtime.

### Graph interactions

| Gesture | Action |
|---|---|
| Left-click a mote | Select it |
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
their relative layout on paste. The topbar's garden button opens a quick
switch/create dropdown; its "Manage gardens…" item (also reachable from
the File menu) opens the fuller modal that Export/Delete/Import still
live in, since those need real per-row buttons a narrow menu item can't
give them.

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

Deleting motes/relationships or a garden surfaces a toast with an "Undo"
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
File hosts "New garden…", "Import garden…", "Export garden as JSON…"
and "Export to Calendar" -- the garden import/export items are the same
flows the gardens modal's own Import button and per-row Export button
run, sharing one implementation so the two entry points can't drift
(they replaced "soon" stubs that advertised features already working
elsewhere in the app); View hosts "Reset
view", a "Hide/Show controls" toggle for the always-docked
top-right panel (species legend, relationship filters, depth, reset
view) -- the first way to reclaim that screen space, persisted across
restarts -- and "Advanced view settings…" (below); Tools hosts the
simulation step count and the Simulate…/Exit
simulation trigger for the same non-blocking preview/scrub/commit bar
described above. A handful of items (Export as CSV, Print graph,
Timeline view, Group by type, Automations, Webhooks, API access) are
inert placeholders with a small "soon" badge, staged for future
expansion -- clicking one just toasts an explanation rather than doing
nothing silently. A stub is only allowed to describe something that
genuinely doesn't exist yet: if the app can already do it somewhere,
the menu item points at that instead.

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
web address or a local file path) and its detail panel row gets an
"Open" (↗) button that launches it via the OS's default handler.
`core/links.py::classify_link` decides which: a web address goes to the
stdlib `webbrowser` module, while a local file is instead handed to the
OS's native "open with the registered default app" mechanism
(`ui/api.py::_open_with_os_default`), not `webbrowser.open()`, since
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

### Exporting dated motes to Google Calendar

The File menu's "Export to Calendar" item pushes every mote with a `date`
property in the current garden to Google Calendar (one-way, manual).
Deleting a dated mote, or removing its `date`, deletes the corresponding
event on the next export. See `docs/google-calendar.md` for the reserved
properties involved, the one-time Google Cloud setup only you can do,
and what happens on first export.

### Reserved names

`docs/architecture.md` lists every property key and relationship type
MonOSS recognizes by convention (`url`, `date`, `duration`, `equation`,
`instance_of`, ...). Any property or relationship using one of these
gets a small dot badge next to its name in the detail panel, so it
doesn't look like ordinary data -- hover it for a tooltip. This is
purely a visual cue; the name still works exactly like any other
property/relationship, editable and deletable like anything else.

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

If that prints a version with no errors, `python3 run_ui.py` should open the
MonOSS window.

Note: if your `python3` on `PATH` resolves to a non-system interpreter (e.g.
a conda/miniforge install), `--system-site-packages` won't see the system
`gi` package — it only reaches the site-packages of the interpreter the venv
was built from. Point the venv at the real system interpreter explicitly,
e.g. `/usr/bin/python3 -m venv --system-site-packages .venv`.

## Desktop launcher

A user-level launcher lives at `~/.local/share/applications/monoss.desktop`
(not tracked in this repo — it's machine-specific, pointing at an absolute
`.venv` path) so MonOSS shows up in the regular application menu instead of
needing a terminal. Its icon is `assets/icon.svg`. Regenerate it with:

```
cat > ~/.local/share/applications/monoss.desktop <<EOF
[Desktop Entry]
Type=Application
Name=MonOSS
Comment=Personal knowledge-graph organizer
Exec=$(pwd)/.venv/bin/python $(pwd)/run_ui.py
Icon=$(pwd)/assets/icon.svg
Terminal=false
Categories=Utility;
StartupWMClass=MonOSS
EOF
update-desktop-database ~/.local/share/applications/
```

## Regression testing

`pytest` covers `core/`/`ui/api.py` in isolation, but doesn't touch the
actual GTK/WebKit window. `scripts/ui_smoke_test.py` drives the real running
app via pywebview's `evaluate_js` (mote/relationship CRUD, label rendering,
layout position stability, search/browse/keyboard-nav, garden-switch
isolation, species picking and templating, filters, depth, previewing
and committing a time-based simulation from the non-blocking sim bar
(including the changed-mote highlight ring, the inspector's read-only
projected values, an edit being blocked mid-preview, scrubbing to and
committing an earlier tick, and Discard writing nothing), live box-select, middle-drag pan, the context menu's
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
controls toggle and its persistence, the Tools menu swapping Simulate…/
Exit simulation with the real preview state, a "soon" stub toasting
instead of doing nothing, a hidden species' instances actually dropping
out of the physics loop instead of just not being drawn, the advanced
view settings panel (a per-relationship-type force slider reaching the
layout and persisting, a custom species colour surviving a garden switch,
colour-by-property-value giving different values different colours and no
value grey, and "Reset every view setting" restoring every default),
attractor mode (entering it, dragging an attractor so the species' motes
follow, clicking a mote *not* opening the panel, and leaving), and the
Export to Calendar item -- with
`api.export_to_google_calendar` itself stubbed, since the real OAuth/
network path needs a real Google account and has no place in an
automated, offline suite) against a throwaway temp `GARDENS_DIR` and
webview storage dir — never touches real data. Run it by hand after UI
changes:

```
.venv/bin/python scripts/ui_smoke_test.py
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
closes independently of the graph window.

```
./run_mora.py                       # the default garden
./run_mora.py --garden reactor      # a specific garden
./run_mora.py --note <mote id>      # straight to one mote's note
```

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
| Ctrl/Cmd+S, or **Save** | Write the source back to the mote's `modoc` property -- and nothing else |
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
were logged.

```
.venv/bin/python scripts/mora_smoke_test.py
```

Prints PASS/FAIL per check and exits non-zero if anything failed. Never
touches real data: `GARDENS_DIR` and the webview storage dir are both a
fresh temp directory.

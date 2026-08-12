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
| Left-drag empty canvas | Box-select (hold Shift to add to the current selection) |
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

Motes highlight live as a box-select drag grows over them, not only once
the mouse is released. Copying more than one mote preserves their
relative layout on paste. Gardens (switch/create/export/import/delete)
live in a modal opened from the topbar's garden button, not a dropdown.

Shift+clicking edges builds up a relationship selection the same way
box-select does for motes, just one at a time rather than by dragging a
rectangle -- there's no edge-aware box-select. Selecting relationships
and selecting motes are mutually exclusive, same as a single relationship
and a single mote already were: picking one kind clears the other. The
detail panel's bulk delete button (and Delete/Backspace) act on whichever
kind is currently selected.

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

The topbar's "Export to Calendar" button pushes every mote with a `date`
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
and inspecting a relationship by clicking its edge, shift+click building
a multi-relationship selection and bulk-deleting it, the "New mote here"
form opening near the click, creating a mote by pressing Enter in that
form instead of clicking Create, that form closing on an outside click,
the search palette's `key = value` exact-match query, a hidden species'
instances actually dropping out of the physics loop instead of just not
being drawn, and the Export to Calendar button -- with
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

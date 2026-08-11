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
| Left-click empty canvas | Deselect |
| Left-drag a mote | Move + pin it |
| Left-drag empty canvas | Box-select (hold Shift to add to the current selection) |
| Ctrl+Left-drag from a mote | Connect it to another mote |
| Middle-drag | Pan |
| Right-click a mote | Context menu: Copy, Pin/Unpin, Delete (acts on the whole selection if the mote is part of one) |
| Right-click empty canvas | Context menu: New mote here, Paste |
| Ctrl/Cmd+C / Ctrl/Cmd+V | Copy / paste the current selection (Ctrl+V pastes at the viewport center) |
| Arrow keys | Select whichever visible neighbour sits closest to that screen direction |
| Delete / Backspace | Delete the current selection |
| Esc | Close the frontmost menu/panel, or deselect if nothing else is open |
| Scroll | Zoom |

Motes highlight live as a box-select drag grows over them, not only once
the mouse is released. Copying more than one mote preserves their
relative layout on paste. Gardens (switch/create/export/import/delete)
live in a modal opened from the topbar's garden button, not a dropdown.

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
isolation, species picking and templating, filters, depth, running a
simulation, live box-select, middle-drag pan, the context menu's
copy/paste/pin, multi-select copy/paste, Ctrl+C/Ctrl+V, bulk delete,
Esc/Del, arrow-key navigation, and the gardens modal's list/switch/
create/delete) against a throwaway temp `GARDENS_DIR` and webview storage
dir — never touches real data. Run it by hand after UI changes:

```
.venv/bin/python scripts/ui_smoke_test.py
```

Prints PASS/FAIL per check and exits non-zero if anything failed.

One check samples the canvas pixels directly. That is not paranoia: the
graph's render loop was once entirely dead while every other check passed,
because hit-testing works off node coordinates rather than what was drawn —
clicking, dragging and connecting all succeeded against a blank canvas.

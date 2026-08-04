# Running the desktop UI

The UI is a native window (via `pywebview`) wrapping a local HTML/CSS/JS
frontend — no browser tab, no local web server. Entry point: `run_ui.py`.

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
isolation) against a throwaway temp `GARDENS_DIR` and webview storage dir —
never touches real data. Run it by hand after UI changes:

```
.venv/bin/python scripts/ui_smoke_test.py
```

Prints PASS/FAIL per check and exits non-zero if anything failed.

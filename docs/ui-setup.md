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

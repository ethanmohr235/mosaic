#!/bin/sh
# Put Mosaic (the console, run_console.py) in the desktop's application
# menu: a user-level .desktop entry pointing at this checkout's .venv
# (docs/ui-setup.md -> Desktop launcher). Machine-specific, so it is
# written here rather than tracked.
#
# It writes ~/.local/share/applications/monoss.desktop -- the same file
# Monarch's launcher used -- so an old Monarch entry, whose script is
# gone, becomes Mosaic. Run it again after moving the checkout.
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
PYTHON="$ROOT/.venv/bin/python"
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
ENTRY="$APPS/monoss.desktop"

if [ ! -x "$PYTHON" ]; then
  echo "no $PYTHON -- build the venv first (docs/ui-setup.md)" >&2
  exit 1
fi

mkdir -p "$APPS"
# StartupWMClass must match the window's title, the `name` console/app.py
# passes to launch_window, or the menu entry won't own the running window.
cat > "$ENTRY" <<EOF
[Desktop Entry]
Type=Application
Name=Mosaic
Comment=Personal knowledge-graph organizer
Exec=$PYTHON $ROOT/run_console.py
Icon=$ROOT/assets/icon.svg
Terminal=false
Categories=Utility;
StartupWMClass=Mosaic
EOF

if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$APPS" || true
fi
echo "wrote $ENTRY"

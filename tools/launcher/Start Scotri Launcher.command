#!/bin/bash
# macOS equivalent of "Start Scotri Launcher.bat": launches the Scotri Service
# Launcher GUI detached, so the Terminal window this was double-clicked from
# can go away instead of babysitting the process.
#
# Double-click it in Finder, or run it from a shell. First time only, if Finder
# refuses to open it, mark it executable:  chmod +x "Start Scotri Launcher.command"
set -euo pipefail

LAUNCHER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCOTRI_ROOT="$(cd "$LAUNCHER_DIR/../.." && pwd)"
VENV_PY="$SCOTRI_ROOT/services/.venv/bin/python"

if [ ! -x "$VENV_PY" ]; then
  echo "Cannot find the services venv interpreter at:"
  echo "  $VENV_PY"
  echo
  echo "Create it first, from $SCOTRI_ROOT/services:"
  echo "  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
  echo
  read -r -p "Press Return to close this window. "
  exit 1
fi

# tkinter is a separate install on some Pythons (e.g. a Homebrew python without
# python-tk) - say so here rather than letting the GUI die silently.
if ! "$VENV_PY" -c "import tkinter" >/dev/null 2>&1; then
  echo "This Python has no tkinter, so the launcher GUI cannot start:"
  echo "  $VENV_PY"
  echo
  echo "Install Tk support for it (Homebrew: brew install python-tk), or point"
  echo "the venv at a Python that has it."
  echo
  read -r -p "Press Return to close this window. "
  exit 1
fi

mkdir -p "$LAUNCHER_DIR/state/logs"
nohup "$VENV_PY" "$LAUNCHER_DIR/scotri_launcher.py" \
  >>"$LAUNCHER_DIR/state/logs/launcher.log" 2>&1 &
disown || true

# Close the Terminal window Finder opened for us. Needs Automation permission
# the first time; if it is declined the window just stays open - harmless.
osascript -e 'tell application "Terminal" to close (every window whose name contains "Start Scotri Launcher")' \
  >/dev/null 2>&1 || true

exit 0

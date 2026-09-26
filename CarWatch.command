#!/usr/bin/env bash
# Open the CarWatch dashboard (macOS): start it if it isn't running, then open
# the browser.
#
# CarWatch.app, which setup.sh builds next to this file, runs this with no
# Terminal window - that is the one to double-click, or keep in the Dock.
# Double-clicking this file works too, but opens a Terminal window.
#
# The work is done by carwatch/launch.py, the same launcher the Windows icon
# runs. The dashboard runs in the background, writing to logs/dashboard.log,
# and stops by itself once no CarWatch page has been open for five minutes -
# so closing the browser closes it. A collection in progress finishes first.
#
# Run ./setup.sh once first. (Written for macOS but not yet run on a real Mac.)

cd "$(dirname "$0")" || exit 1

if [[ ! -x .venv/bin/python ]]; then
  echo "CarWatch is not set up in $(pwd) yet. Run ./setup.sh in this folder first." >&2
  exit 1
fi

exec .venv/bin/python -m carwatch.launch "$@"

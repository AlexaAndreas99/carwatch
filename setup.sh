#!/usr/bin/env bash
# One-time setup for CarWatch on a Mac (or Linux).
#
#   ./setup.sh
#
# Creates the virtual environment, installs the dependencies and the Chromium
# that olx.ro needs, and leaves you a config.yaml to fill in. Safe to run
# again: whatever is already in place is left alone.
#
# --skip-browser installs no Chromium. autovit and mobile.de still work; olx
# does not, because CloudFront refuses plain HTTP requests.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$root"

skip_browser=0
[ "${1:-}" = "--skip-browser" ] && skip_browser=1

echo "CarWatch setup - $root"
echo

# --------------------------------------------------------------- Python
python="$(command -v python3 || true)"
if [ -z "$python" ]; then
  echo "python3 is not installed. On a Mac: install it from https://www.python.org/downloads/" >&2
  echo "or, with Homebrew:  brew install python@3.12" >&2
  exit 1
fi

version="$("$python" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
if ! "$python" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
  echo "Found Python $version, but CarWatch needs 3.11 or newer." >&2
  exit 1
fi
echo "Python $version - ok"

# ----------------------------------------------------- virtual environment
if [ -x ".venv/bin/python" ]; then
  echo "Virtual environment already there."
else
  echo "Creating the virtual environment (.venv)..."
  "$python" -m venv .venv
fi

echo "Installing dependencies..."
.venv/bin/python -m pip install --upgrade pip --quiet
.venv/bin/python -m pip install -r requirements.txt --quiet
echo "Dependencies - ok"

# ------------------------------------------------------------- Chromium
# Into the project rather than ~/.cache, so a scheduled run finds it too
# (see carwatch/adapters/browser.py).
if [ "$skip_browser" = "1" ]; then
  echo "Skipping Chromium (--skip-browser). olx will not collect."
else
  echo "Installing Chromium for olx (a few hundred MB, once)..."
  PLAYWRIGHT_BROWSERS_PATH="$root/.playwright" .venv/bin/python -m playwright install chromium
  echo "Chromium - ok"
fi

# --------------------------------------------------------------- config
fresh=0
if [ -f config.yaml ]; then
  echo "config.yaml already there - left alone."
else
  cp config.example.yaml config.yaml
  fresh=1
  echo "Created config.yaml from the example."
fi

# The Mac launcher and the shell wrappers have to be executable; a clone does
# not necessarily carry that bit.
chmod +x CarWatch.command run.sh schedule.sh setup.sh 2>/dev/null || true

echo
echo "Done. Next:"
if [ "$fresh" = "1" ]; then
  echo "  1. Open config.yaml and paste your search URLs over PASTE_URL_HERE."
  echo "  2. Double-click CarWatch.command in Finder (first time: right-click > Open)."
else
  echo "  Double-click CarWatch.command in Finder (first time: right-click > Open)."
fi
echo
echo "A schedule is optional and off by default - set it on the Runs page."

#!/usr/bin/env bash
# One-time setup for CarWatch on a Mac (or Linux).
#
#   ./setup.sh
#
# Creates the virtual environment, installs the dependencies and the Chromium
# that olx.ro needs, and leaves you a config.yaml - searches are added from
# the dashboard. Safe to run again: whatever is already in place is left alone.
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

# ------------------------------------------------------------ CarWatch.app
# What you double-click on a Mac. A .command file always opens a Terminal
# window; an AppleScript app does not, so this one just runs CarWatch.command
# out of sight. Built here rather than shipped: an app made on your own Mac
# carries no quarantine flag, so Gatekeeper opens it without the right-click
# dance. Rebuilt every time, and it finds the project from its own location,
# so it keeps working as long as it stays in this folder.
build_app() {
  local script
  script="$(mktemp -t carwatch-app)"
  cat > "$script" <<'APPLESCRIPT'
on run
	set root to do shell script "dirname " & quoted form of POSIX path of (path to me)
	try
		do shell script "cd " & quoted form of root & " && ./CarWatch.command"
	on error message
		display dialog message buttons {"OK"} default button 1 with title "CarWatch" with icon stop
	end try
end run
APPLESCRIPT
  # Checked step by step: inside an `if`, which is where this is called,
  # bash's `set -e` does not stop at a failure.
  rm -rf CarWatch.app
  osacompile -o CarWatch.app "$script" || { rm -f "$script"; return 1; }
  rm -f "$script"

  # The CarWatch icon in place of the generic script one. Cosmetic, so a
  # failure here leaves the default icon rather than failing setup.
  if [ -f carwatch.png ] && command -v sips >/dev/null && command -v iconutil >/dev/null; then
    local iconset size
    iconset="$(mktemp -d -t carwatch-icon)/carwatch.iconset"
    mkdir -p "$iconset"
    for size in 16 32 128 256 512; do
      sips -z "$size" "$size" carwatch.png --out "$iconset/icon_${size}x${size}.png" >/dev/null
      sips -z $((size * 2)) $((size * 2)) carwatch.png --out "$iconset/icon_${size}x${size}@2x.png" >/dev/null
    done
    iconutil -c icns "$iconset" -o CarWatch.app/Contents/Resources/applet.icns       && touch CarWatch.app       || echo "Kept the default icon for CarWatch.app."
    rm -rf "$(dirname "$iconset")"
  fi
  return 0
}

app=0
if command -v osacompile >/dev/null; then
  if build_app; then
    app=1
    echo "CarWatch.app - ok"
  else
    echo "Could not build CarWatch.app; CarWatch.command still opens the dashboard."
  fi
fi

if [ "$app" = "1" ]; then
  open_it="Double-click CarWatch.app in this folder (drag it to the Dock to keep it handy)."
else
  open_it="Run ./CarWatch.command to open the dashboard."
fi

echo
echo "Done. Next:"
echo "  $open_it"
if [ "$fresh" = "1" ]; then
  echo "  Then add your first configuration in the dashboard: paste the search"
  echo "  links from Autovit, OLX or mobile.de. No file to edit."
fi
echo
echo "A schedule is optional and off by default - set it on the Runs page."

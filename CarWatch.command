#!/usr/bin/env bash
# Double-click in Finder to open the CarWatch dashboard (macOS).
#
# Starts the dashboard if it isn't running, then opens it in the browser. The
# Terminal window that opens is the dashboard: closing it stops it. If the
# dashboard was already running, it just opens the browser.
#
# Run ./setup.sh once first; it installs everything and makes this file
# executable. (Written for macOS but not yet run on a real Mac.)

cd "$(dirname "$0")" || exit 1
port=8009
url="http://127.0.0.1:$port/"

if curl -fs -o /dev/null --max-time 2 "$url"; then
  open "$url"
  exit 0
fi

if [[ ! -x .venv/bin/python ]]; then
  echo "CarWatch is not set up in $(pwd) yet."
  echo "Run ./setup.sh in this folder first."
  read -r -p "Press Enter to close."
  exit 1
fi

.venv/bin/python -m carwatch.web --port "$port" &
server=$!

ready=0
for _ in $(seq 1 40); do
  sleep 0.5
  if curl -fs -o /dev/null --max-time 2 "$url"; then
    open "$url"
    ready=1
    break
  fi
done

# Keep the window up when it never came alive, so the reason stays readable.
# On a fresh machine that reason is almost always a config.yaml still holding
# PASTE_URL_HERE.
if [[ $ready -eq 0 ]]; then
  echo
  echo "The dashboard did not start - the message above says why."
  read -r -p "Press Enter to close."
  exit 1
fi

echo
echo "CarWatch dashboard is running at $url — close this window to stop it."
wait "$server"

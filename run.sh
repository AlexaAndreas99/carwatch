#!/usr/bin/env bash
# CarWatch collector wrapper (macOS / Linux).
#
# Activates the local venv, runs a collection, and appends everything to a log
# file. This is what cron should call, so the crontab entry is one line and the
# output isn't lost.
#
#   ./run.sh                                       # all enabled searches
#   ./run.sh --status                              # config + DB state
#   ./run.sh --all --site autovit
#   ./run.sh --search "Nissan Qashqai 2025 4x4 Tekna"
#
# Exit code is the collector's own: 0 all ok, 2 bad config,
# 5 at least one source blocked or errored.

set -uo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
python="$root/.venv/bin/python"

if [[ ! -x "$python" ]]; then
  cat >&2 <<EOF
No virtualenv at $python
Create it first:
    python3 -m venv .venv
    .venv/bin/python -m pip install -r requirements.txt
EOF
  exit 1
fi

# cron runs with a minimal environment and an arbitrary working directory, so
# move to the project root to make config.yaml (and its relative db_path) resolve.
cd "$root" || exit 1

mkdir -p "$root/logs"
log="$root/logs/collect-$(date +%Y-%m).log"

# --jitter <minutes>: wait a random 0..N minutes before collecting. Consumed
# here, never passed to Python.
#
# A cron job firing at exactly 08:00:00 every day is the most machine-looking
# traffic pattern there is, and it means every CarWatch user hits the same sites
# at the same instant. Spreading the start is politer and costs nothing.
#
# --scheduled: started by the schedule. Consumed here, and the run's result is
# written to logs/last-scheduled-run.json, as run.ps1 does on Windows.
jitter_minutes=0
scheduled=0
args=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --jitter) jitter_minutes="${2:-0}"; shift 2 ;;
    --scheduled) scheduled=1; shift ;;
    *) args+=("$1"); shift ;;
  esac
done
if [[ ${#args[@]} -eq 0 ]]; then
  args=(--all)
fi
set -- "${args[@]}"

{
  printf '\n===== %s  run.sh %s =====\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
} >> "$log"

wait_s=0
if [[ "$jitter_minutes" -gt 0 ]]; then
  wait_s=$(( RANDOM % (jitter_minutes * 60) ))
fi

# A scheduled run says when it will actually collect, once it has picked its
# random delay (as run.ps1 does). Removed when the run finishes.
current="$root/logs/scheduled-run.json"
if [[ "$scheduled" -eq 1 ]]; then
  at_s=$(( $(date +%s) + wait_s ))
  # BSD date (macOS) takes -r, GNU date takes -d @.
  collect_at="$(date -r "$at_s" '+%Y-%m-%dT%H:%M:%S' 2>/dev/null || date -d "@$at_s" '+%Y-%m-%dT%H:%M:%S')"
  printf '{"started": "%s", "collect_at": "%s"}\n' \
    "$(date '+%Y-%m-%dT%H:%M:%S')" "$collect_at" > "$current"
fi

if [[ "$wait_s" -gt 0 ]]; then
  printf 'jitter: sleeping %ss before collecting\n' "$wait_s" >> "$log"
  sleep "$wait_s"
fi

# Tee so an interactive run still prints while a cron run leaves a trace.
# PIPESTATUS keeps the collector's exit code rather than tee's.
"$python" -m carwatch.collect "$@" 2>&1 | tee -a "$log"
code=${PIPESTATUS[0]}

printf -- '----- exit code %s -----\n' "$code" >> "$log"

if [[ "$scheduled" -eq 1 ]]; then
  printf '{"finished": "%s", "exit_code": %s}\n' "$(date '+%Y-%m-%dT%H:%M:%S')" "$code" \
    > "$root/logs/last-scheduled-run.json"
  rm -f "$current"
fi
exit "$code"

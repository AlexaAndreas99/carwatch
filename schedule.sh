#!/usr/bin/env bash
# Install (or remove) a daily CarWatch collection in cron, on macOS or Linux.
#
#   ./schedule.sh                # daily at 08:00
#   ./schedule.sh --at 19:30     # daily at 19:30
#   ./schedule.sh --status       # show the current crontab entry
#   ./schedule.sh --remove       # take it out again
#   ./schedule.sh --print        # print the line without installing it
#
# Exit code is 0 on success. The collector's own exit code (0 ok, 5 a source
# was blocked or errored) ends up in the log, not in cron's.

set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
marker="# carwatch-daily"
at="08:00"
jitter=20
mode="install"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --at)     at="${2:?--at needs a time like 08:00}"; shift 2 ;;
    --jitter) jitter="${2:?--jitter needs a number of minutes}"; shift 2 ;;
    --status) mode="status"; shift ;;
    --remove) mode="remove"; shift ;;
    --print)  mode="print"; shift ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

if [[ ! "$at" =~ ^([01][0-9]|2[0-3]):([0-5][0-9])$ ]]; then
  echo "Could not read --at '$at'. Use 24-hour HH:MM, e.g. 08:00 or 19:30." >&2
  exit 2
fi
hour="${BASH_REMATCH[1]#0}"; hour="${hour:-0}"
minute="${BASH_REMATCH[2]#0}"; minute="${minute:-0}"

# The wrapper already logs and cd's to the project root; cron only needs to
# call it. Redirecting to /dev/null is safe *because* run.sh keeps its own log.
line="$minute $hour * * * $root/run.sh --all --jitter $jitter >/dev/null 2>&1  $marker"

current="$(crontab -l 2>/dev/null || true)"
without="$(printf '%s\n' "$current" | grep -vF "$marker" || true)"

case "$mode" in
  print)
    echo "$line"
    ;;

  status)
    existing="$(printf '%s\n' "$current" | grep -F "$marker" || true)"
    if [[ -z "$existing" ]]; then
      echo "No CarWatch cron entry installed."
      echo "Install it with:  ./schedule.sh --at 08:00"
    else
      echo "Installed cron entry:"
      echo "  $existing"
      echo
      echo "Logs: $root/logs/"
    fi
    ;;

  remove)
    if printf '%s\n' "$current" | grep -qF "$marker"; then
      printf '%s\n' "$without" | crontab -
      echo "Removed the CarWatch cron entry."
    else
      echo "No CarWatch cron entry to remove."
    fi
    ;;

  install)
    if [[ ! -x "$root/run.sh" ]]; then
      chmod +x "$root/run.sh"
    fi
    printf '%s\n%s\n' "$without" "$line" | grep -v '^$' | crontab -
    echo "Installed — CarWatch will collect daily at $at."
    echo "  entry : $line"
    echo "  logs  : $root/logs/"
    echo
    echo "Check it with :  ./schedule.sh --status"
    echo "Run it now    :  ./run.sh --all"
    echo "Remove it     :  ./schedule.sh --remove"
    echo
    echo "macOS note: cron needs Full Disk Access for your terminal, or use"
    echo "launchd instead — see the README."
    ;;
esac

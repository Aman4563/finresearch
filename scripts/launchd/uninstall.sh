#!/bin/bash
# Remove the FinResearch LaunchAgents (#507; docs/USAGE.md "Start at login").
#
#   scripts/launchd/uninstall.sh               unload the three jobs and delete their plists
#   scripts/launchd/uninstall.sh --print       show what would be done; change nothing
#   --label-prefix NAME                        default in.finresearch
#
# Unloading a job STOPS the service it runs (that is what `launchctl bootout` does): after this the API, the monitor
# and the web app are down until you start them by hand (docs/USAGE.md "Running the app") or install again. A copy
# you started by hand is not a launchd job and is not touched. Safe to run again; data/ and the logs stay.
#
# Environment (tests): LAUNCH_AGENTS_DIR (default ~/Library/LaunchAgents), LAUNCHCTL (default launchctl).
set -euo pipefail

DEST="${LAUNCH_AGENTS_DIR:-$HOME/Library/LaunchAgents}"
LAUNCHCTL="${LAUNCHCTL:-launchctl}"
PREFIX="in.finresearch"
MODE="uninstall"

while [ $# -gt 0 ]; do
  case "$1" in
    --print) MODE="print" ;;
    --label-prefix) PREFIX="${2:?--label-prefix needs a name}"; shift ;;
    -h | --help) sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "uninstall: unknown option $1 (see --help)" >&2; exit 64 ;;
  esac
  shift
done
case "$PREFIX" in *[!A-Za-z0-9._-]* | '') echo "uninstall: a label may hold letters, digits, dot, dash and underscore only" >&2; exit 64 ;; esac

DOMAIN="gui/$(id -u)"
for svc in healthcheck web api; do
  label="$PREFIX.$svc" plist="$DEST/$PREFIX.$svc.plist"
  if [ "$MODE" = "print" ]; then
    echo "# would run: launchctl bootout $DOMAIN/$label (if loaded), then delete $plist (if present)"
    continue
  fi
  if "$LAUNCHCTL" print "$DOMAIN/$label" >/dev/null 2>&1; then
    "$LAUNCHCTL" bootout "$DOMAIN/$label"
    echo "uninstall: unloaded $label (its service is stopped)"
  else
    echo "uninstall: $label is not loaded"
  fi
  if [ -f "$plist" ]; then
    rm -f "$plist"
    echo "uninstall: deleted $plist"
  fi
done

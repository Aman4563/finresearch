#!/bin/bash
# Install the FinResearch LaunchAgents (#507; docs/USAGE.md "Start at login"): the API + monitor, the web app and
# the five-minute health check start at login, and launchd restarts the first two when they exit.
#
#   scripts/launchd/install.sh                 write the three plists and load the jobs that are not loaded yet
#   scripts/launchd/install.sh --print         show the plists that would be written; change nothing
#   scripts/launchd/install.sh --no-load       write the plists only (they load at the next login)
#   scripts/launchd/install.sh --allow-move-stale-postmaster-pid
#                                              opt in: the health check may MOVE a stale postmaster.pid aside
#                                              (default: it only tells you; see scripts/healthcheck.py)
#   --pgdata DIR                               Postgres data directory (default /opt/homebrew/var/postgresql@17)
#   --label-prefix NAME                        default in.finresearch (jobs NAME.api, NAME.web, NAME.healthcheck)
#
# Safe to run again (idempotent): an unchanged plist is not rewritten and a loaded job is left exactly as it is.
# It never stops, kills or restarts anything. Loading a job while the app already runs (started by hand) starts no
# second copy: the job's launcher waits until the port is free (run-service.sh). When a plist changed for a job
# that is already loaded, the two commands that apply it are printed for you to run; they restart that service.
# It reads no .env, no ~/.pgpass and no Keychain item.
#
# Environment (tests): LAUNCH_AGENTS_DIR (default ~/Library/LaunchAgents), LAUNCHCTL (default launchctl).
set -euo pipefail
shopt -u patsub_replacement 2>/dev/null || true  # bash >= 5.2: keep "&" literal in ${var//pattern/replacement}

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
DEST="${LAUNCH_AGENTS_DIR:-$HOME/Library/LaunchAgents}"
LAUNCHCTL="${LAUNCHCTL:-launchctl}"
PREFIX="in.finresearch"
PGDATA="/opt/homebrew/var/postgresql@17"
MOVE_STALE=0
MODE="install"
LOAD=1

while [ $# -gt 0 ]; do
  case "$1" in
    --print) MODE="print" ;;
    --no-load) LOAD=0 ;;
    --allow-move-stale-postmaster-pid) MOVE_STALE=1 ;;
    --pgdata) PGDATA="${2:?--pgdata needs a directory}"; shift ;;
    --label-prefix) PREFIX="${2:?--label-prefix needs a name}"; shift ;;
    -h | --help) sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "install: unknown option $1 (see --help)" >&2; exit 64 ;;
  esac
  shift
done
case "$PREFIX" in *[!A-Za-z0-9._-]* | '') echo "install: a label may hold letters, digits, dot, dash and underscore only" >&2; exit 64 ;; esac

DOMAIN="gui/$(id -u)"
BASE_PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

# the folders of the tools the jobs run, when they are not on the template's PATH already (e.g. node from nvm)
TOOL_DIRS=""
for tool in uv pnpm node pg_isready caffeinate; do
  if found="$(command -v "$tool" 2>/dev/null)" && [ -n "$found" ]; then
    dir="$(dirname "$found")"
    case ":$BASE_PATH:$TOOL_DIRS" in *":$dir:"*) ;; *) TOOL_DIRS="$TOOL_DIRS$dir:" ;; esac
  else
    echo "install: warning: '$tool' is not on your PATH now; the jobs will look in $BASE_PATH" >&2
  fi
done

xml() {  # a value as XML text
  local s="$1"
  s="${s//&/&amp;}"
  s="${s//</&lt;}"
  s="${s//>/&gt;}"
  printf '%s' "$s"
}

render() {  # render <service>: the filled plist on stdout
  local t
  t="$(cat "$HERE/in.finresearch.$1.plist.template")"
  t="${t//__LABEL_API__/$(xml "$PREFIX.api")}"
  t="${t//__LABEL_WEB__/$(xml "$PREFIX.web")}"
  t="${t//__LABEL__/$(xml "$PREFIX.$1")}"
  t="${t//__REPO__/$(xml "$REPO")}"
  t="${t//__HOME__/$(xml "$HOME")}"
  t="${t//__TOOL_DIRS__/$(xml "$TOOL_DIRS")}"
  t="${t//__PGDATA__/$(xml "$PGDATA")}"
  t="${t//__UID_DOMAIN__/$(xml "$DOMAIN")}"
  t="${t//__MOVE_STALE_PID__/$MOVE_STALE}"
  printf '%s\n' "$t"
}

SERVICES="api web healthcheck"

if [ "$MODE" = "print" ]; then
  for svc in $SERVICES; do
    echo "# ---- would write $DEST/$PREFIX.$svc.plist"
    render "$svc"
  done
  exit 0
fi

mkdir -p "$DEST" "$REPO/data/logs" "$REPO/data/state"
CHANGED=""
for svc in $SERVICES; do
  plist="$DEST/$PREFIX.$svc.plist"
  new="$(render "$svc")"
  if [ -f "$plist" ] && [ "$new" = "$(cat "$plist")" ]; then
    echo "install: $plist is up to date"
    continue
  fi
  printf '%s\n' "$new" >"$plist.tmp"
  if command -v plutil >/dev/null 2>&1 && ! plutil -lint "$plist.tmp" >/dev/null; then
    rm -f "$plist.tmp"
    echo "install: the rendered $svc plist is not a valid property list; nothing was installed for it" >&2
    exit 1
  fi
  mv "$plist.tmp" "$plist"
  CHANGED="$CHANGED $svc"
  echo "install: wrote $plist"
done

if [ "$LOAD" -eq 1 ]; then
  for svc in $SERVICES; do
    label="$PREFIX.$svc" plist="$DEST/$PREFIX.$svc.plist"
    if "$LAUNCHCTL" print "$DOMAIN/$label" >/dev/null 2>&1; then
      case " $CHANGED " in
        *" $svc "*)
          echo "install: $label is loaded with its OLD settings and was left running. To apply the new plist (this restarts it):"
          echo "           launchctl bootout $DOMAIN/$label && launchctl bootstrap $DOMAIN '$plist'"
          ;;
        *) echo "install: $label is already loaded: left as it is" ;;
      esac
    else
      "$LAUNCHCTL" bootstrap "$DOMAIN" "$plist"
      echo "install: loaded $label"
    fi
  done
else
  echo "install: --no-load: the jobs load at your next login, or now with: launchctl bootstrap $DOMAIN <plist>"
fi

cat <<EOF

Done. If the app was already running (started by hand), it still is: the new jobs wait for its ports and take over
when it stops. From now on restart a service with ONE command (never start a second copy by hand):
  launchctl kickstart -k $DOMAIN/$PREFIX.api     # API + monitor
  launchctl kickstart -k $DOMAIN/$PREFIX.web     # web app
Is it running?   launchctl print $DOMAIN/$PREFIX.api | grep -E 'state|pid|last exit'
                 curl -s http://127.0.0.1:8710/api/health ; tail -n 5 '$REPO/data/logs/healthcheck.log'
Remove:          scripts/launchd/uninstall.sh
EOF

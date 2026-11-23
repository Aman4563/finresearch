#!/bin/bash
# The launchd jobs' launcher (#507; docs/USAGE.md "Start at login"). launchd runs
#   run-service.sh api          the API and the monitor on 127.0.0.1:8710 (as by hand: caffeinate -i uv run finresearch serve)
#   run-service.sh web          the web app on 127.0.0.1:3100 (as by hand: pnpm start -H 127.0.0.1 -p 3100, in web/;
#                               here under caffeinate -i, see below)
#   run-service.sh healthcheck  one run of scripts/healthcheck.py
#
# Never a second copy. `finresearch serve` starts the monitor before it binds its port, so a second copy would run
# a scheduler for a moment and could claim tonight's jobs before failing on "address already in use". So when the
# port already answers (a copy started by hand, or the old instance still shutting down), this launcher starts
# nothing: it says so once, looks again every FINRESEARCH_PORT_WAIT_S seconds (15) and starts the service when the
# port is free. launchd sees one long-running job, not a crash loop, and nothing is ever killed here.
#
# The API's own log goes to data/logs/serve.log, rotated by size (`serve --log-file`); what launchd captures of this
# launcher and of a crash before logging starts goes to data/logs/serve.launchd.log; the web app's output goes to
# data/logs/web.launchd.log (files only launchd writes: #523).
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SERVICE="${1:-}"
API_PORT="${FINRESEARCH_API_PORT:-8710}"
WEB_PORT="${FINRESEARCH_WEB_PORT:-3100}"
WAIT_S="${FINRESEARCH_PORT_WAIT_S:-15}"

log() { printf '%s run-service %s: %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$SERVICE" "$*"; }

# true when something accepts TCP connections on 127.0.0.1:<port> (bash's /dev/tcp: no lsof, no nc)
port_in_use() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }

wait_for_port() {
  local port="$1" told=0
  while port_in_use "$port"; do
    if [ "$told" -eq 0 ]; then
      log "port $port already answers (a copy started by hand?): not starting a second one; waiting until it is free"
      told=1
    fi
    sleep "$WAIT_S"
  done
  [ "$told" -eq 0 ] || log "port $port is free: starting"
}

case "$SERVICE" in
  api)
    mkdir -p "$REPO/data/logs"
    wait_for_port "$API_PORT"
    cd "$REPO" || exit 1
    exec caffeinate -i uv run finresearch serve --port "$API_PORT" --log-file "$REPO/data/logs/serve.log"
    ;;
  web)
    mkdir -p "$REPO/data/logs"
    wait_for_port "$WEB_PORT"
    cd "$REPO/web" || exit 1
    # under caffeinate, like the API: macOS asks for folder access on behalf of the job's MAIN process. With node
    # there, a checkout under ~/Desktop (or Documents, Downloads) raised "node would like to access files in your
    # Desktop folder" and the web app hung, silent, until someone answered; a node upgrade asks again (#523).
    exec caffeinate -i pnpm start -H 127.0.0.1 -p "$WEB_PORT"
    ;;
  healthcheck)
    cd "$REPO" || exit 1
    # --no-project: any Python uv can find, without the app's environment (the check must work when the app does not)
    exec uv run --no-project --quiet python "$REPO/scripts/healthcheck.py"
    ;;
  *)
    echo "usage: run-service.sh api|web|healthcheck" >&2
    exit 64
    ;;
esac

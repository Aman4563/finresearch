#!/usr/bin/env bash
# Live dashboard smoke check: render each page in headless Chrome against a running API (`finresearch serve`) and a
# running dashboard (`pnpm --dir web start`), and fail on any uncaught browser error.
#   scripts/smoke_web.sh <run_id>
set -euo pipefail
RUN_ID=${1:?usage: scripts/smoke_web.sh <run_id>}
WEB=${WEB:-http://127.0.0.1:3100}
CHROME=${CHROME:-"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"}
OUT=$(mktemp -d)
trap 'rm -rf "$OUT"' EXIT
fail=0
# the live run page keeps an SSE stream open, so it is checked with a hard timeout instead of network idle
for page in "/" "/runs" "/usage" "/runs/$RUN_ID/report" "/runs/$RUN_ID"; do
  name=$(echo "$page" | tr '/' '_')
  timeout 45 "$CHROME" --headless=new --disable-gpu --user-data-dir="$OUT/profile$name" --enable-logging=stderr \
    --virtual-time-budget=10000 --dump-dom "$WEB$page" >"$OUT/dom$name.html" 2>"$OUT/log$name.txt" || true
  if grep -q "Uncaught" "$OUT/log$name.txt"; then
    echo "FAIL $page"; grep "Uncaught" "$OUT/log$name.txt" | head -3; fail=1
  elif grep -q "This page couldn" "$OUT/dom$name.html"; then
    echo "FAIL $page (error boundary)"; fail=1
  else
    echo "ok   $page"
  fi
done
exit $fail

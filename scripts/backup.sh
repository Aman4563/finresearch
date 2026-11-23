#!/usr/bin/env bash
# Nightly backup of the FinResearch database (docs/BACKUPS.md; roadmap §E Phase 1 item 6).
#
# Writes data/backups/finresearch-<UTC timestamp>.dump (pg_dump custom format, compressed) plus a manifest of the
# document files under data/docs (path, bytes), checks the dump with pg_restore --list, and keeps the newest $KEEP
# dumps. data/ is gitignored, so backups never reach git. The database holds no secrets (they are in the macOS
# Keychain; rows keep references), and the script refuses to dump if `finresearch secrets check` finds any left.
#
# Exit codes: 0 done; 2 a missing tool or a bad KEEP; 3 REFUSED, plaintext secrets found; 4 the off-machine copy
# failed (the local dump is kept); 5 the database cannot be reached; 6 the secrets check itself could not run; any
# other code is the failing command's own (pg_dump, pg_restore). Every run, good or bad, writes its outcome to
# <state dir>/backup_status (key=value lines: at, outcome, exit, last_ok_at, last_ok_dump), which the app's
# data-health pass reads (dq.misc.backup_check, #507), so a refused or failed backup shows in the morning brief.
#
# Environment (all optional):
#   FINRESEARCH_DATABASE_URL  database to dump; default: the app's own setting (read through finresearch.config,
#                             so the URL is never printed or copied here)
#   BACKUP_DIR                default: <repo>/data/backups
#   DOCS_DIR                  default: <repo>/data/docs
#   KEEP                      how many dumps to keep (default 14)
#   FINRESEARCH_BACKUP_OFFSITE_DIR  when set (env or the app's settings), also push encrypted off-machine copies of
#                             the database and its forward-only tables there (`finresearch backup offsite`, #375);
#                             a failed copy keeps the local dump and exits 4
#   FINRESEARCH_STATE_DIR     where backup_status is written; default: the app's own setting, else <repo>/data/state
#   FINRESEARCH_CLI           the `finresearch` command to run (tests); default: uv run --quiet finresearch
set -euo pipefail
umask 077

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP_DIR="${BACKUP_DIR:-$REPO/data/backups}"
DOCS_DIR="${DOCS_DIR:-$REPO/data/docs}"
KEEP="${KEEP:-14}"
PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"  # launchd starts with a minimal PATH (uv: ~/.local/bin)

finresearch_cli() {
  if [ -n "${FINRESEARCH_CLI:-}" ]; then "$FINRESEARCH_CLI" "$@"; else uv run --quiet finresearch "$@"; fi
}

# ---- the run's outcome, for the data-health card (#507). Written on every exit, also a failed one.
STATE_DIR="${FINRESEARCH_STATE_DIR:-}"
if [ -z "$STATE_DIR" ]; then
  STATE_DIR="$(cd "$REPO" && uv run --quiet python -c 'from finresearch.config import get_settings; print(get_settings().state_dir)' 2>/dev/null)" || STATE_DIR=""
fi
[ -n "$STATE_DIR" ] || STATE_DIR="$REPO/data/state"
STATUS="$STATE_DIR/backup_status"
OUTCOME="failed"  # until a step says otherwise: an unexpected exit is a failed backup, never an unknown one
OK_DUMP=""
TMP=""

write_status() {
  local rc="$1" now ok_at="" ok_dump=""
  now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  if [ -n "$OK_DUMP" ]; then
    ok_at="$now" ok_dump="$OK_DUMP"
  elif [ -f "$STATUS" ]; then  # a failed run keeps the last good backup on record
    ok_at="$(sed -n 's/^last_ok_at=//p' "$STATUS" | head -n 1)"
    ok_dump="$(sed -n 's/^last_ok_dump=//p' "$STATUS" | head -n 1)"
  fi
  mkdir -p "$STATE_DIR"
  {
    printf 'at=%s\noutcome=%s\nexit=%s\n' "$now" "$OUTCOME" "$rc"
    if [ -n "$ok_at" ]; then printf 'last_ok_at=%s\nlast_ok_dump=%s\n' "$ok_at" "$ok_dump"; fi
  } >"$STATUS.tmp" && mv "$STATUS.tmp" "$STATUS"
}

on_exit() {
  local rc=$?
  [ -z "$TMP" ] || rm -f "$TMP"
  [ "$rc" -ne 0 ] || OUTCOME="ok"
  write_status "$rc" || echo "backup: could not write $STATUS" >&2
}
trap on_exit EXIT

command -v pg_dump >/dev/null || { echo "backup: pg_dump not found" >&2; exit 2; }
command -v pg_restore >/dev/null || { echo "backup: pg_restore not found" >&2; exit 2; }
case "$KEEP" in '' | *[!0-9]* | 0) echo "backup: KEEP must be a positive integer" >&2; exit 2 ;; esac

URL="${FINRESEARCH_DATABASE_URL:-}"
if [ -z "$URL" ]; then
  URL="$(cd "$REPO" && uv run --quiet python -c 'from finresearch.config import get_settings; print(get_settings().database_url)')"
fi
APP_URL="$URL"
URL="${URL/+psycopg2/}"  # SQLAlchemy driver suffixes: pg_dump wants a plain libpq URL
URL="${URL/+psycopg/}"

mkdir -p "$BACKUP_DIR"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)-$$"  # the pid keeps two runs in one second apart
OUT="$BACKUP_DIR/finresearch-$STAMP.dump"
TMP="$OUT.partial"

# secrets live in the Keychain (issue #245): refuse to write a dump while any is still plain text in the database.
# The check's exit code says which case this is (#507): only 1 means plaintext was found; on 12 nights in
# Nov-2026 an unreachable database was logged as "REFUSED: plaintext secrets" and sent the reader the wrong way.
check=0
(cd "$REPO" && finresearch_cli secrets check) || check=$?
case "$check" in
  0) ;;
  1)
    OUTCOME="refused_plaintext_secrets"
    echo "backup: REFUSED: plaintext secrets are still in the database; run 'uv run finresearch secrets migrate --apply'" >&2
    exit 3 ;;
  3)
    OUTCOME="database_unreachable"
    echo "backup: FAILED: cannot reach the database, so nothing was dumped (this is not the plaintext-secrets refusal). Is Postgres running? Try: pg_isready; brew services list; docs/USAGE.md 'If the app or the database is down'" >&2
    exit 5 ;;
  *)
    OUTCOME="secrets_check_failed"
    echo "backup: FAILED: the secrets check could not run (exit $check), so nothing was dumped; run 'uv run finresearch secrets check' to see why" >&2
    exit 6 ;;
esac

pg_dump --format=custom --compress=6 --no-owner --no-privileges --file="$TMP" "$URL"
pg_restore --list "$TMP" >/dev/null  # a truncated or corrupt archive fails here, before it replaces anything
mv "$TMP" "$OUT"
OK_DUMP="$(basename "$OUT")"  # from here on a good local dump exists, whatever happens next

if [ -d "$DOCS_DIR" ]; then
  (cd "$DOCS_DIR" && find . -type f | LC_ALL=C sort | while IFS= read -r f; do
    printf '%s\t%s\n' "$(wc -c <"$f" | tr -d ' ')" "$f"
  done) >"$BACKUP_DIR/finresearch-$STAMP.docs-manifest.tsv"
fi

# rotation: keep the newest $KEEP dumps and their manifests
ls -1 "$BACKUP_DIR"/finresearch-*.dump 2>/dev/null | sort -r | tail -n +"$((KEEP + 1))" | while read -r old; do
  rm -f "$old" "${old%.dump}.docs-manifest.tsv"
done

echo "backup: wrote $OUT ($(du -h "$OUT" | cut -f1)); keeping the newest $KEEP"

OFFSITE="${FINRESEARCH_BACKUP_OFFSITE_DIR:-}"
if [ -z "$OFFSITE" ]; then
  OFFSITE="$(cd "$REPO" && uv run --quiet python -c 'from finresearch.config import get_settings; print(get_settings().backup_offsite_dir or "")')"
fi
if [ -n "$OFFSITE" ]; then
  if ! (cd "$REPO" && FINRESEARCH_DATABASE_URL="$APP_URL" FINRESEARCH_BACKUP_OFFSITE_DIR="$OFFSITE" \
        finresearch_cli backup offsite); then
    OUTCOME="offsite_failed"
    echo "backup: the off-machine copy FAILED (the local dump above is kept); see docs/BACKUPS.md" >&2
    exit 4
  fi
fi

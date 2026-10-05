#!/usr/bin/env bash
# Nightly backup of the FinResearch database (docs/BACKUPS.md; roadmap §E Phase 1 item 6).
#
# Writes data/backups/finresearch-<UTC timestamp>.dump (pg_dump custom format, compressed) plus a manifest of the
# document files under data/docs (path, bytes), checks the dump with pg_restore --list, and keeps the newest $KEEP
# dumps. data/ is gitignored, so backups never reach git.
#
# Environment (all optional):
#   FINRESEARCH_DATABASE_URL  database to dump; default: the app's own setting (read through finresearch.config,
#                             so the URL is never printed or copied here)
#   BACKUP_DIR                default: <repo>/data/backups
#   DOCS_DIR                  default: <repo>/data/docs
#   KEEP                      how many dumps to keep (default 14)
set -euo pipefail
umask 077

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP_DIR="${BACKUP_DIR:-$REPO/data/backups}"
DOCS_DIR="${DOCS_DIR:-$REPO/data/docs}"
KEEP="${KEEP:-14}"
PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"  # launchd starts with a minimal PATH

command -v pg_dump >/dev/null || { echo "backup: pg_dump not found" >&2; exit 2; }
command -v pg_restore >/dev/null || { echo "backup: pg_restore not found" >&2; exit 2; }
case "$KEEP" in '' | *[!0-9]* | 0) echo "backup: KEEP must be a positive integer" >&2; exit 2 ;; esac

URL="${FINRESEARCH_DATABASE_URL:-}"
if [ -z "$URL" ]; then
  URL="$(cd "$REPO" && uv run --quiet python -c 'from finresearch.config import get_settings; print(get_settings().database_url)')"
fi
URL="${URL/+psycopg2/}"  # SQLAlchemy driver suffixes: pg_dump wants a plain libpq URL
URL="${URL/+psycopg/}"

mkdir -p "$BACKUP_DIR"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)-$$"  # the pid keeps two runs in one second apart
OUT="$BACKUP_DIR/finresearch-$STAMP.dump"
TMP="$OUT.partial"
trap 'rm -f "$TMP"' EXIT

pg_dump --format=custom --compress=6 --no-owner --no-privileges --file="$TMP" "$URL"
pg_restore --list "$TMP" >/dev/null  # a truncated or corrupt archive fails here, before it replaces anything
mv "$TMP" "$OUT"

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

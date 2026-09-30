# Backups

The research ledger, decision journal, watches, alerts and the forecast ledger live only in the local Postgres
database. `scripts/backup.sh` takes a nightly `pg_dump` so a disk failure or a bad migration can't lose them
(roadmap §E Phase 1 item 6).

## What the script does

- Writes `data/backups/finresearch-<UTC time>-<pid>.dump`. This is `pg_dump` custom format, compressed, with no owner
  or privileges.
- Checks the archive with `pg_restore --list` before it moves the file into place. A truncated dump never replaces a
  good one.
- Writes `finresearch-<…>.docs-manifest.tsv` next to the dump: the bytes and path of every file under `data/docs`.
  Documents are sha256-addressed and never rewritten in place, so the manifest shows what a restore needs from the
  file backup (Time Machine or similar). The PDFs themselves are not copied.
- Keeps the newest `KEEP` dumps (default 14) and their manifests, and deletes older ones.
- `data/` is gitignored, and the files are created with `umask 077`.

| Variable | Default |
|---|---|
| `FINRESEARCH_DATABASE_URL` | the app's own setting, read through `finresearch.config` (never printed) |
| `BACKUP_DIR` | `<repo>/data/backups` |
| `DOCS_DIR` | `<repo>/data/docs` |
| `KEEP` | `14` |

To run it by hand: `scripts/backup.sh`.

## Nightly with launchd (macOS)

1. Save the following as `~/Library/LaunchAgents/in.finresearch.backup.plist`.
2. Change the two paths if the repo lives somewhere else. The space in "learnings " is intentional.
3. Load it with `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/in.finresearch.backup.plist`.

The job runs at 02:30 local time. launchd runs a missed job when the Mac wakes.

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>in.finresearch.backup</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>/Users/macbookpro/Desktop/learnings /stock research and other infos/finresearch/scripts/backup.sh</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>2</integer><key>Minute</key><integer>30</integer></dict>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    <key>KEEP</key><string>14</string>
  </dict>
  <key>StandardOutPath</key>
  <string>/Users/macbookpro/Desktop/learnings /stock research and other infos/finresearch/data/backups/backup.log</string>
  <key>StandardErrorPath</key>
  <string>/Users/macbookpro/Desktop/learnings /stock research and other infos/finresearch/data/backups/backup.log</string>
</dict>
</plist>
```

- Run it once now: `launchctl kickstart gui/$(id -u)/in.finresearch.backup`.
- Check the output: `tail data/backups/backup.log`.
- Remove the job: `launchctl bootout gui/$(id -u)/in.finresearch.backup`.

The job needs `uv` on its `PATH`, because the script asks the app for the database URL. If `uv` lives elsewhere,
either add its folder to `PATH` or set `FINRESEARCH_DATABASE_URL` in `EnvironmentVariables`.

## Restoring

Restore into a new database first, check it, and only then switch the app over.

```sh
createdb finresearch_restored
psql -d finresearch_restored -c 'CREATE EXTENSION IF NOT EXISTS vector'   # optional: the dump recreates it
pg_restore --no-owner --no-privileges --exit-on-error \
  --dbname=postgresql://localhost/finresearch_restored data/backups/finresearch-<stamp>.dump
psql -d finresearch_restored -c 'SELECT count(*) FROM research_run; SELECT count(*) FROM forecast;'
FINRESEARCH_DATABASE_URL=postgresql+psycopg://localhost/finresearch_restored uv run alembic current
```

To switch, point `FINRESEARCH_DATABASE_URL` at the restored database, or rename the databases while the API is stopped.

## How it is tested

`tests/test_backup.py` covers the round trip against the test database only (`FINRESEARCH_TEST_DATABASE_URL`):

1. Runs the script three times with `KEEP=2` and checks that exactly two dumps remain.
2. Checks the documents manifest.
3. Restores the newest dump into a scratch `<name>_restore_test` database.
4. Compares row counts and a marker row, then drops the scratch database.

The test is skipped in these cases:
- `pg_dump` or `pg_restore` is missing.
- The client is older than the server.
- The test role cannot create databases.

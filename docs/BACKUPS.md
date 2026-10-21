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
- Holds no secrets. Broker credentials and tokens, the opt-in CAS password and notification tokens live in the macOS
  Keychain; the database keeps only references (`{"secret_ref": ...}`). Before dumping, the script runs
  `finresearch secrets check` and refuses (exit 3) if any secret is still stored as plain text; run
  `uv run finresearch secrets migrate --apply` once to move them. A restore therefore needs the same Keychain (the
  login keychain is in Time Machine); on a new Mac, re-enter the broker and notification credentials. Dumps taken
  before the migration still contain the old plaintext: delete them once a new dump exists.

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
    <!-- optional, #375: an existing folder off this Mac's disk; copies are encrypted first -->
    <!-- <key>FINRESEARCH_BACKUP_OFFSITE_DIR</key><string>/Volumes/Backup/finresearch</string> -->
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

## Off-machine copies (#375)

The local dumps sit on the same disk as the database. The quotes, the F&O card ledger, the IPO decision-time
subscription snapshots and the intraday series are recorded forward only and can never be fetched again, so they
also go off this Mac, encrypted.

1. Choose a destination folder that is not on this Mac's disk: an external drive, a NAS share or a cloud-synced
   folder. The folder must already exist; if it is missing (drive not mounted), the copy fails instead of quietly
   writing to a local folder.
2. Create the key once: `uv run finresearch backup init-key`. It is 32 random bytes in the login Keychain
   (`finresearch.secrets`, item `finresearch/<database>/backup/offsite_key`). It is never printed and never
   replaced: a new key would make every existing copy unreadable.
3. **Keep a second copy of the key somewhere that does not depend on this Mac** (see "Key escrow" below). Without
   it, the off-machine copies are useless if the Mac is lost.
4. Set `FINRESEARCH_BACKUP_OFFSITE_DIR=<folder>` in `.env` or in the launchd job's `EnvironmentVariables`.
   `scripts/backup.sh` then also runs `finresearch backup offsite` after the local dump. If the off-machine copy
   fails, the local dump is kept and the script exits 4, so `backup.log` shows the failure.

`finresearch backup offsite` takes its own dumps (so the row counts match the dump exactly; see below):

| Kind | What | Kept at the destination |
|---|---|---|
| `db` | the whole database | newest 7 |
| `tables` | `option_quote`, `card_log`, `subscription_snapshot`, `intraday_series` | newest 14 |
| `vault` (`--vault`) | the vault database (`FINRESEARCH_VAULT_DATABASE_URL`), run once after a load | newest 2 |

Each copy is `<dest>/<kind>/finresearch-<kind>-<UTC time>-<pid>.enc` plus a `.json` manifest. The `.enc` file is
the `pg_dump` archive encrypted with AES-256-GCM in 16 MB frames (`backups/crypto.py`): a wrong key, a changed byte,
or a copy cut short fails to decrypt, and nothing is written. Before the manifest is written, the copy is read back
and must decrypt to the same SHA-256 as the dump. The manifest holds the time, size, SHA-256, a key fingerprint and
the row count of each forward-only table, taken in the same database snapshot as the dump (`pg_dump --snapshot`).
Manifests are not encrypted, so they hold no row data and never name or count the personal tables (portfolio,
journal, connections) that the `db` dump also contains. The vault copy has no row counts, because counting would be a read of the sealed data.
The plain dumps, and the read-back copy used to check them, are written to a private temporary folder under
`data/state` and deleted once pushed (so a run briefly needs about twice the dump size free locally).

`uv run finresearch backup status` exits 1 unless each forward-only table is in a copy at most one day old and a
restore drill passed in the last 92 days. A missing or unreadable destination counts as not fresh.

### Key escrow

The key lives only in this Mac's login Keychain. Time Machine keeps the Keychain, but a lost or stolen Mac takes
both the Keychain and any locally attached backup with it. Store a second copy of the key separately, for example in
a password manager or on paper in a safe. The app has no command that prints the key, so open Keychain Access, find
the item `finresearch/finresearch` with the account `backup/offsite_key`, and copy its password by hand.
On a new Mac, install the saved key with `uv run finresearch backup import-key`. It asks for the base64 key with
hidden input, or reads it from stdin, and never takes it as an argument. It refuses a key that is not 32 bytes and
never replaces an existing key.

## Restore drill (quarterly)

```sh
uv run finresearch backup drill                 # the tables copy; --kind db for the whole database
uv run finresearch backup drill --kind db --scratch finresearch_drill_test --keep-db   # leave it for inspection
```

The drill decrypts the newest copy into a private temporary folder and restores it into a NEW database. The
database name must end in `_test` and must not be the configured or the vault database (default:
`<database>_drill_test`), so a drill can never overwrite data. It then compares each table's row count with the
manifest, appends the result to `<dest>/drills.jsonl`, and drops the scratch database. It exits 1 on any mismatch
and 2 on a refused target. A tables copy is restored without its post-data section: it holds `card_log`'s
append-only trigger but not the trigger's function, which lives in the full dump. Run it every quarter, for
example with a launchd `StartCalendarInterval` on the 1st of January, April, July and October; `backup status`
reports an overdue drill.

## Research batches

Backfills, SVI fits, CSCV/SPA bootstraps and simulations run through `finresearch batch run -- <command>`
(`batchwindow.py`). A job started between 09:00 and 15:45 IST on a trading day is deferred: the command exits 75,
or sleeps until 15:45 with `--wait`. A weekday whose NSE holiday list is unknown counts as a trading day. The job
runs under `taskpolicy -b` (macOS background QoS, which throttles CPU and disk I/O) or `nice -n 15` elsewhere.
`--cpu-seconds` adds a hard CPU-time cap. If the job is still running at 09:00, its process group is stopped
(SIGSTOP) and continued at 15:45.

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

`tests/test_backup_crypto.py` covers the encryption: round trips across frame sizes, a wrong key, a flipped byte, a
copy cut at a frame boundary, and a file that is not a copy. `tests/test_backup_offsite.py` covers the key (created
once, never replaced), pushes to a tmp destination, rotation, a missing destination, and freshness.
`tests/test_backup_drill.py` and `tests/test_backup_cli.py` dump the test database, push, run the drill into a
scratch `*_drill_test` database (matching counts, a forced mismatch, refused targets) and check the exit codes.
`tests/test_backup.py` also runs `backup.sh` with a destination, and with a missing one (exit 4, local dump kept).
`tests/test_batch_window.py` covers the market-hours window, deferral, `--wait`, and the pause and resume.

The database tests are skipped in these cases:
- `pg_dump` or `pg_restore` is missing.
- The client is older than the server.
- The test role cannot create databases.

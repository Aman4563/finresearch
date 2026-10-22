# Using FinResearch

The app (`http://127.0.0.1:3100`) covers everyday use; it needs the API running (`uv run finresearch serve`). This
page lists the command-line equivalents and the jobs that are easier to start from a terminal. Every command has
`--help`.

## Setup details

```bash
# local models for the fallback tier (optional; skipped tasks are marked degraded)
ollama pull qwen3.5:9b && ollama pull qwen3.5:4b && ollama pull qwen3-embedding:0.6b && ollama pull glm-ocr

cp .env.example .env        # optional overrides; never commit .env
uv run finresearch bridge health          # Claude login, local models, memory, limits
uv run finresearch bridge limits          # how much of the Claude plan's 5-hour and 7-day windows is used
uv run finresearch bridge usage --runs 10 # which agent roles spend the plan: cost share, tokens, turns per role
```

## Running the app

```bash
uv run finresearch serve                 # API on http://127.0.0.1:8710 (OpenAPI docs at /api/docs); also runs the monitor
cd web && pnpm build && pnpm start       # app on http://127.0.0.1:3100
```

The monitor inside `serve` runs the scheduled jobs: live refresh in market hours, IPO subscription and listing
checks, the nightly portfolio valuation, fund ranks, the stock peer dataset, disclosures, the statement inbox and
forecast scoring. `uv run finresearch monitor run` runs it without the API.

The after-close market jobs (stock peers, disclosures, IV history) skip NSE holidays. If NSE's holiday list for the
year can't be loaded, they don't run at all (they fail closed): the monitor logs it, raises one warning alert a day,
and tries the list again every hour.

### Logs and retention

```bash
uv run finresearch serve --log-file ../data/logs/serve.log                 # API + monitor logs, rotated by size
cd web && pnpm start 2>&1 | uv run finresearch logpipe ../data/logs/web.log  # the app's output, rotated by size
uv run finresearch prune                                                      # run the retention policy now
```

Each log file is capped at 10 MB and keeps 5 older files (`serve.log.1` … `.5`). A shell redirection
(`> serve.log`) is never rotated, so use the commands above.

Retention (the monitor runs it weekly, early on Monday IST; `monitor/retention.py`):
- HTTP cache (`data/cache/http`): an entry is deleted once it is past the TTL it was cached with. Entries cached
  before the TTL was stored are deleted after 60 days (the longest TTL in use is 45 days).
- Intraday 1-minute series (`intraday_series`) are never deleted (#366): NSE serves only the current session, so
  an old session cannot be fetched again (about 1 MB a year; until #366 rows older than 400 days were deleted).
- Never deleted: research packs and reports, backups (`scripts/backup.sh` keeps the newest 14), documents, run
  transcripts and the validation archive (`data/archive`).

### Backups and research batches

```bash
scripts/backup.sh                                   # nightly local dump; also off-machine when a destination is set
uv run finresearch backup init-key                  # once: the off-machine copies' key, in the Keychain
uv run finresearch backup import-key                # a new Mac: install the escrowed key (hidden prompt / stdin)
uv run finresearch backup offsite                   # encrypted copies of the database and its forward-only tables
uv run finresearch backup offsite --vault           # the vault database, once after a load (keeps 2 copies)
uv run finresearch backup status                    # exit 1 unless every forward-only table has a copy <= 1 day old
uv run finresearch backup drill                     # quarterly: restore the newest copy into a scratch *_test database
uv run finresearch batch run -- uv run finresearch fno backfill --from 2022-01-01   # outside 09:00-15:45 IST only
```

The off-machine destination is `FINRESEARCH_BACKUP_OFFSITE_DIR`, a folder that must already exist (a mounted drive,
NAS share or cloud-synced folder); copies are encrypted on this Mac first. Details: `docs/BACKUPS.md`.
`batch run` exits 75 when it defers a job (`--wait` sleeps until 15:45 IST instead), runs the job at background
CPU and I/O priority, and pauses it while the market is open.

### Which requests write

GET requests don't change your records, the alert inputs or the forecast ledger:
- The portfolio value history (behind the drawdown and drift alerts) is recorded by the monitor's daily pass and by
  `POST /api/portfolio/snapshot`, which the portfolio page calls once every price is in.
- Journal drafts for new trades are created by the monitor (every minute) and by `POST /api/journal/sync`, which the
  journal page calls when it opens.
- A viewed stock or IPO signal is logged in the forecast ledger by `POST /api/signals/{asset}/{instrument}`, which
  the signal views call. `GET` on the same path never logs. The monitor's own signal checks don't log either.
- IPO subscription snapshots are recorded by the monitor's scheduled checks and archive slots.
  `GET /api/watches/{id}/live` only reads.
- The F&O card log (#360) is an append-only audit of what the app showed: every `POST /api/fno/plan`,
  `POST /api/fno/strategy` and `GET /api/fno/{symbol}/suggest` response appends one row (the suggest GET is the one
  F&O read that writes, because a card shown and never logged cannot be recovered). Rows are never updated or
  deleted; `uv run finresearch fno card-log --since <ISO time with zone>` counts the verdicts before and after it.

Some GETs still write caches, by design. They store fetched market data, never your records: the intraday series
archive, the disclosure feed refresh, the ISIN map, the fund cap list, and the HTTP and price caches. The broker OAuth
flow is the one exception that stores state: `GET /api/connections/{key}/login-url` stores the one-time login state,
and the broker's redirect to `GET /api/connections/{key}/callback` stores the token. An OAuth redirect is always a
GET.

## Research reports

```bash
# documents
uv run finresearch docs discover acevector --name "AceVector Limited" --nse-symbol ACEVECTOR   # NSE + SEBI + IR pages
uv run finresearch docs add <pdf-or-url> --company orient-cables --name "Orient Cables (India) Limited" --kind RHP
uv run finresearch docs list
uv run finresearch docs search "largest customer share of revenue" --company orient

# IPOs
uv run finresearch ipo run orient-cables --wait     # full multi-agent run (finds the offer documents first if needed)
uv run finresearch ipo status <run_id>             # steps, models, turns, time and plan-window usage
uv run finresearch ipo render <run_id>             # rebuild the research pack
uv run finresearch evidence regrade                # re-check grade-D (fincalc) citations saved before #265; dry run
uv run finresearch evidence regrade --apply        # write it (back up first: scripts/backup.sh); reversible, idempotent

# listed stocks, mutual funds, bonds
uv run finresearch docs discover infosys --name "Infosys Limited" --nse-symbol INFY --kind stock
uv run finresearch stock run infosys --wait         # verdict BUY / ACCUMULATE / HOLD / REDUCE / AVOID
uv run finresearch fund search "axis midcap direct"
uv run finresearch fund run 120505 --wait
uv run finresearch bond search LTF
uv run finresearch bond run INE027E07998 --wait

uv run finresearch research resume <run_id> --wait  # resume any paused or failed run; finished steps are kept
uv run finresearch mcp config                       # MCP config file for using the tools from Claude Code
```

A finished run writes a research pack to `data/reports/<company>/run-<id>/`:
- folders for offer documents, financial reports, news, major events, valuation and the final report;
- the final report as HTML/PDF, where every figure links to its evidence (page and line, or URL and access time);
- a fact-check log, the claim ledger as Excel/CSV, financial tables and charts.

A report that fails the publish gate is written only as a clearly marked draft (`report_NOT_PUBLISHED.*`) with the
gate's reasons.

### Long runs and a sleeping Mac

A research run takes an hour or more. While a run worker is busy it holds a macOS `caffeinate -is -w <pid>`
assertion so an idle Mac does not sleep (closing the lid still does); turn this off with
`FINRESEARCH_KEEP_AWAKE=false`. Passing failures (the Mac slept, a login refresh collided, the network dropped) are
retried with backoff; when retries run out the run pauses with the reason and resumes by itself. Any other failure
marks the run failed with the reason on its page, and Resume continues from the last finished step.

## Markets and monitoring

```bash
uv run finresearch monitor watch orient-cables     # subscription to the close, allotment, listing, lock-ins
uv run finresearch monitor holidays                # NSE trading and settlement holidays
uv run finresearch fund rank                       # rank every fund within its SEBI category now
uv run finresearch stock peers                     # build the stock peer dataset now (NIFTY 500; slow, polite)
uv run finresearch audit prices                    # cross-check price sources against the exchanges' own figures
uv run finresearch index backfill --from 2013-03-01  # NSE's daily index closes, P/E, P/B, yield, India VIX (#366)
uv run finresearch audit index-eod                 # bhavcopy underlying vs index close; India VIX vs IV30 (no network)
uv run finresearch index tri NIFTY --from 2015-01-01  # APPROXIMATE total-return index from stored closes + yields (#366)
uv run finresearch rates backfill --from 2022-01-01   # FBIL G-sec par curve, T-bill, overnight/term MIBOR (#366; needs the FBIL data-rights decision)
uv run finresearch audit rates                     # 3-month par-curve rate vs FBIL's 3-month T-bill, 15 bp (no network)
uv run finresearch equity backfill --from 2025-09-01  # NSE's full equity bhavcopy: closes, delivery; one file a day (#373)
uv run finresearch audit equity-eod --sample 50    # stored closes vs NSE's history bar on 50 random stock-days (50 requests)
uv run finresearch dq run                          # data-quality checks now (the monitor runs them 21:00 and 08:00 IST, #368)
uv run finresearch dq replay --from 2024-01-01 --to 2026-10-21  # content checks over stored history, read only
uv run finresearch events seed                     # load the curated event calendar (RBI MPC, Budgets, 2024 election, FOMC; #367)
uv run finresearch events check --from 2026-10-22 --to 2026-11-24  # the plan card's tier-1 event check for a window
uv run finresearch fno span backfill --from 2008-01-01  # NSE SPAN scan ranges, one file a month (#370)
uv run finresearch fno span margin-check           # margin estimate vs Groww's calculator: the P4.3 trigger
uv run finresearch fno participants --from 2015-01-01  # participant-wise OI and volume (Client/DII/FII/Pro), 2 files a day (#371)
uv run finresearch fno card-log --since 2026-10-21T18:00:00+05:30   # F&O card verdicts before/after a release (#360)
uv run finresearch fno marks --from 2026-10-01      # missing daily marks of open paper legs from stored bhavcopy days (#383)
uv run finresearch fno review --month 2026-10      # the month's post-trade review in one line (#383; reads only)
uv run finresearch vault backfill --from 2018-01-01 --to 2021-12-31 --max-days 400   # the sealed vault (#365), below
uv run finresearch vault status                    # vault load manifest: days by year and status (no market data)
uv run finresearch vault engine-hash               # the engine hash for the vault pin (PREREG.md §2.7)
```

The sealed 2008-2021 vault (#364, #365) is loaded only into its own database, `FINRESEARCH_VAULT_DATABASE_URL`
(never the app's: the loader refuses a URL naming it, and refuses days outside 2008-2021). Create it once with
`createdb finresearch_vault` and build its tables with `FINRESEARCH_DATABASE_URL=postgresql+psycopg://localhost/finresearch_vault
uv run alembic upgrade head` (a per-command variable: the app keeps its own URL). Load the 2018-2021 tranche first
(COVID; unblocks the stress replays), then 2008-2017, a few hundred days per night with `--max-days`; it keeps NIFTY,
BANKNIFTY and FINNIFTY index futures and options only, no raw zips by default, and derives fno_iv_eod and
fno_contract with the app's code. Lots come from NSE's circulars (`backtest.lots`) and charges from
`fincalc.charges`; a date with an unsourced lot or charge is skipped by the backtest, never priced with a guess.
The vault is read only through `finresearch.vault.guard` (registered studies, logged reads).

NSE data is collected for personal, non-distributed use only (owner decision #362, `finresearch.data_rights`): it
stays in the local database and `data/`. Backfills go through the shared NSE rate limit, stop by themselves between
09:00 and 16:00 IST on weekdays and after two refusals (HTTP 403/429) in a row, and resume where they stopped.

The nightly stock peer build prices peers from NSE's quote and price-history APIs (about 1,000 website requests a
night) until `FINRESEARCH_PEER_PRICE_SOURCE=equity_eod` switches it to the stored full bhavcopy (#373): peers' prices
are then the bhavcopy's official closes and the year-ago close comes from the store, with a quote only to refresh a
stock's issued shares and classification (every 30 days, or at once on a sign of a split or bonus). A stock the
bhavcopy does not list keeps the quote path. Switch only after a year of history is loaded (`equity backfill`), the
data-health card's `equity_eod` close_vs_quote check has matched for a few nights and `audit equity-eod` shows no
mismatch.

## Portfolio

Most portfolio work happens on **Portfolio → Import** in the app: drop CAS PDFs, tradebooks, holdings statements or
an AIS file there. PDF passwords are typed into a masked field and never stored. Which files to download from each
broker, and the automatic inbox folder, are described in [BROKER_SETUP.md](BROKER_SETUP.md).

From a terminal (the password prompt is hidden; run it in your own terminal):

```bash
uv run finresearch portfolio import-cas ~/Downloads/<cas>.pdf            # preview
uv run finresearch portfolio import-cas ~/Downloads/<cas>.pdf --apply    # import
uv run finresearch portfolio import-tradebook ~/Downloads/<orders>.xlsx --apply
uv run finresearch portfolio tax-csv gains.csv --fy 2027                 # realised gains, one row per FIFO disposal
```

## Forecasts and calibration

```bash
uv run finresearch forecasts calibration     # Brier score, skill vs the base rate, hit rate with Wilson intervals
uv run finresearch forecasts resolve         # score every due forecast now (the monitor does this daily)
uv run finresearch forecasts cards           # record every logged F&O card (card_log) as a forecast (#376; the monitor does this hourly)
uv run finresearch forecasts horse-race      # the pre-registered card horse race on resolved cards (#377; --window, --json); read only
<<<<<<< HEAD
uv run finresearch forecasts skill           # the live skill monitor per method x event kind x underlying (#378; --json); read only
=======
uv run finresearch forecasts horse-race --source replay --window seen   # the same race on one replayed card per stored session, 2022-26 (addendum A1; design | seen | tuesday); read only, run outside market hours
>>>>>>> origin/main
```

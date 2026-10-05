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
```

## Running the app

```bash
uv run finresearch serve                 # API on http://127.0.0.1:8710 (OpenAPI docs at /api/docs); also runs the monitor
cd web && pnpm build && pnpm start       # app on http://127.0.0.1:3100
```

The monitor inside `serve` runs the scheduled jobs: live refresh in market hours, IPO subscription and listing
checks, the nightly portfolio valuation, fund ranks, the stock peer dataset, disclosures, the statement inbox and
forecast scoring. `uv run finresearch monitor run` runs it without the API.

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
```

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
```

# Architecture

FinResearch is a single-user app that runs entirely on one Mac. The invariants every change must keep are in
[dev/ENGINEERING_HANDOFF.md](dev/ENGINEERING_HANDOFF.md).

```
   Next.js app (web/, 127.0.0.1:3100)          finresearch CLI (cli.py)
                    └──────────── FastAPI (api/, 127.0.0.1:8710) ─────────── monitor (scheduled jobs)
                                               │
   research:  orchestrator ── agents ── Claude Bridge ── MCP server (the agents' only tools)
   markets:   adapters (NSE, BSE, AMFI, SEBI, FBIL, XBRL) ── fincalc ── signals ── alerts
   personal:  portfolio ── wealth ── suggest (profile, rules, journal)
                                               │
              Postgres 17 + pgvector  ·  immutable document store  ·  data/ (gitignored)
```

## Principles

- The model reads and writes; Python computes and verifies. All figures come from `fincalc` using `Decimal`.
- Primary documents beat secondary sources. Every number carries a citation: document page and line, or URL and
  access time. Interim and unverified figures are labelled as such.
- Nothing is silently truncated or invented: tools paginate, engines refuse over-long inputs, and a missing value
  is `None` with a reason, never 0.
- Portfolio, statement, AIS, journal, wealth and household data stay local and are never sent to a model. The only
  personal inputs a model sees are the IPO profile fields the advisor uses (`suggest.profile.ADVISOR_FIELDS`:
  capital per IPO, risk appetite, horizon, tax slab, category, max position, typed holdings and notes, rules), and
  the profile setting `local_suggestion` replaces even that with a rule-based suggestion written locally.

## Claude Bridge (`bridge/`)

Every model task goes through one interface with three tiers:

1. **Claude Code CLI** under your own Claude login, with structured JSON output. It records the plan's 5-hour and
   7-day window usage so runs slow down before a limit, and backs off after a limit hit.
2. **The same CLI on an API key**, if one is configured (off by default).
3. **Local Ollama models**, tuned for 16 GB: one generation at a time, 9B → 4B fallback when memory is short, schema
   validation with repair turns, loop-safe OCR. Local results are marked degraded, tasks needing the web, tools or
   long context are refused rather than faked, and a degraded run never issues a final verdict.

## Research pipeline (`orchestrator/`, `agents/`, `mcp_server/`, `verify/`, `render/`)

A resumable, budget-aware pipeline per research kind (IPO, stock, fund, bond): a planner, research streams,
adversarial verifiers, bull and bear analysts, a synthesiser and a completeness critic. Agents work only through the
FinResearch MCP tools: documents, sections, search, tables, `fincalc`, live market data and the claim ledger.

Every finding is saved as an atomic claim (metric, value, unit, period) with citations. The verification gate:
- checks each quoted value against its cited lines;
- catches conflicts between streams, stale live figures and wrong bidding-day labels;
- turns verifier corrections into new, re-checked claims, and gives high-importance claims a second verifier;
- treats web and PDF content as untrusted data, never as instructions.

The publish gate releases a report only if every citation in it is sound; otherwise it is rendered as a marked draft.

## Markets, signals and accuracy (`adapters/`, `fincalc/`, `signals/`, `evals/`, `disclosures/`)

Adapters fetch public exchange and AMFI data through one polite HTTP client (rate limits, retries, timeouts, size
caps, URL checks; failures are never cached as "no data"). Signals for every asset class share one contract: a call,
a probability with its range, the factors, and how the model was validated. Each forecast goes into a ledger and is
scored when it resolves; calibration counts independent events only. New models run in shadow until they pass a
pre-registered test (`evals/experiments/*/PREREG.md`).

## Portfolio and wealth (`portfolio/`, `wealth/`, `suggest/`)

Statements are parsed locally (casparser for CAS, broker-specific readers for tradebooks and holdings statements),
de-duplicated and replayed into FIFO lots. Read-only broker connectors and the inbox folder go through the same merge
rules, which never overwrite another source. The tax engine applies the capital-gains rules in force on each trade
date. The portfolio's past values come from one canonical history rebuilt from the transactions and
official closes (`portfolio.series`); the saved snapshots are the "as shown" record, reconciled against it. Analytics, the AIS check, rebalancing, ELSS lock-ins and the decision journal all read the same lots.

## Module map

```
src/finresearch/
  adapters/      NSE, BSE, AMFI, SEBI, FBIL, XBRL clients and the polite HTTP client
  agents/        research roles, prompts, skills and the role runner
  alerts/        alert rules, the evaluation engine and portfolio alerts
  api/           local HTTP API for the app
  bridge/        Claude Code, API-key and Ollama engines; limit tracking; router
  db/            database models (Alembic migrations in migrations/)
  disclosures/   surveillance, pledge, insider, deal, rating and SEBI-order feeds
  evals/         backtests, calibration and pre-registered experiments
  fincalc/       deterministic finance and tax calculations
  ingest/        document store, OCR, section mapper, table rebuild, chunks and search
  mcp_server/    FinResearch MCP server and claim ledger
  monitor/       scheduler and jobs: live refresh, IPO checks, nightly data, notifications
  orchestrator/  resumable research pipeline, one subclass per research kind
  portfolio/     importers, lots, tax, analytics, connectors, journal, AIS, rebalancing
  render/        report HTML/PDF, tables, charts and the research pack
  signals/       buy/hold/sell signals and the forecast ledger
  suggest/       investor profile, personal rules and the advisor
  verify/        verification and publish gates
  wealth/        household balance sheet, goals and allocation
  cli.py         finresearch command line
web/             Next.js app (design notes in web/DESIGN.md)
tests/           offline tests with recorded fixtures
scripts/         live smoke checks, backups and gold-set ingestion
```

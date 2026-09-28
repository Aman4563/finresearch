# FinResearch

[![CI](https://github.com/Aman4563/finresearch/actions/workflows/ci.yml/badge.svg)](https://github.com/Aman4563/finresearch/actions/workflows/ci.yml)
![Python 3.12](https://img.shields.io/badge/python-3.12-blue)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

**FinResearch is a personal, local-first research engine that writes deep, fact-checked investment reports.** It starts with Indian IPOs and will cover listed stocks, mutual funds, bonds and F&O.

For each company it reads the offer documents and filings, pulls live exchange data, and runs a team of AI research agents. Every agent must cite the exact page and line it relies on. Python then checks every quote and recomputes every number before a verdict is written.

> **Disclaimer.** FinResearch is a personal research tool. Its output is not investment advice and not research published by a SEBI-registered research analyst. Grey-market data is unofficial, and subscription figures are interim while an issue is open. Always check primary sources and your own circumstances before investing.

## What it does

- **Reads offer documents properly.**
  - Downloads the RHP, DRHP, annual reports and financial statements, and OCRs scanned pages.
  - Maps every SEBI ICDR section (Risk Factors, Capital Structure, Objects, Basis for Offer Price, Restated Financials, MD&A, Litigation and others) with page and line ranges.
  - Rebuilds prospectus financial tables deterministically.
- **Finds evidence fast.** Hybrid keyword and semantic search over every document, with page and line anchors.
- **Uses live market data.**
  - NSE IPO calendars, and subscription by category, combined NSE+BSE as well as NSE-only, with timestamps.
  - Price-wise demand and issue details.
  - SEBI DRHP/RHP filings.
- **Never trusts the model's arithmetic.** A tested Decimal finance library covers:
  - Indian number formats and units;
  - growth, margins, returns and working capital;
  - P/E, P/B and EV/EBITDA;
  - IPO share maths, allotment odds, lock-in schedules and market-day counting.
- **Keeps a claim ledger.** Every finding is stored as a claim with citations. A claim whose quote isn't actually at the cited lines is marked unsupported automatically and can't appear in a report as fact.
- **Runs on your machine.** It uses your Claude subscription through the official Claude Code CLI, and falls back to local models when limits are reached.

## How it works

```
                    finresearch CLI  (web app planned)
                              │
             Orchestrator: deterministic stages, budgets, resume
                              │  agent tasks
   ┌──────────────────── Claude Bridge ─────────────────────┐
   │ 1. Claude Code (official CLI) on your Claude plan       │  limit-aware: 5-hour / 7-day window
   │ 2. Same CLI on an Anthropic API key (optional)          │  ceilings, cool-down to reset,
   │ 3. Local models via Ollama (degraded mode)              │  circuit breaker, call ledger
   └──────────────────────────┬─────────────────────────────┘
                              │  MCP (stdio)
               FinResearch MCP server — the agents' only tools
     documents · sections · grep · search · tables · fincalc
     NSE / SEBI live data · claim ledger with citation checks
                              │
        Postgres + pgvector            immutable document store
   claims · citations · chunks · runs   raw PDFs + canonical text
```

**Claude Bridge.** Every LLM task goes through one interface.
- **Tier 1** runs the official `claude` CLI under your own Claude login, with structured JSON output. It records how much of the plan's 5-hour and 7-day windows is used, so the engine can slow down before hitting a limit.
- **Tier 2** is the same CLI billed to an API key, for when you add one.
- **Tier 3** is local Ollama models, tuned for a 16 GB Apple-silicon Mac:
  - one generation at a time;
  - automatic 9B → 4B fallback when memory is tight;
  - schema validation with repair turns;
  - loop-safe OCR.

  Local results are always marked degraded. Tasks that need the web, tools or long context are refused locally rather than faked.

**Research agents.** A planner, seven research streams, adversarial verifiers, bull and bear analysts, a synthesiser and a completeness critic. The seven streams cover:
- financials;
- business and industry;
- risks and governance;
- offer and valuation;
- 30-day news;
- demand and subscription;
- history, sector and macro.

They work only through the MCP tools, run as a resumable pipeline that schedules around plan limits, and hand their claims to a verification gate. The gate:
- checks every value against its cited lines;
- catches conflicts between streams, stale live figures and wrong bidding-day labels;
- turns verifier corrections into re-checked claims;
- gives high-importance claims a second independent verifier;
- only publishes a report whose every citation is sound.

## Status

| Version | Scope | State |
|---|---|---|
| **v0.1.0** | Claude Bridge, document pipeline, sections and search, NSE/SEBI data, fincalc, MCP server, claim ledger | ✅ Released |
| **v0.2.0** | IPO report engine: agents and skills, multi-agent pipeline, verification gate, report and folder-pack renderer, document discovery, gold-set evaluation | ✅ Released |
| **v0.3.0** | Web app: dashboard, live agent view, report reader with citations, "ask about this report", personal suggestions, monitoring | ✅ Released |
| **v0.4.0** | Quality and scale: evaluation and back-testing on past IPOs, SME IPOs, Hindi news | 🚧 In progress |
| Later | Listed stocks, mutual funds, bonds, F&O analytics | Planned |

## Requirements

- macOS on Apple silicon; 16 GB RAM is enough.
- [uv](https://docs.astral.sh/uv/) and Python 3.12.
- [Claude Code](https://code.claude.com), logged in (`claude auth status` shows your account).
- Homebrew packages: `postgresql@17`, `pgvector`, `tesseract`, `poppler`.
- Node.js 22+ and pnpm for the dashboard.
- [Ollama](https://ollama.com) with `qwen3.5:9b`, `qwen3.5:4b`, `qwen3-embedding:0.6b` and `glm-ocr`.

## Setup

```bash
brew install postgresql@17 pgvector tesseract poppler
brew services start postgresql@17
createdb finresearch && createdb finresearch_test

ollama pull qwen3.5:9b && ollama pull qwen3.5:4b && ollama pull qwen3-embedding:0.6b && ollama pull glm-ocr

git clone https://github.com/Aman4563/finresearch.git && cd finresearch
uv sync
uv run alembic upgrade head
cp .env.example .env        # optional overrides; never commit .env
```

## Usage

```bash
# engines
uv run finresearch bridge health          # Claude login, local models, memory, limits
uv run finresearch bridge limits          # Claude plan window usage
uv run finresearch bridge run "Summarise ..." --schema schema.json --model-class standard

# documents
uv run finresearch docs discover acevector --name "AceVector Limited" --nse-symbol ACEVECTOR   # NSE + SEBI + IR pages
uv run finresearch docs add <pdf-or-url> --company orient-cables --name "Orient Cables (India) Limited" --kind RHP
uv run finresearch docs list
uv run finresearch docs sections <document_id>
uv run finresearch docs search "largest customer share of revenue" --company orient

# MCP server for Claude Code
uv run finresearch mcp config             # writes the --mcp-config file for the FinResearch tools

# research reports
uv run finresearch ipo run orient-cables --wait     # full multi-agent run; pauses and resumes around plan limits
                                                   # (finds and ingests the offer documents first if none are stored)
uv run finresearch ipo status <run_id>             # steps, models, turns, time and plan-window usage
uv run finresearch ipo resume <run_id> --wait      # continue a paused or failed run (finished steps are kept)
uv run finresearch ipo render <run_id>             # rebuild the research pack (report md/html/pdf, tables, charts)

# local API for the app (always 127.0.0.1; OpenAPI docs at /api/docs); also runs the monitor
uv run finresearch serve                           # http://127.0.0.1:8710

# monitoring after the report: subscription to the close, allotment, listing, anchor lock-ins
uv run finresearch monitor watch orient-cables     # schedule the checks from NSE's issue information
uv run finresearch monitor run                     # run the checks without the API
```

The dashboard (IPO radar, live agent view, report reader with clickable evidence, "ask about this report" chat, personal suggestions checked against your own rules, a decision journal, monitoring alerts, plan usage) is a Next.js app in
`web/`:

```bash
cd web && pnpm install && pnpm build && pnpm start   # http://127.0.0.1:3100 (needs `finresearch serve`)
```

A finished run produces a research pack under `data/reports/<company>/run-<id>/`:
- **Folders:** offer documents, financial reports, news, major events, valuation and a final report.
- **Final report:** an HTML/PDF version where every figure links to its evidence (document page and line, or URL and access time).
- **Supporting files:** a fact-check log, the full claim ledger as Excel/CSV, financial tables and charts.
- **Blocked reports:** a report that fails the publish gate is rendered only as a clearly marked draft.

## Development

```bash
uv run ruff check . && uv run ruff format --check .
uv run pytest -q                           # offline suite + database tests (finresearch_test)
uv run python scripts/smoke_live.py        # live check of the Claude and local tiers
uv run python scripts/smoke_mcp_live.py    # live check: Claude -> MCP tools -> verified claim
```

Contributions follow an issue → branch → pull request workflow; see [CONTRIBUTING](.github/CONTRIBUTING.md). Please read the [Code of Conduct](.github/CODE_OF_CONDUCT.md), and report vulnerabilities privately as described in the [Security policy](.github/SECURITY.md).

## Project layout

```
src/finresearch/
  bridge/       Claude Code, API-key and Ollama engines; limit tracking; router
  ingest/       document store, OCR, section mapper, table rebuild, chunks and search
  adapters/     polite HTTP client, NSE and SEBI
  fincalc/      deterministic finance calculations
  db/           database models (migrations/ holds Alembic migrations)
  mcp_server/   FinResearch MCP server and claim ledger
  agents/       research roles, prompts, skills and the role runner
  orchestrator/ resumable, budget-aware IPO report pipeline
  verify/       deterministic verification gate and publish gate
  render/       report HTML/PDF, tables, charts and the research folder pack
  api/          local HTTP API and live run events for the app
  suggest/      investor profile, personal rules, advisor and decision journal
  monitor/      scheduled checks after the report and alerts
  cli.py        finresearch command line
web/            Next.js dashboard (radar, live runs, report reader, usage)
tests/          offline tests and recorded fixtures
scripts/        live smoke checks and gold-set ingestion
```

## License

[MIT](LICENSE)

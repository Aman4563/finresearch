# FinResearch

[![CI](https://github.com/Aman4563/finresearch/actions/workflows/ci.yml/badge.svg)](https://github.com/Aman4563/finresearch/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/Aman4563/finresearch)](https://github.com/Aman4563/finresearch/releases)
![Python 3.12](https://img.shields.io/badge/python-3.12-blue)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

**A personal, local-first investment research app for Indian markets.** It writes deep, fact-checked research
reports on IPOs, stocks, mutual funds and bonds, tracks live market data, and turns your own broker statements into
a portfolio with P&L, XIRR and capital-gains tax. Everything runs on your own machine.

Research is done by Claude agents that must cite the exact page and line (or URL and time) behind every claim.
Python, not the model, does all the arithmetic and checks every quote before anything is published.

> **Disclaimer.** FinResearch is a personal research tool, not investment advice, and not research published by a
> SEBI-registered research analyst. Signals and forecasts are probabilities with stated uncertainty; some have no
> proven edge and say so. Live and grey-market figures can be interim or unofficial. Check primary sources before
> investing.

## Features

**Research reports**
- Multi-agent reports for IPOs, listed stocks, mutual funds and bonds/NCDs: planner, research streams, adversarial
  verifiers, bull and bear cases, synthesiser and critic.
- Reads offer documents (RHP/DRHP), annual reports and filings, with OCR, section mapping and table rebuilding.
- A claim ledger with citations; a publish gate blocks any report that cites an unsupported or contradicted claim.
- Research packs as HTML/PDF where every figure links to its evidence, plus a fact-check log and the full ledger.

**Markets**
- **IPOs:** calendar, live subscription by category, allotment odds, lot sizes, listing-day tracking.
- **Stocks:** NSE and BSE quotes (official close after hours), intraday charts, results, corporate actions, a peer
  table, and red flags: ASM/GSM surveillance, promoter pledge, F&O ban, insider and bulk/block deals, rating actions
  and SEBI orders.
- **Mutual funds:** AMFI NAVs, returns and risk, SEBI-category rank, portfolio look-through and fund overlap.
- **Bonds and NCDs:** yield to maturity on the exchange's dirty price, accrued interest, after-tax yield.
- **F&O:** option chain, greeks, implied volatility and strategy payoffs (analysis only).

**Your portfolio** (kept on your machine, never sent to an AI model)
- Import CAMS/KFintech CAS and NSDL/CDSL e-CAS PDFs, broker tradebooks and holdings statements (Groww, Zerodha,
  Upstox), or connect a broker read-only; a watched inbox folder imports statements automatically.
- FIFO lots, realised and unrealised P&L, XIRR, allocation, risk, concentration and costs.
- Capital-gains tax by the rules in force on each trade date, tax-loss harvesting, ELSS lock-ins, an AIS check, and
  tax-aware rebalancing suggestions.
- Household wealth and goals, a morning brief, alert rules (with phone notifications), and a decision journal with a
  pre-trade checklist and a behaviour report.

**Signals and accuracy**
- Buy/hold/sell signals for every asset class, each with a probability, its range and how it was validated.
- Every forecast is logged and scored when it resolves (Brier score, calibration); experimental models run in
  shadow until they pass a pre-registered test.

## How it works

```
   Next.js app (127.0.0.1:3100)            finresearch CLI
              └──────────── FastAPI (127.0.0.1:8710) ── monitor (scheduled jobs)
                                     │
        orchestrator ── Claude Bridge: Claude Code CLI → API key (optional) → local Ollama
                                     │ MCP
   FinResearch MCP tools: documents · search · tables · fincalc · NSE/BSE/AMFI/SEBI data · claim ledger
                                     │
               Postgres + pgvector  ·  immutable document store  ·  local data/ folder
```

The full design, invariants and module map are in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Quick start

Requirements: macOS on Apple silicon (16 GB RAM is enough), [uv](https://docs.astral.sh/uv/) with Python 3.12,
Node.js 22+ with pnpm, Homebrew `postgresql@17`, `pgvector`, `tesseract` and `poppler`,
[Claude Code](https://code.claude.com) logged in, and optionally [Ollama](https://ollama.com) for local fallback.

```bash
brew install postgresql@17 pgvector tesseract poppler && brew services start postgresql@17
createdb finresearch && createdb finresearch_test

git clone https://github.com/Aman4563/finresearch.git && cd finresearch
uv sync && uv run alembic upgrade head

uv run finresearch serve                                  # API + monitor on http://127.0.0.1:8710
cd web && pnpm install && pnpm build && pnpm start        # app on http://127.0.0.1:3100
```

Then open the app, set up your profile, and either start a research run (`uv run finresearch ipo run <company>`) or
import a statement on **Portfolio → Import**. The complete command reference is in [docs/USAGE.md](docs/USAGE.md).

## Documentation

| Guide | What it covers |
|---|---|
| [Usage](docs/USAGE.md) | CLI commands, research runs, research packs, long runs |
| [Broker setup](docs/BROKER_SETUP.md) | Read-only broker connections, the statement inbox, which files to download |
| [Backups](docs/BACKUPS.md) | Nightly database backups and restoring |
| [Architecture](docs/ARCHITECTURE.md) | Components, the Claude Bridge, agents, the verification gate |
| [Engineering handoff](docs/dev/ENGINEERING_HANDOFF.md) | Invariants and sharp edges for anyone changing the code |
| [Product requirements](docs/dev/PRODUCT_REQUIREMENTS.md) | Requirement status and the verification log |
| [Research roadmap](docs/dev/RESEARCH_ROADMAP.md) | Accuracy and signal research behind the models |

Release notes are on the [Releases](https://github.com/Aman4563/finresearch/releases) page.

## Privacy and data sources

- Services bind to `127.0.0.1` only. Your portfolio, statements and journal stay in the local database and the
  gitignored `data/` folder; statement passwords are used once and never stored.
- Claude is used only through the official Claude Code CLI or Agent SDK under your own login. Personal financial
  data is never sent to a model.
- Market data comes from public NSE, BSE, AMFI, SEBI and FBIL pages, fetched politely with caching and rate limits,
  for personal use.

## Contributing

Issues and pull requests are welcome; see [CONTRIBUTING](.github/CONTRIBUTING.md), the
[Code of Conduct](.github/CODE_OF_CONDUCT.md) and the [Security policy](.github/SECURITY.md).

```bash
uv run ruff check . && uv run ruff format --check . && uv run pytest -q
cd web && npx tsc --noEmit && npx eslint src
```

## License

[MIT](LICENSE)

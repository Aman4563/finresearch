# FinResearch

A personal research engine that produces deep, fact-checked reports. It starts with Indian IPOs and will extend to stocks, mutual funds, bonds and F&O.

The design is in `../FinResearch_App_Blueprint/`; start with `02_Final_Architecture_v1.2.md`. The reference reports this engine has to match or beat are in `../Moneyview_IPO_Research/` and `../OrientCables_IPO_Research/`.

> Personal use only. This is not SEBI-registered investment advice.

Workflow and gates: [CONTRIBUTING.md](CONTRIBUTING.md) · invariants: [ENGINEERING_HANDOFF.md](ENGINEERING_HANDOFF.md) · status: [PRODUCT_REQUIREMENTS.md](PRODUCT_REQUIREMENTS.md)

## Status: P0 spike ✅ · P0 foundations ✅

### P0 spike: Claude Bridge

The **Claude Bridge** gives every LLM task one interface over three engines, tried in this order:

| Tier | Engine | Status |
|---|---|---|
| 1. `claude_max` | The official Claude Code CLI (`claude -p`) on the logged-in **Max** subscription | ✅ primary |
| 2. `claude_api` | The same CLI billed to `ANTHROPIC_API_KEY` | ⏸ disabled (no key by choice). Set `FINRESEARCH_CLAUDE_API_ENABLED=true` + a key to enable |
| 3. `local` | Ollama: `qwen3.5:9b` / `qwen3.5:4b`, `qwen3-embedding:0.6b`, `glm-ocr` | ✅ fallback (degraded mode) |

**Guarantees the router enforces:**
- **Max always means the subscription.** API-key and auth-token env vars are stripped for Tier 1, so the CLI can't silently bill an API key.
- **Limit-aware.**
  - Every Claude run records the Max 5-hour and 7-day window utilisation from Claude Code's `rate_limit_event`.
  - Before each call, Max is skipped when a window is above its ceiling (default 92% / 97%).
  - When a limit is hit, Max cools down until the reported reset time.
  - A circuit breaker handles repeated overloads.
- **No silent fakes.**
  - Local results are always flagged `degraded=True`.
  - Tasks that need web or tools, or whose prompt is too long for local context, are refused locally (`CapabilityMismatch`) rather than quietly truncated or invented.
  - A task can opt out of local entirely with `allow_degraded=False`.
- **Local tier is sized for a 16 GB M1 Pro.**
  - Only one generation runs at a time.
  - It drops from 9B to 4B automatically when reclaimable RAM is short.
  - Structured output is validated against the JSON Schema, with up to 2 automatic repair turns.
  - Thinking mode is off for speed and determinism.
  - It warns when output is mostly null.
- **OCR is safe:** glm-ocr output is streamed, and the run is cut at the first repetition loop. That took one test page from 200 s / 48K garbage chars to 8–14 s / a clean page.
- **Everything is logged:** each attempt goes to `data/state/ledger.jsonl`, and each Claude run's stream-json transcript is saved to `data/runs/**/transcripts/`.

**Ingest:** `finresearch.ingest.layout_table` rebuilds tables from `pdftotext -layout` output deterministically. It handles wrapped multi-line headers, per-line horizontal shifts, Notes columns and period detection, so an LLM never has to parse layout. With it, even `qwen3.5:4b` extracts P&L values exactly. Use `ingest.text.read_lines()` for line numbers: pdftotext page breaks (`\f`) make `str.splitlines()` disagree with grep/sed.

### P0 foundations: data, documents, MCP tools, claim ledger

| Component | What it does |
|---|---|
| **Postgres 17 + pgvector** (Homebrew, native) | Tables: company, document, document_page, section, chunk (HNSW vector + GIN full-text), research_run, claim, citation, ipo_offer, subscription_snapshot. Alembic migrations |
| **Document pipeline** (`ingest.documents`) | Stores files by sha256 (immutable), runs `pdftotext -layout`, and OCRs scanned pages with Tesseract (+ optional loop-safe glm-ocr for low confidence). Writes a canonical `text.txt` whose line numbers match `grep -n`. Page→line spans are stored per page |
| **Section mapper** (`ingest.sections`) | Maps SEBI ICDR offer-document sections (Risk Factors, Capital Structure, Objects, Basis for Offer Price, Business, Restated Financials, MD&A, Litigation, …) with line and page ranges. Verified on Moneyview and Orient RHP + DRHP |
| **Index + search** (`ingest.index`) | Line-anchored chunks, local `qwen3-embedding` vectors, hybrid full-text + vector search (RRF) |
| **Adapters** (`adapters.*`) | Polite HTTP (rate limits, retries, opt-in cache, provenance). NSE: current, upcoming and past issues, `ipo_detail` (combined NSE+BSE vs NSE-only subscription, demand, issue info). SEBI: DRHP/RHP/prospectus listings and full-PDF resolution |
| **fincalc** | Deterministic Decimal finance library: Indian number parsing and units, growth, ratios, valuation, IPO maths (issue, reservation, lots, allotment floor, lock-ins), market-day arithmetic. Golden-tested against the manual reports |
| **FinResearch MCP server** (`mcp_server`) | Tools for Claude agents: list/read documents and sections with line and page numbers, grep, hybrid search, `extract_table`, `fincalc_call`, live NSE/SEBI, `start_run`, **`save_claim` with deterministic quote-at-cited-lines verification**, `list_claims` |

**Live-verified:** Claude Sonnet 5 on the Max plan, using only the MCP tools, found Orient Cables' largest-customer share (38.54%, Q1 FY27, RHP p26 L1663). It saved a claim, and the server confirmed the quote is at that line: 12 s, 6 turns (`scripts/smoke_mcp_live.py`).

## Setup (once)

```bash
brew install postgresql@17 pgvector tesseract poppler && brew services start postgresql@17
createdb finresearch && createdb finresearch_test
uv sync && uv run alembic upgrade head
ollama pull qwen3.5:9b qwen3.5:4b qwen3-embedding:0.6b glm-ocr
```

## Usage

```bash
uv run finresearch bridge health                      # engines, login, models, RAM, limits
uv run finresearch bridge limits                      # Max 5h / 7d window utilisation
uv run finresearch bridge run "..." --schema s.json   # one task through the bridge
uv run finresearch docs add <pdf|url> --company orient-cables --kind RHP   # ingest + OCR + sections + index
uv run finresearch docs list | docs sections <id> | docs search "query" --company orient
uv run finresearch mcp config                         # write Claude Code --mcp-config for the MCP server
uv run python scripts/ingest_gold.py                  # load the Moneyview + Orient gold set
uv run pytest -q                                      # 111 offline tests (+ DB tests on finresearch_test)
uv run python scripts/smoke_live.py                   # live bridge check
uv run python scripts/smoke_mcp_live.py               # live Claude -> MCP -> claim ledger check
```

## Layout

```
src/finresearch/
  bridge/        types, claude_code (Tier 1/2), ollama_engine (Tier 3), limits, router
  ingest/        documents (store/extract/OCR), sections (ICDR mapper), index (chunks, embeddings, hybrid
                 search), layout_table (pdftotext table rebuild), text (line-safe reading)
  adapters/      http (polite client), nse, sebi
  fincalc/       numbers, growth, ratios, valuation, ipo, dates
  db/            SQLAlchemy models + session       (migrations/ = Alembic)
  mcp_server/    FinResearch MCP server (tools), claims (ledger + citation checks), config
  config.py      settings (FINRESEARCH_* env / .env)
  cli.py         `finresearch` CLI (bridge, docs, mcp)
tests/           offline tests + fixtures (real RHP excerpts, NSE/SEBI recordings, fake claude CLI)
scripts/         smoke_live.py, smoke_mcp_live.py, ingest_gold.py
data/            gitignored: docs (raw + derived), state, runs, cache, logs
```

## Next (P1: IPO report engine)

- Agent definitions (`.claude/agents`) + skills: planner, 7 research streams, adversarial verifiers, bull/bear, synthesizer, critic.
- Python DAG with Max-limit-aware concurrency and resume.
- Numeric verification gate.
- Report renderer (MD/HTML/PDF/XLSX).

Acceptance: regenerate the Moneyview and Orient Cables reports and compare them with the manual fact-check logs.

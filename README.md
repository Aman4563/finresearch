# FinResearch

A personal research engine that produces deep, fact-checked reports. It starts with Indian IPOs and will extend to stocks, mutual funds, bonds and F&O.

The design is in `../FinResearch_App_Blueprint/`; start with `02_Final_Architecture_v1.2.md`. The reference reports this engine has to match or beat are in `../Moneyview_IPO_Research/` and `../OrientCables_IPO_Research/`.

> Personal use only. This is not SEBI-registered investment advice.

## Status: P0 spike (Claude Bridge) ✅

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

## Usage

```bash
uv sync
uv run finresearch bridge health            # login, models, RAM, limits
uv run finresearch bridge limits            # Max 5h / 7d window utilisation
uv run finresearch bridge run "Summarise ..." --schema schema.json --model-class standard
uv run finresearch bridge run "..." --tier local          # force a tier
uv run finresearch bridge reset             # clear cool-downs after a window resets
uv run pytest -q                            # offline test suite (fake CLI + mocked Ollama)
uv run python scripts/smoke_live.py         # live end-to-end check (1 tiny Haiku call + local)
```

## Layout

```
src/finresearch/
  bridge/        types, claude_code (Tier 1/2), ollama_engine (Tier 3), limits, router
  ingest/        layout_table (pdftotext table rebuild), text (line-safe reading)
  config.py      settings (FINRESEARCH_* env / .env)
  cli.py         `finresearch` CLI
tests/           offline tests + fixtures (real RHP excerpts, fake claude CLI)
scripts/         smoke_live.py
data/            gitignored: state, runs, documents
```

## Next (P0 foundations → P1)

Document acquisition and ingestion (section mapper, OCR pipeline), the NSE/SEBI adapters, the FinResearch MCP server, the claim ledger (Postgres), the agent definitions and skills, and the multi-agent IPO DAG. The acceptance test is to regenerate the Moneyview and Orient Cables reports.

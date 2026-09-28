# Functional testing log

These are dated entries for bugs we reproduced, what caused them, and how they are now checked. Add new entries at the bottom.

## Test suites

| Suite | What it covers | Where |
|---|---|---|
| Offline unit | Bridge (fake `claude` CLI), local engine (mocked Ollama), layout tables, loop detection, adapters (recorded NSE/SEBI), fincalc (golden values) | `tests/test_bridge.py`, `test_ingest.py`, `test_adapters.py`, `test_fincalc.py` |
| DB integration | PDF ingest (pages, OCR, idempotency), MCP tools, claim ledger citation checks | `tests/test_db_pipeline.py` (skipped if `finresearch_test` is unreachable; CI provides pgvector) |
| Live bridge | Max tier, local extraction, simulated failover, embeddings, OCR | `scripts/smoke_live.py` |
| Live MCP | Claude on Max → MCP tools → verified claim in the ledger | `scripts/smoke_mcp_live.py` |

## Entries

### 2026-09-28: local extraction returned all nulls
- **Seen:** `qwen3.5:9b` returned schema-valid JSON with every value null for an RHP P&L excerpt.
- **Cause:** the excerpt was really the balance sheet. `str.splitlines()` splits on the `\f` page breaks that pdftotext emits, so Python line numbers drifted from `grep`/`sed` by about 30 lines. The model's nulls were correct.
- **Now checked:** all line access goes through `ingest.text.read_lines()`, covered by `test_read_lines_ignores_form_feeds`. The local engine also warns on mostly-null output.

### 2026-09-28: small models could not read RHP tables
- **Seen:** even with clean input, the 4-line wrapped headers and per-row horizontal shifts (Moneyview) stopped models aligning values with periods.
- **Now:** `ingest.layout_table` rebuilds tables deterministically, including column-aware split dates and a Notes column. It is covered by fixture tests on the real Moneyview and Orient layouts, and `smoke_live.py` checks all 8 values exactly.

### 2026-09-28: glm-ocr loop
- **Seen:** one page took 200 s and produced 48K characters. The model transcribed the page, then replayed paragraphs (and "2013, 2013, …") until the token limit.
- **Now:** OCR is streamed and cut at the first repeated paragraph or token run (`find_repetition`, tested). The page now takes 8–14 s and yields clean text.

### 2026-09-28: 9B model downgraded unnecessarily
- **Seen:** 2.6 GB free RAM, even though 9B was already loaded, forced a switch to 4B.
- **Now:** the engine counts memory that Ollama can reclaim from loaded models (`test_local_downgrades_9b_when_memory_tight`).

### 2026-09-28: DRHP "Objects of the Offer" mapped into the summary
- **Seen:** the summary section repeats sub-headings as standalone upper-case lines.
- **Now:** every section except Definitions and the Summary must follow RISK FACTORS. Verified on 2 RHPs and 2 DRHPs.

### 2026-09-28: a successful Claude result could have been discarded as a limit hit
- **Seen:** a `rate_limit_event` with status `rejected` would have raised `LimitReached` even when the run succeeded (code review).
- **Now:** limit classification applies only when the run failed, and stderr is drained concurrently to avoid pipe deadlock.

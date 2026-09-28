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

### 2026-09-28: secret scan failed on the new public repository
- **Seen:** the first push to the re-created public repository failed the `secrets` job ("failed to scan Git repository"). gitleaks-action scans the push's `before..after` range, and `before` does not exist on a new repository or after a force push.
- **Now:** CI runs the pinned gitleaks CLI over the full history, so every push and PR scans everything, independent of event ranges or PR API permissions (#11).

### 2026-09-28: pipeline tests overwrote a live run's report
- **Seen:** after adding the gate tests, `data/runs/3/report.md` (the real run 3 report, 20 KB) had been replaced by a 59-byte test stub. The DB fixture redirected the database and the docs/state dirs but not `runs_dir`, and test-database run ids overlap real ones.
- **Recovered:** the report was restored from the `agent_step` output of `synthesis:r2`. Test-only run folders were removed.
- **Now:** an autouse fixture in `tests/conftest.py` gives every test its own runs/state/docs directories. `tests/test_isolation.py` fails if any test's settings resolve inside the real `data/` folder.

### 2026-09-28: gate replay on live run 3
- **Seen:** replaying the publish gate on run 3's real report blocked it for 10 raw `[RHP L…]` citations (the synthesizer bypassing the ledger). It also warned that 63 lines carry figures without a claim citation.
- **Now:** in a live run this triggers a revision round (`synthesis:fixN`), and a report that still fails is saved only as `report_blocked.md` with run status `blocked`.
- **Also seen:** the first live-figure check flagged a lead-manager statement just for mentioning "subscription". It now requires a live number (x, ₹, %) in the claim (`test_live_check_ignores_statements_without_a_live_figure`).

### 2026-09-28: corrupted PDF on re-render (Moneyview, manual run) is now an automated test
- **Seen (earlier, by hand):** re-rendering the Moneyview report over an existing PDF produced two concatenated documents that viewers refused to open, and headless Chrome sometimes never exited.
- **Now:** `render.pdf.html_to_pdf` prints to a unique temp file with its own Chrome profile, kills the process group, validates with pypdf in strict mode, rewrites a clean single document and atomically renames it. `test_pdf_rerender_over_existing_file_stays_valid` renders twice over the same path and asserts a single `%%EOF` and identical page counts.

### 2026-09-28: live discovery crashed while indexing (AceVector)
- **Seen:** the first live `docs discover acevector` failed with `asyncio.run() cannot be called from a running event loop`. `discover()` is a coroutine, and indexing calls `asyncio.run()` for embeddings (and OCR).
- **Now:** ingestion and indexing run in a worker thread (`asyncio.to_thread`). `test_discover_indexes_documents_from_inside_an_event_loop` runs discovery with indexing inside an event loop.

### 2026-09-28: NSE's RHP ZIP also contains the General Information Document
- **Seen:** the NSE `RHP_ACEVECTOR.zip` held two PDFs: the 575-page RHP and a 50-page "GID". Both were tagged `RHP`, so an offer-document lookup could have picked the wrong one.
- **Now:** each ZIP member is classified by its own filename (`kind_for_member`: GID → OTHER, abridged → ABRIDGED_PROSPECTUS). The stored AceVector GID was retagged.

### 2026-09-28: the same RHP stored twice from NSE and SEBI (Orient Cables)
- **Seen:** agent discovery for Orient Cables ingested NSE's copy of the RHP as a second RHP. It has different bytes from SEBI's copy (so sha256 did not match) but 480 of 491 pages are identical; only the signature pages differ. `Orient_GID.pdf` was also tagged RHP, because `\bgid\b` does not match after an underscore.
- **Now:** after extraction, a document whose pages are at least 90% identical to another document of the same company is rolled back and reported as a duplicate, and its stored files are removed (`test_a_byte_different_copy_with_the_same_text_is_a_duplicate`). The GID pattern no longer relies on word boundaries. The stored GID was retagged, and the duplicate RHP copy was removed after run 5 finished.
- **Also:** each discovered document is now committed on its own, so a crash on one document keeps the others (`test_a_broken_document_fails_alone_and_earlier_documents_are_kept`).

### 2026-09-28: the gold scorer counted related figures as contradictions (runs 4 and 5)
- **Seen:** run 4 showed 16 "contradicted" gold facts. They came from DRHP figures, the lower price band, peers' ratios, post-dilution EPS and underscored metric names. Run 5 showed 1: segment and export revenue claims contradicted "revenue from operations FY26" because the pattern includes a bare `revenue`.
- **Now:** a contradiction needs the claim's own metric and period fields to name the gold fact, excluding other contexts (DRHP, lower band, peers, dilution) and component metrics (segment, export, domestic, product, region). Run 5: 100% recall and 0 contradicted. Regression cases are in `test_underscored_metrics_and_other_contexts_are_handled`.

### 2026-09-28: price band, lot size and fresh issue never reached the ledger (run 4)
- **Seen:** the valuation stream used these as fincalc inputs but never saved them as claims. Four high-importance gold facts were missing, so recall was 82%.
- **Now:** `verify/baseline.py` records the NSE issue-information facts (price band, lot, face value, fresh issue, OFS, anchor portion) as verified claims citing the NSE URL, and the valuation stream has a checklist of offer facts to record. Run 5 found all of them.

### 2026-09-28: the dashboard could not save the profile or journal entries (CORS)
- **Seen (code review while adding monitoring):** the API allowed only GET and POST cross-origin, so the browser's preflight for `PUT /api/profile` and `PATCH /api/decisions/{id}` from the dashboard origin would be rejected. The page-load smoke test never saves, so it missed this.
- **Now:** PUT and PATCH are allowed for the dashboard origins. `test_watch_api_and_cors_for_the_dashboard` sends the preflight for both methods, and a live `curl` preflight returns 200.

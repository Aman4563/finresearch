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

### 2026-09-29: SME subscription tables would have read as zero demand
- **Seen (live BMISL, SME):** NSE's category table for SME issues has no offered shares and shows 0.00x for every category, while the issue was 1.27x subscribed overall. Parsed as-is, that would have fired a "QIB < 1x" rule and reported zero demand.
- **Now:** a category with no offered shares has unknown times. Rules on unknown metrics become conditions, and the monitor takes the overall total from NSE's current-issues list (`test_sme_category_times_are_unknown_not_zero`, `test_monitor_records_sme_subscription_from_the_current_issues_total`).

### 2026-09-29: translated Hindi figures were not marked as translations (live run 7)
- **Seen:** the news agent cited a Hindi Business Standard article with the Hindi quote but wrote an English statement without saying it was translated. Neither the prompt instruction nor the tool's note changed this.
- **Now:** `save_claim` detects Devanagari in a citation quote and appends "(translated from Hindi)" to an unmarked statement (`checks.translation_mark_added`). The report gate still warns about unmarked Hindi claims saved earlier (`test_hindi_quotes_are_flagged_and_unmarked_translations_warned`).

### 2026-09-29: the stock fundamentals stream skipped most statement lines (live Infosys run 8)
- **Seen:** run 8 recorded revenue, PAT and EPS but not PBT, tax, expense lines, total assets, working capital or dividends paid. Recall was 11/23.
- **Now:** the fundamentals prompt has a checklist of consolidated P&L, balance-sheet and cash-flow lines for the latest two years. Run 9 recall was 21/23.

### 2026-09-29: USD figures read as rupees by the gate and scorer
- **Seen:** Infosys's revenue in "USD million" was scaled as ₹ million and flagged as contradicting the INR gold fact.
- **Now:** `rupee_scale` returns None for foreign currencies (USD, $, EUR, GBP, JPY) (`test_foreign_currency_units_never_get_a_rupee_scale`).

### 2026-09-29: the scorer dropped or wrongly contradicted related figures (runs 8 and 9)
- **Seen, and now fixed (each with a regression test):**
  - An annual claim was dropped because its statement mentioned the quarter it was booked in. `period_exclude` now reads the period field only.
  - A consolidated claim citing a standalone comparative was dropped. `exclude_unless` now lifts a statement-only exclusion.
  - A `standalone_net_profit` metric counted for the consolidated fact. An exclusion in the metric or period always applies.
  - Adjusted or normalised EPS, profit before exceptional items, and interim and final dividend parts counted as contradicting the reported totals. Derived variants no longer contradict, unless the fact's own pattern names them.

### 2026-09-29: fund returns disagreed between tools, and a 5-year return was missing
- **Seen (live AMFI checks):** the 5-year trailing return was empty because the history started exactly 5 years back. The peer table was anchored on today and the NAV history on the last NAV date, so Axis Midcap's 1-year return was 6.76% in one and 7.17% in the other.
- **Now:** the history request has a 10-day buffer, and both tools anchor on the latest NAV date. They agree exactly.

### 2026-09-29: rolling returns not recorded; peers' figures counted against the scheme (fund runs 10 and 11)
- **Seen:** run 10 never recorded the tool's 5-year rolling statistics. Run 11's scoring counted category peers' volatility and drawdown, and the rolling median and maximum, as contradicting Axis Midcap's figures.
- **Now:** the rolling minimum, median and maximum are baseline claims. Fund gold facts use `require` (the claim must name the scheme) and tight period patterns (`test_require_keeps_peers_figures_out`). The ledger stores fund returns in % so 6 decimals keep their precision.

### 2026-09-29: post-tax yield of a premium bond was overstated (live bond run 12)
- **Seen:** `bond_analytics` gave the post-tax yield as YTM x (1 − tax rate). The verifier contradicted it: for a bond bought above par, only coupons are taxed as interest, and the pull to par at redemption is a capital loss that saves tax only if it can be offset.
- **Now:** `fincalc.bonds.after_tax_ytm` solves the yield on after-tax cash flows (coupons after slab tax; the redemption gain or loss taxed or relieved separately). The tool returns `after_tax_ytm` with a note (`test_after_tax_ytm_uses_after_tax_cash_flows`).

### 2026-09-29: expected listing date fell on an exchange holiday
- **Seen:** the date calendar had no exchange holidays, so Orient Cables' expected T+3 listing was 2-Oct-2026, a trading holiday.
- **Now:** NSE's holiday master (trading and clearing, CM segment) is cached and used in every exchange-date calculation. T+3 from 29-Sep-2026 is 5-Oct-2026, and a check whose slot moves is cancelled rather than run on the old day (`test_orient_t_plus_3_skips_gandhi_jayanti`, `test_moved_listing_date_cancels_the_old_check`).

### 2026-09-29: bond conventions verified against primary sources (#59)
- **Price basis:** NSE's page for bonds traded in the capital market says they trade and settle on a dirty price (accrued interest included). `bond_analytics` now defaults `price_basis` to `dirty` and returns `price_basis_source`. NSE's separate debt segment quotes clean prices, so that segment is out of scope.
- **Day count:** SEBI circular CIR/IMD/DF-1/122/2016 sets Actual/Actual for listed debt: 366 days when the year (counted between maturity anniversaries) contains 29-Feb, otherwise 365. Before this fix, `accrued_interest` always used 365 (`test_accrued_interest_is_actual_actual_per_sebi`).
- **L&T NCD INE027E07998:** the Tranche 1 prospectus (22-Feb-2019, on sebi.gov.in) lists Series VI Option 2 at 8.98%, paid monthly on an Actual/Actual basis, face value ₹1,000. Its gold file now cites the prospectus for the frequency.

### 2026-09-29: deep code review (#63, #66, #67)
- **Seen:** 24 problems across the monitor, the pipeline and the API/dashboard (listed in PRODUCT_REQUIREMENTS, verification log). 18 were reproduced against running code or throwaway tests.
- **Now:** every one is fixed, with a regression test that fails on the old code (`tests/test_monitor.py`, `test_orchestrator.py`, `test_gate.py`, `test_db_pipeline.py`, `test_api.py`). The redeployed app was checked live: a cross-site POST is refused (403), and the 12:00 Orient check ran on the new monitor code.

### 2026-09-29: BSE SME issues were not covered (#60, #69)
- **Seen:** only NSE issues appeared on the radar, and a BSE-only SME issue could not be researched or watched.
- **Now:** a BSE adapter with the full browser headers BSE's edge needs (the same headers from curl get a 403). The radar, issue facts, watches, subscription and listing checks all work for BSE-only issues. Tests run offline on payloads recorded live on 29-Sep-2026 (`tests/test_bse.py`).

### 2026-09-29: new bond page assumed yearly coupons (#79)
- **Seen:** `/bonds/INE027E07998` showed YTM 7.68% and accrued ₹49.21, because NSE's list has no coupon frequency and the page defaulted to yearly. The bond pays monthly (SEBI prospectus; verified claim #2180 in run 12).
- **Now:** without an explicit `freq`, the analytics use the verified `coupon_frequency` claim from the bond's latest research, else flag the frequency as assumed. The page shows the source (`test_bond_frequency_comes_from_verified_research_else_is_flagged_as_assumed`). Live: YTM 5.81%, accrued ₹3.94, after tax 3.22%.

### 2026-09-29: headless screenshots caught animations mid-way
- **Seen:** Chrome's `--screenshot` captured before chart and entrance animations finished, so bars looked empty and list rows looked faded. It also cannot lay out narrower than about 500 px.
- **Now:** UI checks drive Chrome through its debugging protocol with an explicit wait and 390 px device emulation, and `?theme=` / `?welcome=0` pin the theme and hide the welcome tour.

### 2026-09-29: NSE data gaps (#83, #84, #85)
- **Seen:** the stock page had no FII/DII split, results stopped at Dec-2024, and NSE mainboard IPOs showed no lot cost.
- **Now:**
  - The shareholding split comes from the filed shareholding-pattern XBRL (INFY Jun-2026: FPI 27.09, DII 42.96, promoter 13.82, matching the filing).
  - Results come from NSE Integrated Filing (Financials) from Mar-2025 (INFY Q1 FY27 figures match the filed PDF). Banks and insurers are mapped.
  - Lots come from NSE's issue page, cross-checked against BSE (Orient: 55 shares, confirmed by NSE's Security Parameters PDF).
  - A band mismatch between NSE's list and its issue page (Runwal ₹302 vs ₹305) is flagged.

### 2026-09-29: report PDF showed nothing in the user's browser (#91)
- **Seen:** three 200 responses for run 9's report.pdf with nothing displayed. Not reproducible: the file is valid, and Chrome settings and extensions don't intercept PDFs. `HEAD` returned 405.
- **Now:** the in-app pdf.js viewer is used for every PDF. File routes answer HEAD and send a filename. Scanned pages render (pdf.js decoder assets are copied at build).

### 2026-09-29: report charts misrepresented ranges (#94, found in review)
- **Seen:** "Fair value vs price" drew entry zones and bands as bars from ₹0. On run 12 a needs-review YTM the report had dropped appeared as a headline tile.
- **Now:** ranges are floating low–high bars and single values are dots, with the missing end of a range never invented. Headlines use verified claims only (`test_summary_headlines_use_verified_claims_only`, `test_fair_values_pair_low_and_high_ends_but_never_invent_one`).

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

### 2026-09-30: research runs died on Mac sleep and OAuth refresh collisions (run 13, #100/#99)
- **Seen:** "API Error: Your computer went to sleep mid-response", then `TaskFailed ... Failed to refresh OAuth token: another Claude Code process is refreshing it`. The worker crashed and the run stayed `running` with no reason given.
- **Now:** these are classified transient, including a stream silent for 15 minutes. A failed step is retried with backoff; after that the run pauses with a reason and resumes itself. An unexpected exception fails the run with `last_error` instead of crashing. Workers keep the Mac awake. Stalled runs show Resume.

### 2026-09-30: after-tax bond yield ignored accrued interest (#104)
- **Seen:** `after_tax_ytm` priced cash flows against the clean price, so the buyer's accrued interest was left out. Between coupon dates this overstated the yield.
- **Now:** the dirty price paid is used. L&T INE027E07998 (monthly): 3.06% monthly-compounded, 3.10% effective, matching an independent IRR (3.00%).
- **Also found in review:** the PR text quoted a yearly-coupon 4.30%. It came from the builder's test script; the signal uses the verified monthly frequency and flags an assumed one.

### 2026-09-30: NSE IPO data gaps found by the harvest (#109)
- **Seen:** of 461 mainboard issues:
  - 22 have no final book on NSE, and 10 are corrupted at the source (numbers truncated at a comma: PAYTM, NYKAA, STARHEALTH and others);
  - CAMS and PROTEAN listed on BSE months before NSE.
- **Now:** these are excluded with reasons instead of guessed; coverage is 423 complete rows.

### 2026-09-30: test runs collided on a shared test database
- **Seen:** 167 errors (`relation "research_run" does not exist`, duplicate keys) when two pytest runs shared `finresearch_g_test`. conftest resets the whole schema.
- **Now:** every parallel worktree uses its own `finresearch_<x>_test`, and the full suite passes (605) on a private database.

### 2026-09-30: NSE price after the close showed the last trade, not the official close (TMCV)
- **Seen:** after 15:30 the stock page showed TMCV at ₹420.00, NSE `lastPrice` (the last trade). The official close was ₹421.65: NSE's close (VWAP of the last 30 min), the NSE history bar, BSE 421.60, and TradingView 421.65. The day change showed −10.15 (−2.36%) instead of −8.50 (−1.98%). Market cap, portfolio value, signals and alerts inherited the error.
- **Now (#134):** one display-price rule for NSE and BSE: last traded in session, official close once published, and change vs the adjusted base. It is applied to quotes, overview, portfolio, signals, alerts, monitor, dashboard and market cap. `finresearch audit prices` reconciles NSE quote vs history vs BSE vs intraday, fund NAVs and bonds. The UI shows 'Sources disagree' with both values when they differ.

### 2026-09-30: failures cached as 'no data' (#132)
- **Seen:** a one-off DNS failure reaching BSE was cached for 12 h, and the results card said BSE had no machine-readable results. BSE block pages (HTML with HTTP 200) could be written to the disk cache.
- **Now:** transient failures are classified and never cached. The UI distinguishes 'couldn't reach' from 'no data', and only JSON is written to disk caches.

### 2026-09-30: stuck background waiters matched their own command line
- **Seen:** three `while pgrep -f "ipo harvest"` loops kept running for 3.5 h after the harvest ended, because each loop's own command line contains "ipo harvest".
- **Now:** they were stopped. Wait on a PID (`wait`/`kill -0 <pid>`) or a result condition, never on `pgrep -f` of a string that appears in the waiter itself.

### 2026-10-01: IPO sizing told sNII applicants to bid one lot (#141)
- **Seen:** the IPO signal capped sizing at 1 lot for every category. An sNII bid must exceed ₹2 lakh (Orient: 14 lots = ₹2,09,440). When retail is below 1x, every bid is filled in full, not a lottery.
- **Now:** sizing and EV are per category; SKIP if capital can't meet the category minimum. Regression tests fail on the old code.

### 2026-10-01: forecasts that could be scored with hindsight (#141)
- **Seen:** a forecast made after the listing-day open (a report finishing on listing day) was scored; a NIFTYBEES split or a demerger in the window was scored on unadjusted prices.
- **Now:** such forecasts are voided with a reason.

### 2026-10-01: fund-file weights 100x off (#150)
- **Seen:** mixed '%' text and plain numbers in a weight column were scaled inconsistently. The shipped test asserted 0.40 for '40.00%' in a percent column.
- **Now:** percent text is converted once, and a '%'-text GRAND TOTAL is a percent total.

### 2026-10-01: an experiment wrote to the live database
- **Seen:** an agent started a test API without FINRESEARCH_DATABASE_URL, so it used the live DB and logged 4 forecast rows.
- **Now:** the rows were reviewed and kept (valid, pre-listing). Rule: every non-live server must point at a test DB (the brief now says so).

### 2026-10-01: fact-checking holes found in the verification audit (#158)
- **Seen:** the publish gate only checked `[C123]` citations, while the dashboard also links `(C123)`, `(C1/C2)`, `[C1, C2]` and a bare `C123`, so a report citing a contradicted claim in those forms published. The citation check's table-row fallback matched numbers as substrings ("12" inside "1,234"), so a made-up quote could be marked found. A verifier's verdict was applied to any claim of the run, not only those it was given. BSE exchange facts (`bse_equity`) were not deterministic, so a conflicting agent claim demoted them. Units such as "years" or "users" were read as rupees ("rs").
- **Now:** the gate checks every spelling the dashboard links (and warns to bracket them); quoted numbers must be whole numbers in the cited lines; verdicts outside the verifier's brief are ignored and logged; `bse_equity` is deterministic; "Rs"/"INR" are matched as words. Regression tests fail on the old code.

### 2026-10-01: portfolio audit: phantom losses, double-counted openings, STT in proceeds (#161)
- **Seen (synthetic data):**
  - A sale with no price booked a 100 % loss.
  - A second CAS's opening balance became a second inflow in the value history and blocked XIRR.
  - CAS STT reduced redemption proceeds, which s.48 (fifth proviso) does not allow.
  - Month-first tradebook dates were half mis-read.
  - Older units before a tradebook ignored a split inside it: 50 units instead of 10.
  - Statements imported between 00:00 and 05:30 IST were dated the previous (UTC) day.
  - A partial sync lost trades.
  - Term cover counted goal money twice: a ₹2 lakh gap where the gap is ₹6 lakh.
- **Now:** each is fixed, and a regression test in tests/test_portfolio_audit.py, test_connectors.py or test_wealth_api.py fails on the old code.
### 2026-10-01: first portfolio load took over two minutes (#159)
- **Seen:** on a test server with 22 synthetic holdings (one unknown NSE symbol), the first GET /api/portfolio took 140.8 s. NSE's quote API answers an unknown symbol with a fast 404, but the batch re-warmed from that symbol's quote page, which NSE never answers (ReadTimeout), retried 3 times at 30 s each. The page's stream and the valuation after it each paid this.
- **Now:** a fixed warm-up page, no re-warm on a 404, 10 s / 1 retry per request, 20 s per quote, a 45 s budget per load, failed quotes remembered for 30 s. Same holdings: 14.4 s cold, 0.07 s warm.

### 2026-10-01: holidays read as trading days on a fresh install (#159)
- **Seen:** the monitor never fetched NSE's holiday list (`Deps.live()` did not enable it). A test server with no cached list reported the next open as Friday 2-Oct-2026, which is Gandhi Jayanti.
- **Now:** the monitor refreshes the list using the IST year. If the year is missing it retries hourly, and /api/market/status reports `holidays_known` and a warning.

### 2026-10-01: listing price taken from the wrong day (#159)
- **Seen:** a listing-day quote failure (a timeout or 403) became "not listed yet" and was retried the next trading day, which recorded that day's open as the listing price. An NSE open of 0 would have been recorded as a −100% listing.
- **Now:** network errors are retried within 30 minutes. A quote from after the listing day uses the listing day's bar, or fails rather than record a wrong price.

### 2026-10-01: audit follow-ups (calibration n, stock event label, rule range, renamed symbols, bond range)
- **Seen:** /api/calibration counted one ledger row per day per signal, so one IPO listing viewed on several days counted several times, and daily stock forecasts were divided by a fixed 12 meant for monthly logging. A `p_listing_gain < 60` rule (meant as 60 %) made every IPO SKIP. A holding under a renamed NSE symbol showed "no price". An AVOID bond showed "0 %, range 0 %–0 %". The stock signal's docstring said the event left dividends out while the card and the resolver count them.
- **Now:** calibration counts independent events (latest forecast per IPO listing, non-overlapping 12-month windows per stock). Probability rule values outside 0–1 are refused on save; stored percents load as fractions with a warning. A 404 for a holding's NSE symbol looks its ISIN up and prices the new symbol with a "symbol changed" note. A bond event decided by arithmetic shows no range and is labelled rule-based. The stock docstring and base-rate text say the backtest is a price-return proxy for the dividend-inclusive event.

### 2026-10-01: ELSS lock-in (#176)
- **Seen:** tax-loss and gain harvesting suggested redeeming ELSS lots that were long-term for tax (over 12 months) but still inside the 3-year lock-in. On a synthetic fund with lots from 10-Aug-2023 and 10-Jun-2024, valued 30-Sep-2026, it suggested 20 units / ₹600 of tax-free gain, and the registrar would refuse 10 of those units. The pre-trade checklist recognised ELSS only by "ELSS"/"tax saver" in the name, plus `PortfolioHolding.category`, which nothing writes. So an ELSS named without those words (AMFI files e.g. "HDFC Long Term Advantage Plan" under Equity Scheme - ELSS) was never checked, and selling its locked units passed.
- **Now:** harvesting drops lots still inside the lock-in, which leaves a FIFO prefix (10 units / ₹300). ELSS is recognised by AMFI's category (NAVAll, or the daily valuation cache), with the name as an unverified fallback, and the checklist caps a sale at the unlocked units. Regression tests: `tests/test_elss.py::test_harvest_never_suggests_locked_elss_units` and `::test_category_detects_an_elss_whose_name_does_not_say_so`.

### 2026-10-01: fund category rank (#177): empty TER file, peer headings, segregated names
- **Seen:** on 1-Oct-2026 AMFI's October TER file downloaded fine but had no rows, and `signals.fund._ter` only fell back to September on an exception, so every fund read "no TER" (first ranking run: 0 of 869 matched). `/api/funds/{code}/peers` matched AMFI's raw heading, so a fund under "Equity Scheme - Flexi Cap Fund" never saw the peers under "Equity Schemes - Flexi Cap Fund" (AMCs are moving to the new spelling; 26 vs 46 direct-growth Flexi Cap schemes), while a close-ended series and segregated-portfolio rows with the same heading counted as peers. A first "segregated" filter also dropped main schemes named "(No. of segregated portfolios- 3)".
- **Now:** an empty TER table falls back to the previous month (868 of 869, then 875 of 876 matched). `/peers` groups by `category_key` and uses the ranking's direct-growth filter; only a segregated portfolio's own row ("(Segregated - 06032020)", "Segregated Portfolio 1") is left out. Regression tests: `test_empty_early_month_ter_file_falls_back_to_the_previous_month`, `test_peers_endpoint_groups_amfi_spelling_variants_and_drops_close_ended`, `test_universe_keeps_one_direct_growth_row_per_scheme_and_maps_its_other_plans`.

### 2026-10-05: one position limit and one rebalancing band (#194, #195)
- **Seen:** /portfolio concentration flagged a single stock above a flat 10 % when the profile set no max position, while the stock signal's sizing and the pre-trade checklist used 5 / 8 / 10 % by risk appetite, so a low-risk profile saw a 7 % stock as fine on /portfolio and over the limit in the checklist. /wealth called a class outside its band at max(5 pp, 25 % of target) while the Rebalance card used the tighter min(): debt at 13 % against a 10 % target was "inside" on /wealth (band 5 pp) and "outside" on the card (band 2.5 pp).
- **Now:** `portfolio/limits.position_limit` (profile max position, else 5 / 8 / 10 %) feeds concentration, signal sizing, the checklist and the alert catalogue's defaults, and every place names the rule ("5 % — your risk profile: low"). `portfolio/limits.band_pp` (the tighter rule; 0 % target: absolute only; widths on the profile) feeds both /wealth and the Rebalance card. Regression tests: `tests/test_portfolio_limits.py` (including the /wealth vs card agreement on the 13 % / 10 % case), `test_concentration_uses_the_one_position_limit`, `test_bands_and_compare`.


### 2026-10-06: a correctly cited but wrong number passed the value check (#217)
- **Seen:** the verification audit (PR #160) left three gaps open in `value_in_source`, reproduced on synthetic RHP tables: a claimed −20.62 % growth verified against "Revenue growth (%) 20.62" (the sign was dropped: `candidate_forms` added `abs(value)`); ₹12,345.60 crore verified against "12,345.60" in a table headed "(₹ in lakh)" (every ₹ scale was tried, the header never read); a FY2026 claim of 10,234.50 verified against the FY2025 column of the same row; a consolidated claim verified on a standalone page. The other way round, real figures were called "derived": bracketed outflows "(2,727)" against a positive capex claim and the "3" in "2%-3%" (read as −3).
- **Now:** `verify/values.py` reads sign, unit and period column at the cited lines (see its docstring); mismatches demote the claim (even verified) and block a cited high-importance one; unit / period it cannot read are warnings, never a pass for high-importance claims. Regression tests: `tests/test_gate_values.py`. Replayed against the source texts of Infosys run 9 and Orient run 5: no previously matched claim flips to a mismatch; gold recall unchanged.
### 2026-10-06: fund look-through cap labels and the quant / Tata files (#214)
- **Seen:** the look-through's "Company size" list showed "Large Cap" and "Large cap" as two buckets: fund holdings take AMFI's list label ("Large Cap"), a direct stock not on the list falls back to `portfolio.valuation.cap_bucket` ("Large cap"). Parsing the real Aug-2026 files (read 06-Oct-2026): quant's sheet named the scheme after its description line and read the RATING column ("N.A.") as the industry; Tata's 65-scheme workbook named every scheme after the suitability blurb ("*Investors should consult…"), stopped at the repeated header above the debt block (so no NET ASSETS total: "no GRAND TOTAL row" on every sheet) and missed its "Portfolio as on 31-08-26" date.
- **Now:** `fincalc.lookthrough.cap_label` gives one spelling ("Large cap") in the exposure, the fund detail and the style series. The parser prefers an INDUSTRY over a RATING column, treats "N.A." as no industry, keeps reading after a repeated header with the same ISIN and weight columns, reads two-digit years, picks a fund-like title line, counts ETF units under "equity" as fund units, and turns quant's "Total Exposure due to futures (non hedging positions)" (25.3 % in quant Flexi Cap, Aug-2026) into a file warning. Regression tests: `test_cap_buckets_have_one_spelling`, `test_quant_layout_*`, `test_tata_layout_*` (synthetic workbooks in the houses' layouts; fail on main).

### 2026-10-05: end-to-end retest of v0.14.0 → v0.16 (PRs #155–#203) (#200)
- **How:** live app (web 3100, API 8710) with GET-only public pages: dashboard, IPOs, stocks list, INFY, ULTRACEMCO, ORIENTCABL, funds list, Parag Parikh Flexi Cap (122639), bonds list and two bond pages, F&O, signals, runs, help, each at 1440 px and 390 px through headless Chrome (console errors, failed requests, page width, clipped text). Personal pages only on a separate server (API 8711, web 3005, its own database) with synthetic files: a Groww order history XLSX (incl. a BSE-only stock under Groww's "NSE$"), a Groww holdings statement XLSX (ISIN only), a Zerodha tradebook CSV, a Groww P&L report, an old .xls, a fake encrypted PDF, the synthetic AIS JSON, plus manual ELSS SIP lots and a flexi-cap fund.
- **Passed:** no console error or failed request on any live public page; no page wider than 390 px on the live public pages. Official close shown after the session; peers card (15 of 15 in the NSE basic industry); disclosures and red flags; earnings-surprise card labelled experimental; the stock signal card says "Informational — no proven edge" with its base rate; signals page shows stock rows as informational. Fund rank (46 direct-growth Flexi Cap funds) and the consistency card (45 peers + the fund) agree on the universe; TER peers 34 vs 33 + 1. Bond YTMs sane (7.35 %, 8.91 %, 6.00 %), no "0 %–0 %" range. Drop zone: all files previewed, the P&L report and .xls refused with their own messages, the wrong PDF password named as such, the AIS previewed and imported; holdings, ELSS badge and lock-in, fund rank badge (after `finresearch fund rank`), Rebalance card (bands from the profile; sold, tax, cess, LTCG exemption, stamp duty and ELSS lock-in recomputed by hand), tax tab and AIS check (FY 2025-26 purchases ₹24,000 and ₹16,200 match the lots), journal drafts from imported trades, pre-trade checklist with "5 % — your risk profile: low" (8.2 % and 2.5 % → 9.9 % recomputed), behaviour report, /wealth bands equal to the Rebalance card's, alert templates, morning brief.
- **Fixed:** (1) the "Largest position" alert held a diversified fund to the single-stock limit (a 9 % fund fired "Position too big" on a medium profile): `max_position_pct` now counts single stocks only, a new `max_fund_pct` metric and "One fund or ETF too big" template use `portfolio.limits.FUND_LIMIT_PCT` (25 %), and /portfolio concentration no longer flags an ETF as a stock. (2) Clearing a band or F&O field on /profile sent "" and the save failed: blank now means the default (API and form). (3) The holding signal alert said "signal action changed"; it now says the tilt changed (stocks: informational). (4) A BSE-only stock stored as Groww's "NSE$" was priced from NSE (₹1,741.30 last trade instead of BSE's ₹1,723.50 official close): importers no longer store placeholders as NSE symbols, a BSE-only company ISIN is quoted on BSE, and after the session BSE's official close beats an NSE last trade. (5) Holdings with only an ISIN (broker holdings statements) had no disclosures, red flags ("unavailable"), signal or events, and an "NSE:" key read as unavailable too: an ISIN map (NSE + BSE listings by ISIN) is stored as a market feed and resolves them; the daily pass now prices with ISIN lookups. ETFs (INF… ISINs, not in NSE's equity list) are never treated as BSE-only. (6) The XIRR tile blamed "unknown cost or price" for broker baselines; it names the real reason (unknown purchase date). (7) At 390 px the Allocation tab was 859 px wide (auto-sized grid columns), the tax tab 516 px; donut legends were clipped; Segmented controls could not wrap. (8) A holdings statement's reconciliation showed "— —" for its units. (9) Listing day: Orient Cables read "−10 % today … previous close ₹272 adjusted for today's corporate action"; ₹272 is the issue price and the change is from the ₹450 call-auction price, now labelled so. (10) The signal card showed two different "base rate" figures; they are now "all stocks" and "similar cases". (11) The fund rank's "consistency" uses 1-year windows while the Consistency card defaults to 3-year windows (60 % vs 16 / 16); the label says so.
- **Not verified / open:** personal pages of the live app (left to the lead); the surveillance and pledge feeds on the synthetic server (never fetched there, so red flags read "unavailable", correctly); a real CAS/e-CAS PDF and a real AIS. Open points: the largest-sector metric counts an ETF's NSE industry ("Mutual Fund Scheme - ETF") as a sector; the behaviour report shows "still held 0" without a value history.

### 2026-10-06: unknown holding period shown as ₹0 tax (#213); split and closed lots (#221)
- **Reproduced (synthetic):** a broker baseline of 10 units at ₹100 (cost known, date unknown) plus 10 bought on 10-Jan-2025; a pre-trade sale of 15 at ₹150 showed tax ₹0 / status "ok": the undated lot's ₹500 gain was neither short- nor long-term, so `tax_delta` ignored it and the item only counted lots with an unknown *cost*. The Tax tab computed an `unknown` count but never showed it. Now: status unknown, value null, "₹500 of gain) can't be classified" (tests/test_tax_safety.py).
- **Reproduced (synthetic):** buy 2 + buy 10, sell 4 (the 2-unit lot sells out), split x5: the sold-out lot kept `quantity` 2 instead of 10, so acquired ≠ open + disposed in today's units. Fixed in `portfolio.lots.build_lots` (tests/test_invariants.py).

### 2026-10-07: rate limit per client instead of per host (#246); plaintext secrets in the database (#245)
- **Reproduced (synthetic, offline):** six PoliteClients in three threads (each with its own event loop, like the API's threadpool) fetching one NSE host at a configured 20 req/s: with per-instance limiters the first six requests left together (gaps ~0 ms), so N clients made N x the rate (~28 req/s against NSE's 2 in the review's count). Now every gap is ≥ 50 ms, also across two processes (tests/test_rate_limit.py).
- **Reproduced (synthetic):** after saving Groww/Zerodha credentials, a CAS password, a broker token and ntfy/Telegram tokens, `SELECT config::text, token FROM broker_connection` and `value::text FROM notification_setting` contained every value in plain text (so every pg_dump did). Now none appears; rows that an older version wrote are moved by `finresearch secrets migrate --apply` (tests/test_secrets.py).
### 2026-10-07: research honesty review (#241, #242, #243, #244)
- **Reproduced (recorded fixture):** infosys run 9's stock report said **ACCUMULATE** with an entry zone, shown green in the report hero, while the stock signal itself was "Informational — no proven edge". Now shown as "Favourable (research view, informational, no validated edge; the report said ACCUMULATE)" with the price range relabelled as context (tests/test_research_views.py).
- **Reproduced (recorded fixtures):** high-importance web claims marked "verified" by the LLM verifier had `quote_found` null (never checked) yet passed the publish gate exactly like document-checked claims; a citation typed as "fincalc: ..." was labelled "Deterministic calculation". With evidence grades the four recorded live runs block on exactly those claims (8, 7, 37 and 26 of them), and a free-text calculation note is grade U (tests/test_evidence_grades.py, tests/test_replay.py).
- **Reproduced (code):** the second-opinion verifier ran the same role, prompt and model as the first and was sent the claim's status, value and the first verifier's evidence (`_claims_text(ids=high)`). Now blind, on another tier (tests/test_blind_verifier.py).
- **Reproduced (code + live DB read-only count):** the IPO listing model trains on final subscription books (published after the 17:00 UPI cut-off); `subscription_snapshot` holds 85 intraday snapshots for 11 issues from 28-Sep-2026 against 461 mainboard history rows, so it cannot be retrained on decision-time books yet: labelled and gated (tests/test_timing.py).
- **Not verified:** no live research run was started (agent rules); the `fetch_page` tool, the blind verifier and the stricter publish gate were exercised only with recorded fixtures and the fake CLI. The F&O page with a live FBIL curve was not opened.

### 2026-10-07: unknowns shown as numbers (#238), two value histories (#239), golden tax corpus (#240)
- **Reproduced (synthetic, tests/test_unknowns.py):** (a) holdings never valued + ₹3 lakh cash: net worth showed portfolio ₹0 with nothing said; (b) a holding priced only by a 2-month-old broker statement close: `complete` true, XIRR reason empty, data health 100 % priced; (c) an opening balance of unknown cost sold in full: realised P&L ₹0 and `complete` true; (d) an undated broker baseline sold this FY: `advance_tax_due_inr` ₹0 "below the ₹10,000 threshold"; (e) switch cost and churn cost with an unclassified sale in the year: plain figures, no flag. All now unknown/flagged as described in PRODUCT_REQUIREMENTS.
- **Reproduced (synthetic, tests/test_portfolio_analytics.py):** a snapshot of ₹1,10,000 on a day the reconstruction values at ₹1,02,625 was never noticed; the drawdown alert and the risk tab read different series. Now reconciled with the reason; the drawdown alert reads the reconstruction.
- **Reproduced (golden case g09):** SGB redeemed by RBI: the FY's `exempt` total was ₹0 instead of the ₹90,000 exempt gain.
### 2026-10-07: cross-source duplicates (#236) and unsupported corporate actions (#237)
- **Reproduced (synthetic, on main 7e5d13d):** 20 units instead of 10 in five cases: (A) Zerodha API sync of a buy, then the Zerodha tradebook with the same fill (added 1, duplicates 0); (B) a manual buy under "Manual", then a Zerodha API sync of the same trade (two accounts, never flagged); (C) a manual buy under "Zerodha", then the tradebook; (D) a Kite Coin fund baseline, then a CAMS CAS of the same fund (100 + 100 units); (E) a Groww API sync, then the Groww order CSV (Groww's own order id vs the exchange order id). The tradebook preview showed the duplicate as "new". Also found: a Zerodha tradebook buy of 10 followed by an API sync of a *different* exchange trade of 10 on the same day was skipped as "same day and units" (a real trade lost). All now covered by `tests/test_dedupe.py` (15 of 17 failed on main; the two that passed are guards: two real buys in one file, two broker accounts).
- **Reproduced (synthetic):** an NSE "Demerger" row for a held stock was dropped by `parse_action` without a warning and the following FY's tax stayed `complete`; a face-value consolidation (Rs 1 → Rs 10) was dropped too. Now recorded as pending and the year is incomplete until resolved (`tests/test_corp_actions.py`).

### 2026-10-07: GET routes that wrote state; holiday list failing open; embedding outage dropped documents (#247, #248)
- **Reproduced (synthetic, test DB):** `GET /api/portfolio` and `/api/portfolio/health` upserted `portfolio_snapshot` (the drawdown and drift alert inputs); `GET /api/journal/notes` and `/summary` created draft entries and moved the sync cursor; `GET /api/signals/stock/{symbol}` (and the IPO signal) logged a forecast on every view; `GET /api/watches/{id}/live` inserted a subscription snapshot. Now each GET writes nothing and the tests fail on the old code (test_portfolio_prices, test_journal_api, test_stock_signal, test_ipo_signal, test_live).
- **Reproduced:** with no NSE holiday list cached, the stock peers, disclosures and IV jobs ran on any weekday, and they ran on cached holidays too (they only checked for weekends). Now they skip holidays, fail closed on an unknown list and raise one alert a day (test_holidays).
- **Reproduced:** `index_document` with an embedder that fails on its second batch raised, and discovery rolled the whole document back. Now the document is stored keyword-only and found by full-text search (test_retrieval).
- **Found by the new eval:** full-text search alone finds only 8 of 16 natural-language gold queries in the top 5 (`websearch_to_tsquery` ANDs every word). Not changed here: see the PR's "needs decision".

### 2026-10-09: unknown read as ₹0 or ok in goals, XIRR, the week's change and data health (#262, #263, #264)
- **Reproduced (synthetic, tests/test_unknowns.py):** holdings never valued and a goal earmarking 50 % of the portfolio: `funded_now` was ₹10,000 (other savings only, the portfolio counted as ₹0) and the goal plan simulated from it; an incomplete valuation was simulated as if complete.
- **Reproduced (tests/test_corp_actions.py):** a held stock with a pending demerger still showed a per-holding XIRR (−12.75 %) and fed the overall XIRR; the corporate-actions health row read "ok, 100 %" for stocks whose actions were never synced.
- **Reproduced (tests/test_value_history.py):** a stored value history made out of date by a backdated trade left the dashboard week, the digest and the brief's daily line empty with no reason; a history ending 5 days ago still printed a two-day "week".
- **Reproduced (tests/test_monitor.py, tests/test_bse_only.py, tests/test_sector_labels.py):** listing alert "open ₹1300.250000 (… ₹272 upper band)"; look-through's top holdings showed NSE's "Mutual Fund Scheme - ETF" and BSE's "-" as sectors (the open point of 2026-10-0x above).

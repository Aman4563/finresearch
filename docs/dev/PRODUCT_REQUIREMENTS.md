# Product requirements and status

This file is the **status authority**. Each requirement has an ID, a status, and a dated **verification log**. Append rows and never rewrite earlier ones.

Statuses:
- `planned`: not started;
- `in-progress`;
- `delivered`: shipped and verified;
- `partial`: core shipped, with the remainder named;
- `deferred`.

## Requirements

### Engine (Claude Bridge)
| ID | Requirement | Status | Milestone |
|---|---|---|---|
| BRIDGE-001 | Tier 1 runs every LLM task through the official Claude Code CLI on the Max subscription, with JSON-schema outputs and transcripts | delivered | v0.1.0 |
| BRIDGE-002 | Subscription-window tracking (5h/7d), pre-flight ceilings, cool-down to reset, circuit breaker | delivered | v0.1.0 |
| BRIDGE-003 | Tier 2 (API key), wired but disabled until a key is configured | delivered (disabled) | v0.1.0 |
| BRIDGE-004 | Tier 3 local fallback (Ollama) sized for 16 GB, with schema validation and repair, degraded flag and capability refusal | delivered | v0.1.0 |

### Documents and data
| ID | Requirement | Status | Milestone |
|---|---|---|---|
| DOC-001 | sha256-immutable document store; pdftotext pages; OCR of scanned pages; grep-compatible canonical text | delivered | v0.1.0 |
| DOC-002 | SEBI ICDR section mapper for RHP / DRHP with line and page ranges | delivered | v0.1.0 |
| DOC-003 | Line-anchored chunks, local embeddings, hybrid search | delivered | v0.1.0 |
| DOC-004 | Deterministic rebuild of offer-document financial tables | delivered | v0.1.0 |
| DATA-001 | NSE IPO adapters (current, upcoming, past issues, ipo-detail combined vs NSE-only) | delivered | v0.1.0 |
| DATA-002 | SEBI public-issue listings and full-PDF resolution | delivered | v0.1.0 |
| DATA-003 | Automatic discovery and download of a company's IR documents (annual reports, audited FS, anchor, price-band ad) | delivered | v0.2.0 |
| CALC-001 | fincalc deterministic finance library, golden-tested against the manual reports | delivered | v0.1.0 |

### Tools and ledger
| ID | Requirement | Status | Milestone |
|---|---|---|---|
| MCP-001 | FinResearch MCP server: documents, sections, grep, search, tables, fincalc, NSE/SEBI | delivered | v0.1.0 |
| LEDGER-001 | Claim ledger with deterministic citation verification (quote at cited lines) | delivered | v0.1.0 |

### IPO report engine (P1)
| ID | Requirement | Status | Milestone |
|---|---|---|---|
| AGENT-001 | Agent definitions and skills: planner, 7 research streams, adversarial verifiers, bull/bear, synthesizer, critic | delivered (all roles live-verified in Orient Cables run 5) | v0.2.0 |
| AGENT-002 | Python DAG orchestrating the agents through the bridge, with Max-limit-aware concurrency, idempotent stages and resume | delivered | v0.2.0 |
| VERIFY-001 | Numeric verification gate: recompute and cross-check every numeric claim with fincalc; block contradicted claims | delivered | v0.2.0 |
| REPORT-001 | Renderer: the 12-section report (MD/HTML/PDF, atomic and validated), XLSX/CSV tables, charts, and the 01_…06_ folder pack | delivered | v0.2.0 |
| EVAL-001 | Gold-set evaluation: regenerate Moneyview and Orient Cables and compare with the manual fact-check logs | delivered (Orient Cables live; Moneyview gold set scored offline) | v0.2.0 |

### Research app (P2)
| ID | Requirement | Status | Milestone |
|---|---|---|---|
| APP-001 | Local API (127.0.0.1 only): companies, documents, runs, steps, claims, reports with gate, packs, limits; runs started as CLI worker processes; live SSE run events | delivered | v0.3.0 |
| APP-002 | Dashboard: IPO radar, run launcher, live agent view | delivered | v0.3.0 |
| APP-003 | Report reader with citation hover and evidence panel | delivered | v0.3.0 |
| APP-004 | Ask Claude about a report, grounded in its claim ledger | delivered | v0.3.0 |
| APP-005 | Investor profile, deterministic personal rules, suggestions and decision journal | delivered | v0.3.0 |
| APP-006 | Plan usage and limits dashboard | delivered | v0.3.0 |
| APP-007 | Post-report monitoring: subscription to close, allotment, listing, lock-ins | delivered | v0.3.0 |

### Quality and scale (P3)
| ID | Requirement | Status | Milestone |
|---|---|---|---|
| EVAL-002 | Offline replay of real runs in CI (gold scorer + publish gate) and a verdict-vs-listing back-test | delivered | v0.4.0 |
| SME-001 | SME IPOs (NSE Emerge / BSE SME): documents, checks and report | delivered (NSE Emerge; BSE SME not covered; no live SME report run under the one-IPO rule) | v0.4.0 |
| NEWS-002 | Hindi business news in the 30-day news stream | delivered | v0.4.0 |

### Listed stocks (P4)
| ID | Requirement | Status | Milestone |
|---|---|---|---|
| KIND-001 | Research-kind registry: one shared pipeline, kinds configure streams, role slots, primary documents and facts | delivered | v0.5.0 |
| STOCK-001 | Listed-stock data: quotes, price history, results, shareholding, corporate actions | delivered | v0.5.0 |
| STOCK-002 | Stock research report end to end, with a gold set and one live run | delivered | v0.5.0 |
| STOCK-003 | Stocks in the app: search, watchlist, results and corporate-action monitoring | delivered | v0.5.0 |

### Mutual funds (P5)
| ID | Requirement | Status | Milestone |
|---|---|---|---|
| FUND-001 | AMFI NAV data, fund analytics (returns, rolling returns, XIRR, SIP, risk-adjusted returns, costs) and the fund research report | delivered | v0.6.0 |

### Bonds (P6)
| ID | Requirement | Status | Milestone |
|---|---|---|---|
| BOND-001 | Listed bonds / NCDs: NSE listing data, bond arithmetic (YTM, accrued, duration, convexity, after-tax yield) and the bond research report | delivered | v0.7.0 |

## Verification log

| Date | ID(s) | Evidence |
|---|---|---|
| 2026-09-28 | BRIDGE-001..004 | `scripts/smoke_live.py` all passed. The Max tier returned schema output (Haiku) with 5h/7d utilisation. A simulated limit routed to local and was marked degraded. 20 offline bridge tests. Commit 74a8969. |
| 2026-09-28 | DOC-004, BRIDGE-004 | After normalisation with `layout_table`, `qwen3.5:4b` extracted all 8 Orient Cables P&L values exactly. Fixture tests on both the Moneyview and Orient layouts. |
| 2026-09-28 | DOC-001..003 | Orient Cables RHP: 491 pages ingested in 6.6 s (14 OCR'd), 1,294 chunks embedded. Sections verified against hand-found lines on the Moneyview and Orient RHPs and DRHPs. Hybrid search found the Orient Electric dispute passage. |
| 2026-09-28 | DATA-001..002, CALC-001 | 25 adapter tests (recorded fixtures) plus live runs (NSE ipo-detail ORIENTCABL/MONEYVIEW, SEBI RHP listing and PDF resolution). 57 fincalc tests reproducing report values (e.g. Moneyview mcap ₹5,984.79 cr, Orient fresh shares 1,17,64,705). |
| 2026-09-28 | MCP-001, LEDGER-001 | `scripts/smoke_mcp_live.py`: Claude Sonnet 5 on Max, using only the MCP tools, saved "largest customer 38.54% (Q1 FY27)" citing RHP p26 L1663; `quote_found=true`; 6 turns, 12 s. DB tests cover a fabricated quote being marked `unsupported`. Commit 3da074b; 111 tests; CI green. |
| 2026-09-28 | AGENT-001 | Live financials stream on Orient Cables (run 2): Sonnet 5 on Max, 65 turns, 267 s, 16 claims with 22 citations, all quote_found. Reproduced the manual findings (revenue CAGR 33.46%, negative CFO FY25–FY26, net debt/EBITDA 0.49x→2.31x→4.67x, CARO receivables gap ₹348.45 mn, largest customer 25.75–38.57%) and added two new ones (₹549.73 mn supplier-finance facility booked in CFF; standalone vs consolidated comparability). One stream used about 8% of the 5-hour window. The least-privilege settings denied an out-of-role `start_run` call. 29 agent tests. |
| 2026-09-28 | AGENT-002, AGENT-001 | Live end-to-end `finresearch ipo run orient-cables --streams financials,demand --concurrency 2 --wait` (run 3): 16 steps, all done (planner → 2 streams → 2 verifiers → bull/bear → synthesis → critic ×2 with 2 follow-up rounds). Max 5-hour window 12%→27%. The verifiers contradicted 14 claims, all real errors (see #5). The report: 3,294 words, 12 sections, 28 cited claims, verdict APPLY-CONDITIONAL (QIB ≥1x and GMP ≥₹50 by 15:30 on 29-Sep; UPI before 17:00) and AVOID long term. This matches the manual analysis. 5 pipeline tests (order, resume after a crash, pause and resume on a limit, budget pre-flight, critic follow-up). |
| 2026-09-28 | VERIFY-001 | Deterministic gate (value-at-cited-lines with ₹/%-unit conversion, cross-claim conflicts, live-figure timestamps and staleness, bidding-day labels), atomic numeric claims, verifier corrections re-checked against the source, a second independent verifier for high-importance claims, a cross-stream conflict pass, and a publish gate with revision rounds (blocked report → `report_blocked.md`). Tests are seeded with real mistakes (weekend Day 3, stale GMP, ₹mn/₹cr scale, 80.36 vs 80.35, conflicting promoter holding, contradicted and raw citations). A replay on live run 3's report blocked its 10 raw `[RHP L…]` citations and flagged 63 uncited figure lines. 156 tests. |
| 2026-09-28 | REPORT-001 | `finresearch ipo render 3` built the live run 3 pack: 32 files, a 17-page PDF (re-rendered over itself and still valid, Quick Look preview OK), source documents copied into 01/02/05, sections, sources, claims.xlsx/.csv and a fact-check log. The gate blocked it, so it is rendered as NOT PUBLISHED with the blocking reasons. Tests: citation links and statuses, evidence appendix, financial pivot (verified first, contradicted excluded), pack layout, published vs blocked naming, and the PDF re-render regression. The pipeline renders automatically at the end of a run. 162 tests. |
| 2026-09-28 | DATA-003 | `finresearch docs discover acevector --no-agent` (live): NSE issue information gave 2 ZIP archives (RHP 575 pages + NSE General Information Document 50 pages, anchor letter 2 pages), all ingested and indexed. The SEBI copy of the RHP was recognised as a duplicate by sha256. Each document stores URL, HTTP status, fetched_at, content type, the ZIP member name and the discovery source. Fixed live: indexing inside the event loop, and the GID being tagged as an RHP. `ipo run` now discovers documents when no RHP/DRHP is stored. Orient Cables with the discovery agent (live, 25 min): 42 new documents from the company's IR "material documents" page (annual reports, the industry report, auditor examination report, WACA/KPI/basis-of-price certificates, offer agreements, SEBI observation letter) plus the NSE archives; the stored SEBI RHP and DRHP were recognised as duplicates. Separate audited financial statements were not listed on that page (already in the store from earlier ingestion). Fixed from this run: the NSE copy of the RHP (byte-different, 480/491 identical pages) is now a same-text duplicate, and `Orient_GID.pdf` is classified OTHER. Moneyview was not re-run live (one-IPO rule); 10 offline discovery tests. |
| 2026-09-28 | APP-001 | `finresearch serve` on 127.0.0.1:8710 against the live database: companies (3), run 5 detail while running (28 steps, 373 claims, 61% of the 5-hour window, 1,783 turns), run 4 report (published, 209 cited claims with evidence), SSE snapshot for run 5. Host-header allow-list rejects non-localhost hosts; pack and document files are served only from the data directories. 9 offline API tests (spawned workers are faked; argv equals the CLI's `ipo resume <id> --wait`). |
| 2026-09-28 | APP-002, APP-003, APP-006 | Next.js 16 dashboard in `web/` (localhost:3100) against the live API: the radar lists 11 open NSE issues (deduplicated, phase from IST dates, SME marked) and links Orient Cables to run 5; the run page follows run 5 over SSE; the report reader renders run 4's report with 209 status-coloured citation chips, hover previews and an evidence panel with the cited source lines; usage shows the 5h/7d meters. Fixed live: a malformed citation URL crashed the reader, and `fincalc:` citations are now shown as calculations. `scripts/smoke_web.sh 4`: 5/5 pages without browser errors. CI builds, type-checks and lints the app. |
| 2026-09-28 | APP-004 | Live on run 4 through the API: "largest customer share Q1 FY27 vs FY26" was answered by Sonnet 5 on Max in 8.5 s (2 turns), citing C97/C99/C100/C101, with all checks passing. A follow-up in the same conversation resumed the Claude Code session (it understood "the same two periods"), cited C103/C105, and was correctly flagged for restating two figures without a citation. The agent has read-only tools (documents, fincalc, `list_claims`; no web, no `save_claim`). 2 offline tests: resume id, unknown, contradicted and non-existent line citations, uncited figures, foreign conversations. |
| 2026-09-28 | APP-005 | Live on run 4 (`POST /api/runs/4/suggest`, 32 s, Opus 5.5 on Max): NSE's combined table at 17:00 IST showed QIB 0.057x, NII 17.37x, RII 9.16x, total 8.32x. The default `qib-floor` rule fired, so Python forced SKIP with 0 lots. The advisor independently said SKIP (high), cited the report's own condition [C339], and noted the fading GMP [C375][C530]. Run 4 predates baseline facts, so lot size and price band now fall back to the run's NSE issue information: lot cost ₹14,960 = 1 lot on ₹15,000. 12 offline tests: rules fire, clear or stay unknown; SEBI retail/sNII lot caps; enforcement overrides the agent; outcome returns via fincalc (listing +10.29%, exit +13.97%, ₹2,090). `scripts/smoke_web.sh 4`: 7/7 pages. |
| 2026-09-28 | EVAL-001, AGENT-001 | Full live Orient Cables run 5 (`finresearch ipo run orient-cables --wait`): 44 steps, 2,661 turns, 174 min of agent time, 80% of one Max 5-hour window. 434 claims (316 verified, 39 contradicted by the verifiers, 17 unsupported). The publish gate passed after revision rounds. `finresearch eval gold 5`: **22/22 key facts found and verified (100%), 0 gold facts contradicted by the report, verdict APPLY-CONDITIONAL agrees with the manual report, release bar PASS.** Run 4 (before the baseline facts) scored 82%; its 4 misses were price band, lot, fresh issue and issue size, now recorded deterministically from NSE issue information. Pack: 83 files, 65-page PDF (pypdf strict, one %%EOF). Only Orient Cables was run live (one-IPO rule); the Moneyview gold set is validated by offline tests. |
| 2026-09-28 | Release v0.2.0 | Milestone closed (16/16 issues). Release bar met on the live Orient Cables run 5: 100% key-fact recall, 0 gold facts contradicted, publish gate passed. `pytest` 206 passed; CI (tests with pgvector, web build, full-history gitleaks) green. |
| 2026-09-28 | APP-007 | Live: `finresearch monitor watch orient-cables` read NSE's issue period (25–29 Sep) and anchor book (60,88,233 shares), then scheduled 11 checks from the next pass: 6 subscription checks on the close day, allotment 30-Sep 19:00, listing open and close (expected 2-Oct, T+3 without holidays; the check moves to the next exchange day until NSE lists the stock), and anchor lock-ins on 30-Oct and 29-Dec. 6 offline tests: the schedule; final-day checks run once per slot and store one snapshot per NSE timestamp; a personal rule that changes status raises an action alert (qib-floor fired → clear at 1.25x); listing on a holiday retries next exchange day, then fills the journal (+10.29%); lock-in and allotment alerts; failures retry then alert. The scheduler runs inside `finresearch serve` or `finresearch monitor run`. `scripts/smoke_web.sh 5`: 9/9 pages. |
| 2026-09-28 | Release v0.3.0 | Milestone closed (7/7 issues: #17–#23). The app runs locally: `finresearch serve` (API, SSE and monitor on 127.0.0.1:8710) and the dashboard (127.0.0.1:3100) were started from the main tree. Orient Cables is watched, with its first close-day subscription check at 10:30 IST on 29-Sep. `pytest` 213 passed; CI green. |
| 2026-09-29 | EVAL-002 | `finresearch eval export 5` wrote live run 5 as a 677 KB text fixture (434 claims with citation checks, 44 steps, final synthesis; no documents or local paths). `tests/test_replay.py` imports it with remapped claim ids in CI and asserts 22/22 recall, 0 contradicted, verdict agreement and a passing publish gate. `finresearch eval backtest` compares each report's verdict with the listing outcome from the monitor or journal; runs 3–5 are pending until Orient Cables lists. |
| 2026-09-29 | SME-001 | Live `docs discover bench-mark-infotech --nse-symbol BMISL --no-agent`: `ipo_detail` fell back to series SME and found 3 NSE archives. Ingested: the 382-page RHP, the GID (stored as OTHER), the anchor letter and three price-band advertisements ("Ratios / Basis of Issue Price"). SEBI has no SME filings. NSE's SME category table publishes no offered shares and prints 0.00x, so category times are now unknown rather than zero; the monitor records the overall times from NSE's current-issues list. Rules and lot limits follow the two-lot minimum above ₹2 lakh (effective 1-Jul-2025), and the skill tells agents to check SME terms in the RHP. 7 offline tests use recorded BMISL and PAPADMALJI payloads. |
| 2026-09-29 | NEWS-002 | Live news30 stream on Orient Cables (run 7, Sonnet 5 on Max, 42 turns, 163 s, about 5% of the 5-hour window): 12+ English and 4+ Hindi searches found 4 relevant Hindi articles (Hindi Business Standard, Prabhat Khabar and others). The agent reported that the Orient Electric trademark dispute is covered only in English media. C985 cites hindi.business-standard.com with its access time and a Hindi quote, and was flagged `source_language: hi`. The agent did not mark its statement as a translation even when told to, so the marker is now added when the claim is saved; the gate warns about unmarked Hindi claims cited in a report. 2 offline tests. |
| 2026-09-29 | Release v0.4.0 | Milestone closed (3/3: #24 offline replay and back-test, #25 SME IPOs, #26 Hindi news). `pytest` 227 passed; CI (tests, web, secret scan) green. |
| 2026-09-29 | KIND-001 | `orchestrator/base.py` holds the shared DAG (budget, gate, revisions, critic rounds); `IpoPipeline` adds only its streams, offer documents and NSE facts. `orchestrator/kinds.py` maps `ResearchRun.kind` to the pipeline, and the CLI and API validate streams per kind. No behaviour change: all 227 existing tests pass, including the CI replay of live run 5 (22/22, 0 contradicted). A test kind with its own streams and no required documents runs the whole DAG through the fake runner. |
| 2026-09-29 | STOCK-001 | Live on INFY: price history 1-Jun to 28-Sep (70 bars; return −4.58%, annualised volatility 31.5%, max drawdown −16.0%, computed with fincalc), shareholding (promoter group 13.82% at 30-Jun-2026), corporate actions (dividends ₹25 and ₹23 parsed; buyback without an amount) and announcements (the Q1 FY27 results PDF of 23-Jul-2026 found). The classic `quote-equity` and `historical/cm` APIs refuse scripted clients (403/503); the quote page's own API works with the page as referer. NSE's results index lags (latest indexed filing Dec-2024), so results PDFs come from the announcements. Results XBRL parser: Infosys Q3 FY25 revenue ₹41,764 cr, PAT ₹6,822 cr, EPS ₹16.43; the year-to-date period is read from the filing's own facts because the context's period is wrong. 6 offline tests on recorded payloads, with hand-checked golden values for the market maths. |
| 2026-09-29 | STOCK-002 | Live Infosys stock runs on Max (one-instrument rule). **Run 8:** gate passed, verdict ACCUMULATE, but only 11/23 key facts: the fundamentals stream skipped most P&L and balance-sheet lines. **Run 9**, with a line-item checklist in the fundamentals prompt: 44 steps, 188 min, 59% of one 5-hour window, 376 claims (263 verified); **21/23 key facts found and verified (91%), 100% of high-importance facts, 0 contradicted, gate passed, release bar PASS.** Verdict ACCUMULATE (3–5 years) in ₹889–1,000 (12–13.5x TTM EPS), with a downgrade trigger at the Q2 FY27 results. Pack: 26 files, 56-page PDF. Discovery ingested the FY26 and FY25 annual reports and four results filings from NSE. The gold set (23 facts) was hand-checked against the FY26 integrated annual report and NSE. Run 9 is replayed in CI (`tests/fixtures/eval/infosys-run9.json`). |
| 2026-09-29 | STOCK-003 | Live on the real database through the API (127.0.0.1:8711): searching "infosys" in NSE's list of about 2,600 listed equities (EQUITY_L.csv) ranked INFY first; `POST /api/companies` resolved the existing `infosys` company; `POST /api/watches` (kind stock) created the Infosys watch. The daily after-close snapshot fetched 6 price bars (28-Sep close ₹1,003.2), 2,927 announcements, 20 corporate actions and 13.82% promoter holding. The first check runs at 16:30 IST and records the baseline without alerting. `scripts/smoke_web.sh 9`: 10/10 pages, including /stocks and the Infosys stock report. The runs page links every research kind to its report; `finresearch research resume/status` work for any kind. 3 offline tests: search ranking, add and watch, and daily alerts (results filed, new corporate action, ex-date within 7 days, promoter holding −1.12 pp, a −6.30% day), each fired once and never repeated. |
| 2026-09-29 | Release v0.5.0 | Milestone closed (4/4: #36 research kinds, #37 stock data, #38 stock reports, #39 stocks in the app). Release bar met on the live Infosys run 9 (91% key-fact recall, 0 contradicted, gate passed), which is replayed in CI. `pytest` 269 passed; CI green. |
| 2026-09-29 | FUND-001 | AMFI adapters live: NAVAll (category and AMC per scheme), NAV history per AMC (AMC codes discovered once and cached), and point-in-time NAVs for all schemes. For Axis Midcap Fund Direct Growth (120505): 1/3/5-year returns 7.17% / 15.50% / 12.76%, 3-year rolling 13.9–25.9% (106 windows), volatility 14.6%, drawdown −20.3%, 7th of 19 mid-cap funds on 3 years; the peer table and NAV history now agree exactly. **Live fund run 10:** gate passed, verdict HOLD, 75% on the first gold set. The rolling statistics were not recorded, and one gold fact (NAV on an arbitrary date) was replaced by the regular-plan NAV. **Run 11**, recording the rolling statistics as baseline facts: 41 steps, 129 min, 41% of the 5-hour window, **8/8 key facts found and verified, 0 contradicted, gate passed, release bar PASS.** Verdict HOLD (existing holders, 5+ years; not first choice for new mid-cap money). Pack: 16 files, 25-page PDF. Replayed in CI. |
| 2026-09-29 | Release v0.6.0 | Milestone closed (#40 mutual funds). Release bar met on the live Axis Midcap run 11 (8/8 key facts, 0 contradicted), replayed in CI. `pytest` 302 passed; CI green. |
| 2026-09-29 | BOND-001 | NSE's list of bonds traded in the capital market (1,461 bonds; coupon, face value, last price, maturity, rating), with warnings for partly redeemed face values and stale interest dates. `fincalc.bonds`: a par bond yields exactly its coupon; the textbook 10% 5-year bond at 96.304 yields 11.00%; a 5-year zero has Macaulay duration 5.0. **Live bond run 12** on L&T Finance 8.98% NCD 2029 (INE027E07998, AAA): 30 steps, 98 min, 36% of the 5-hour window, **3/3 listing facts, 0 contradicted, gate passed, release bar PASS.** Verdict AVOID at ₹1,076: the after-tax yield for a 30% slab is below 4.08%, against 6.40% on an SBI FD and 6.623% on a 2-year G-sec. The coupon frequency (monthly, on the 13th) comes from secondary bond-data providers; no primary offer document was found. The verifier showed that YTM x (1 − t) overstates a premium bond's after-tax yield, so `after_tax_ytm` now taxes coupons and the pull-to-par separately. Pack: 15 files, 16-page PDF. Replayed in CI. |
| 2026-09-29 | Release v0.7.0 | Milestone closed (#41 bonds). Release bar met on the live L&T Finance NCD run 12 (3/3 listing facts, 0 contradicted, gate passed), replayed in CI. `pytest` 329 passed; CI green. |

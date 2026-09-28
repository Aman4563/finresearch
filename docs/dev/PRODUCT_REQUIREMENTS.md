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
| DATA-003 | Automatic discovery and download of a company's IR documents (annual reports, audited FS, anchor, price-band ad) | planned | v0.2.0 |
| CALC-001 | fincalc deterministic finance library, golden-tested against the manual reports | delivered | v0.1.0 |

### Tools and ledger
| ID | Requirement | Status | Milestone |
|---|---|---|---|
| MCP-001 | FinResearch MCP server: documents, sections, grep, search, tables, fincalc, NSE/SEBI | delivered | v0.1.0 |
| LEDGER-001 | Claim ledger with deterministic citation verification (quote at cited lines) | delivered | v0.1.0 |

### IPO report engine (P1)
| ID | Requirement | Status | Milestone |
|---|---|---|---|
| AGENT-001 | Agent definitions and skills: planner, 7 research streams, adversarial verifiers, bull/bear, synthesizer, critic | partial (definitions shipped; live-verified: financials stream) | v0.2.0 |
| AGENT-002 | Python DAG orchestrating the agents through the bridge, with Max-limit-aware concurrency, idempotent stages and resume | delivered | v0.2.0 |
| VERIFY-001 | Numeric verification gate: recompute and cross-check every numeric claim with fincalc; block contradicted claims | delivered | v0.2.0 |
| REPORT-001 | Renderer: the 12-section report (MD/HTML/PDF, atomic and validated), XLSX/CSV tables, charts, and the 01_…06_ folder pack | delivered | v0.2.0 |
| EVAL-001 | Gold-set evaluation: regenerate Moneyview and Orient Cables and compare with the manual fact-check logs | planned | v0.2.0 |

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

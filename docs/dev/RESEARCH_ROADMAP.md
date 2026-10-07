# FinResearch: gap analysis, accuracy and signal research, and roadmap

*Prepared 30-Sep-2026 for a single Indian retail investor on the Claude Max plan. Read-only review of the repository (v0.11.0, 485 tests), of the live app at 127.0.0.1:3100 and 127.0.0.1:8710 (GET only), and of published research. Sources are numbered in §F. Tags: **[V]** = verified this session from a primary or fetched source; **[U]** = cited from memory or secondary coverage, not re-verified; **[W]** = the evidence itself is weak or mixed.*

---

## 0. Summary

**What is strong already.**
- The claim ledger plus the deterministic gate is a better trust architecture than any retail tool I benchmarked.
- `fincalc` does all the arithmetic.
- There are gold sets with CI replays.
- Monitoring is aware of exchange holidays.
- Bond conventions are checked against primary sources.

**Biggest gaps.**
1. **No portfolio.** The app knows nothing about what the user owns: no lots, P&L, XIRR or tax.
2. **Verdicts are never scored against outcomes.** The back-test covers IPO listing only, and has n ≈ 0–1 resolved cases.
3. **Signals are qualitative.** Confidence is `Literal["low","medium","high"]` in `agents/schemas.py`. Scenarios say "plausible / most likely", with no probability to calibrate.
4. **No deterministic forensic or factor layer.** There are no Beneish, Altman, Piotroski, accruals, momentum or valuation-percentile scores.
5. **Rules are IPO-only.** `suggest/profile.py`'s closed `Metric` literal has ten IPO metrics.
6. **F&O has no retail-loss warning and no IV history.**

**Important data finding (probed live, read-only).**
- The app's existing NSE adapters can already assemble a training set for an IPO listing-gain model:
  - `past_issues()` returns 1,466 rows, 462 of them mainboard EQ listings (2016–2026: 24/32/20/11/12/57/33/47/73/83/69 per year).
  - `/api/ipo-detail` still returns the final category book (`activeCat`: QIB/NII/retail shares offered, bid and times) for issues listed in 2016 (LAURUSLABS), 2019 (PRINCEPIPE) and 2023 (INNOVACAP).
  - `NseEquity.history()` returns the listing-day OHLC and VWAP. Example: INNOVACAP was issued at ₹448 and opened at ₹452.1, closing at ₹541.4 on 29-Dec-2023.
- The model therefore needs **no new data source**. It needs a harvesting job, not a scraper.

**Plan-window lens.**
- The dashboard showed the 7-day Max window at **83 %** (5-hour at 4 %).
- Every new *agent* stream competes with the next IPO report.
- Deterministic Python features use no plan window at all. The roadmap (§E) puts them first for that reason.

---

## A. What the app lacks (feature gap analysis)

### A.1 Inventory of what exists (for reference)

| Area | Present today |
|---|---|
| **Research** | IPO, stock, fund and bond reports (agent streams, verifiers, bull/bear, synthesis, critic, publish gate); "Ask about this report"; research pack (MD/HTML/PDF/XLSX/CSV) |
| **Market data** | NSE/BSE IPO lists, subscription, issue terms and lots; NSE quotes, history, results (XBRL, integrated filing), shareholding XBRL, corporate actions, announcements; AMFI NAVs; NSE CM bonds; NSE option chains and lot sizes; SEBI filings; NSE holidays |
| **Personal** | Profile (capital per IPO, category, tax slab, risk, horizon, manual holdings list); 3 default IPO rules; journal (IPO decisions); watches (IPO and stock) with scheduled checks and alerts |
| **Calculators** | SIP/XIRR for funds, bond YTM/after-tax/duration, F&O greeks/payoff/PoP (risk-neutral lognormal) |
| **App** | Dashboard, command palette, help and glossary, dark/light theme, PDF viewer, 390 px layouts |

### A.2 Gaps, prioritised for this user

**Priority key.**
- **P1:** build next; high value and zero or low plan-window cost.
- **P2:** valuable, after P1.
- **P3:** nice to have.
- The "Plan cost" column shows whether the feature consumes the Claude window.

| # | Gap | Why it matters for *this* user | Priority | Plan cost | Effort |
|---|---|---|---|---|---|
| A1 | **Portfolio layer: import, lots, P&L, XIRR, allocation** | Research without holdings cannot answer "should *I* buy more / sell?". Import CAMS/KFintech and NSDL/CDSL CAS PDFs with the MIT-licensed `casparser` [P16], plus broker tradebook CSVs for equity and F&O lots. Account Aggregator is **not** open to unregulated users [P17]. Today `Profile.holdings` is a manual list with no cost basis. | P1 | none | M |
| A2 | **Tax layer** | Realised and unrealised STCG/LTCG per lot (FIFO); running headroom on the ₹1.25 lakh equity LTCG exemption; harvest suggestions before 31-Mar; debt-MF slab treatment; SGB exemption now only for original subscribers held to maturity; F&O as business income; STT costs in P&L. Rates and section renumbering are in §D.7. | P1 (after A1) | none | M |
| A3 | **Outcome tracking and calibration for every verdict** | `evals/backtest.py` covers IPO listing only, and Orient Cables has not yet listed, so n = 0. Without scoring, the app cannot say whether its calls help. Needed: stock calls vs Nifty 50/sector TRI at 3/6/12 m, fund HOLD/SWITCH vs category median, IPO APPLY/AVOID at listing and at 3/6 m. Include Brier score and Wilson intervals (§C.10). | P1 | none | S–M |
| A4 | **Deterministic forensic and quality scorecard** | Beneish M, Altman Z''-EM, Piotroski F, Sloan accruals, CFO/EBITDA, receivable-days trend, other income as % of PBT, pledge %, auditor change. Computed in `fincalc` from XBRL/RHP tables and saved as baseline claims, so agents cite them instead of improvising (§C.5). | P1 | none | M |
| A5 | **IPO base-rate model from NSE history** | The most-used feature (IPO apply/skip) has no quantitative prior. Data is available (see §0). Details in §D.1. | P1 | none | M |
| A6 | **Report staleness and "since the report" panel** | Run 9 set an entry zone of ₹889–1,000. The live price on the stock page is ₹1,014.30, *above* the zone, and neither page says so. Add: price vs entry zone, days since the report, new filings since the report, and a re-run suggestion. | P1 | none | S |
| A7 | **F&O risk disclosure and IV history** | SEBI's Aug-2026 study: 87.7 % of individual F&O traders lost money in FY26 (₹91,685 cr) [P30]. The page shows only "analysis only". IV rank/percentile needs a stored daily ATM-IV series; the adapter has chains only. | P1 (warning) / P2 (IV) | none | S / M |
| A8 | **Rules and alerts for every asset class** | The `Metric` literal is IPO-only. Add stock metrics (price vs entry zone, P/E percentile, 200-DMA, drawdown, pledge change), fund metrics (rolling alpha vs benchmark, TER change, category-rank drop) and bond metrics (spread vs G-sec, rating action). Delivery: macOS notification (`osascript`) or ntfy/Telegram. True web push needs a public HTTPS origin, and iOS requires a Home-Screen-installed PWA [P21]. | P2 | none | M |
| A9 | **Stock screener and peer table** | Screener.in-style query over locally stored XBRL fundamentals plus price data, with "screen alerts" when a stock enters a screen [P11]. Peer comparison with median and IQR, not three hand-picked peers. | P2 | none | L |
| A10 | **Fund data depth** | TER (daily file on AMFI), AUM, monthly portfolio holdings (AMFI/AMC within 10 days of month-end [P27]), benchmark TRI, overlap between the user's funds, rolling alpha and beta. SEBI's 2026 overlap limits (≤50 % Value/Contra, thematic) make overlap a regulated metric [P27]. | P2 | none | M |
| A11 | **Bond context** | G-sec par curve (FBIL), spread over the matching-tenor G-sec, CRISIL cumulative default rates by rating [P29], last-trade date and volume (liquidity), call/put features. The FD comparison default is a manual 7.0 % even though run 12 cited SBI at 6.40 %. | P2 | none | M |
| A12 | **Comparison views** | Fund vs fund (rolling returns, drawdown, TER, overlap); bond vs bond vs FD vs G-sec, post-tax; IPO vs listed peers. | P2 | none | M |
| A13 | **Governance and red-flag feed** | Pledge % from the shareholding XBRL (already parsed), ASM/GSM surveillance lists, auditor resignation or qualification keywords in announcements, SEBI enforcement orders, credit-rating actions. "Phrase alerts" on filings, as Screener does [P11]. | P2 | none | M |
| A14 | **Backup and export** | The personal ledger, journal and future portfolio live in local Postgres; I saw no scheduled `pg_dump`. Add nightly dump and rotation, plus CSV export of portfolio, tax (Schedule-112A-style) and journal. | P1 (backup) / P2 | none | S |
| A15 | **Goal planning and allocation drift** | Target allocation with a ±5 pp drift band and annual review (Vanguard) [P22]; step-up SIP; Monte Carlo goal fan chart. | P3 | none | M |
| A16 | **News sentiment scoring** | Evidence that retail-accessible sentiment adds edge is weak **[W]**, and it costs plan window. Keep news as cited qualitative evidence; don't build a sentiment score. | P3 | high | — |
| A17 | **PWA / mobile** | Layouts already work at 390 px. A PWA manifest is cheap. Push needs HTTPS and a relay (A8). | P3 | none | S |
| A18 | **Accessibility** | Several encodings rely on colour alone (§B). WCAG 1.4.1 requires a second cue [P23]; 1.4.11 requires 3:1 contrast for chart marks. | P2 | none | S |

**What *not* to build.**
- Order placement.
- A social or sharing layer. Publishing calls would bring in SEBI RA registration, see §D.8.
- Grey-market-premium scraping as a *signal*. GMP is unofficial and the evidence on it is thin (§D.1).
- LLM-generated "scores" with no deterministic basis.

---

## B. UI/UX review of the live pages

**Method.**
- Headless Chrome screenshots at 1440 px, dark theme, `welcome=0`, on 30-Sep-2026 around 10:50 IST.
- **First observation:** in a first pass loading four pages at once with a 12 s wait, *every* data card on `/` and `/ipos` was still a skeleton. With a 25 s wait everything rendered.
- Individual endpoints are fast by curl (`/api/ipos` 1.1 s; the others 2–30 ms), so the delay is client-side or comes from contention in the Next.js server. Confound: research run #13's verify step was running on the same machine at the time; re-measure on an idle machine. It should be measured in the browser (Performance panel) before optimising.
- A dashboard that is blank for more than 10 s fails its main job (Few: at-a-glance monitoring [P24]).

**Cross-cutting recommendations**

| # | Issue | Recommendation |
|---|---|---|
| B0.1 | Slow time-to-content | Render server-side from a cached snapshot (stale-while-revalidate), with a "data as of" stamp. Show partial cards as each fetch resolves rather than all-or-nothing. Add a web-vitals log. |
| B0.2 | Colour-only semantics | Some meanings are carried by colour alone: rolling-return histogram bars (red below / green above risk-free), OI bars (calls red, puts green), rule-change alerts drawn in red even when the rule *cleared*, green KPI tiles for a 6.02 % 1-year return that is *below* the 6.5 % risk-free rate. Keep red/green strictly for gain/loss and pass/fail as `web/DESIGN.md` says. Add signs, arrows or labels (WCAG 1.4.1). Offer a colour-blind-safe palette toggle (blue/orange) [P23]. |
| B0.3 | Numbers without a comparator | Returns, P/E and yields are shown bare. Every headline number should carry its benchmark, own-history percentile or peer median (Few [P24]). |
| B0.4 | Uncertainty is verbal | Verdicts, scenarios and fair values show words ("Medium", "most likely"). Show probability plus range, for example "P(beats Nifty over 3 y) ≈ 55 % (90 % CI 40–70 %), based on n = … calibrated calls". Draw ranges as intervals, quantile dot-plots or fan charts [P25]. Until calibration data exists, say "uncalibrated". |
| B0.5 | False precision | "₹4,06,260.47 Cr", "13.54x", "31.71 %" in headlines. Round headlines to 3 significant figures and keep full precision in the evidence panel. |
| B0.6 | Freshness | Pages mix report-time and live values. Put a consistent "as of" chip on every figure group, and flag when the report is older than the latest filing or price move (A6). |

**Per-page findings**

**`/` Dashboard**
- Good: the attention line ("5 IPOs close today, UPI cut-off in 6h 9m"), the 14-day calendar, and "Closing soonest" with lot costs.
- Issues:
  1. The Plan-window tile shows 5-hour "4 %" large and **7-day 83 %** small. The 7-day figure is the binding constraint, so show the tighter of the two, prominently.
  2. "Live subscription" shows Orient Cables as of 29-Sep 5:12 pm, a closed book, next to open issues. Label it "final", or show only open watched issues.
  3. The decision-journal donut is drawn for n = 1 ("Skip 100 %"). Below about 5 decisions, use a list.
  4. "Getting started 3 of 4" still takes prime space after 9 runs. Auto-collapse it once three steps are done.
  5. Add a **portfolio strip** (value, day P&L, XIRR, tax headroom) once A1 exists. It is the most-glanced number for a retail user.

**`/ipos`**
- Good: lot and cost arithmetic per category, lot source provenance ("NSE issue page · BSE agrees"), filters.
- Issues:
  1. Cards show no research signal: no verdict for researched issues, no QIB/NII split (only the total bar), no base-rate expectation (§D.1). Add a compact "QIB x / NII x / Retail x" row and, once A5 exists, "similar past issues: median listing gain x %, P(loss) y %".
  2. BSE-only SME cards show a CLI command (`finresearch ipo run …`) where mainboard cards show a Research button. That is inconsistent; either offer the button or explain why not.
  3. 23 cards is a long scroll. Default to the Table view on desktop, with sortable subscription columns.
  4. The subscription bar has no marker for elapsed bidding days. Day-1 7x means something different from day-3 7x.

**`/stocks/INFY`**
- Good: shareholding from XBRL with quarter-on-quarter pp changes, quarterly results with source links, 52-week position.
- Issues:
  1. The price chart has no benchmark overlay (Nifty 50 / Nifty IT) and no 50/200-DMA. A −29.6 % 1-year return means little without the sector's return.
  2. No valuation block: P/E and P/B vs own 5- and 10-year percentile, EV/EBIT, earnings yield vs 10-year G-sec.
  3. The quarterly results chart puts revenue and net profit on one axis, so profit bars are unreadable. Use small multiples or a margin line.
  4. No link to the latest research verdict (run 9 ACCUMULATE ₹889–1,000). Add a verdict chip that flags price vs zone.
  5. The announcements feed is unfiltered (newspaper-publication notices, ESOP allotments). Group by materiality: results, board meeting, M&A, auditor, rating, litigation.
  6. No red-flag row: pledge, ASM/GSM, forensic scores (A4, A13).

**`/funds/120505`**
- Good: rolling-return distribution with "made money 88 % / beat risk-free 67 %", drawdown chart, SIP calculator with XIRR and the lump-sum counterfactual.
- Issues:
  1. Trailing-returns bars have no benchmark or category median. "Compare with category" is a button, but the comparator should be the default.
  2. No TER, AUM, expense drag, holdings or overlap with the user's other funds.
  3. The risk-free input (6.5 %) is free text. Default it to the current 91-day T-bill with its source.
  4. The KPI tiles colour 6.02 % 1-year return green although it is below risk-free (B0.2).
  5. No SEBI riskometer level or category-rank history.

**`/bonds/INE027E07998`**
- Good: assumptions panel (coupon frequency verified in run 12), after-tax YTM, duration/convexity table with exact vs estimate repricing, FD comparison.
- Issues:
  1. The cash-flow chart draws ₹7.48 coupons next to a ₹1,000 principal bar, so coupons are invisible. Split the principal or use a table with a cumulative line.
  2. "Current yield 8.37 %" is a prominent tile although it is misleading for a premium bond. De-emphasise it and explain it.
  3. The FD default of 7.0 % should come from a sourced current rate, or at least be labelled "your assumption".
  4. Missing: G-sec benchmark and spread, rating history and default probability (CRISIL 3-year CDR by rating [P29]), liquidity (last trade date, volume, number of trades). An illiquid NCD's quoted price may not be executable.

**`/fno`**
- Good: live chain, OI-by-strike, presets, analysis-only framing.
- Issues:
  1. **No risk warning.** Add a persistent, non-dismissable banner quoting SEBI's latest study (87.7 % of individual traders lost in FY26; about 92 % of losses were in options; 0-DTE concentration) [P30].
  2. The option-chain column headers were not visible in the captured viewport, leaving a dense grid of unlabeled numbers. Make the header sticky and label OI / ΔOI / IV / LTP.
  3. The OI chart uses red for calls and green for puts, which reuses gain/loss colours for non-P&L data. Use neutral chart colours.
  4. No IV rank/percentile, expected move (±S·σ·√T), India VIX, cost model (STT 0.15 % on option sale premium from 1-Apr-2026, brokerage, stamp) [P26], or margin estimate.
  5. PoP should state *which* probability it is. Today it is risk-neutral with drift r (`fincalc.options.probability_of_profit`). Also show expected value, including costs, under both risk-neutral and a realised-volatility assumption (§D.5).

**`/runs`**
- Issues:
  1. "Success rate 100 %" reads as accuracy but is the finished-run rate. Run 3 has "no gate record", and earlier runs were blocked. Rename it "Completed" and add "Gate pass rate".
  2. Run #13 (Astral) shows "running · 15h 05m, 14/15 steps". The API shows verify steps finishing overnight, so it is really paused and resumed around plan limits. Add a distinct *waiting for plan window (resets in …)* state and an ETA.
  3. Show per-run plan-window cost (% of 5-hour and 7-day). This is the scarcest resource.

**`/runs/9/report#summary`**
- Good: verdict card with entry zone, "In plain English" with evidence chips, reasons for and against weighted and cited, evidence-strength meter, action checklist.
- Issues:
  1. "How solid is the evidence? 91.7 %" measures *citation verification*, not forecast reliability. Label it "Citations verified", and add the track record of past calls once A3 exists.
  2. The "Medium" confidence meter is uncalibrated (B0.4).
  3. No "since this report" strip (A6): price ₹1,014 is now above the ₹889–1,000 zone.
  4. The 16 gate warnings are collapsed. Show the count by type (UNVERIFIED caveats, needs-review).

**`/runs/9/report#charts`**
- Good: entry-zone overlay on the live price, fair-value ranges drawn as ranges, provenance labels ("from the report's ledger" vs "live market data").
- Issues:
  1. Valuation vs peers shows three peers without a peer median or IQR, and "This company" is unnamed.
  2. The "Fair value vs price" rows mix methods and asterisks. Add a legend and a triangulation band (§C.7).
  3. Scenarios give ranges but no probabilities. Add explicit probability weights and a probability-weighted value.
  4. Add a reverse-DCF row: "the price implies x % FCF growth for 10 years" (§C.7).

**`/monitor/1`**
- Good: lifecycle stepper, subscription-over-time chart, snapshot table, scheduled-checks audit trail.
- Issues:
  1. The subscription chart spaces checks evenly although they are unevenly spaced in time, and the x-axis labels overlap. Use a true time axis.
  2. "Rule changed … now clear" alerts are drawn in the red/danger style although they are good news.
  3. The Scheduled-checks table's Result column is clipped at 1440 px ("anchor (remainin…").
  4. Missing decision aids that `fincalc` can already compute:
     - Retail allotment odds (≈ 1/30.5 for one lot at 30.5x under the SEBI lottery; `allotment_probability_floor`).
     - The expected-value framing: P(allot) × E[listing gain] × lot cost ≈ small.
     - The IPO base-rate band (§D.1).

**`/profile`**
- Issues:
  1. The large gradient banner is decoration (low data-ink [P24]).
  2. Holdings are manual symbol/sector/value only (see A1).
  3. The profile has no goals, asset-allocation target, emergency-fund months, age/horizon per goal, F&O experience or loss limit. These are needed for suitability-style self-checks and for sizing (§D.6).
  4. Tax slab is a percentage; also capture regime (old/new) and whether F&O/business income exists.

**`/help`**
- Good: getting-started path, research pipeline explainer, page guide, 67-term glossary.
- Add:
  1. **"How to read signals and their limits"**: what the base rates, calibration and CIs mean, and why a verified citation ≠ a correct forecast.
  2. An **F&O risk page** citing SEBI studies.
  3. A **Regulatory status** page: personal tool, not SEBI-registered; do not share calls (§D.8).

---

## C. Making reports more accurate and trustworthy

**Principle.** The system's weak point is no longer *citation* accuracy: run 9 had 91.7 % of cited claims verified, and the gold-set recall is 91–100 %. It is two other things:
- (i) **internal consistency across claims** (does the balance sheet balance, do the periods line up?);
- (ii) **forecast validity** (are the verdicts any good?).

The evidence for the LLM side is clear:
- FinanceBench: GPT-4-Turbo with retrieval answered 81 % of questions wrongly or refused [1].
- ALCE: even the best models lacked full citation support about 50 % of the time [2].
- Self-correction without external feedback does not help, and can degrade answers [3].

So the checks below are deterministic Python in `verify/` and `fincalc/`, not more agent turns.

**Data availability key.**
- **Have:** already parsed by an adapter.
- **Partial:** document is ingested, but the table must be extracted with `ingest.layout_table`.
- **New:** needs a new adapter.

### C.1 Accounting-identity checks

| Check | Logic (tolerance ε = max(0.5 × reporting unit × lines summed, 0.1 %)) | Data | Benefit |
|---|---|---|---|
| **Balance sheet balances** | \|TA − (Equity + NCI + TL)\| ≤ ε for each period, consolidated and standalone | RHP restated BS (Partial); annual-report BS (Partial); XBRL results carry **P&L only**. `adapters/xbrl.py` maps revenue, expenses, PBT, tax, PAT and EPS; half-yearly BS XBRL would be New. | Catches extraction and scale errors before any ratio is computed |
| **Cash roll-forward** | Open cash + CFO + CFI + CFF + FX = close cash. Close(t) = open(t+1). Close cash ties to BS cash (net of overdraft per Ind AS 7). | Partial | Catches wrong-period and wrong-column reads, the error class seen in the 28-Sep "balance sheet read as P&L" bug |
| **P&L subtotals** | Σ line items = total expenses. Total income − expenses ± exceptional = PBT. PBT − tax = PAT. PAT = owners + NCI. | **Have** (XBRL) | Cheap. It would have caught the "standalone vs consolidated" and "attributable vs total" mix-ups the scorer fixed by pattern |
| **EPS identity** | EPS × weighted shares ≈ PAT to owners, within 1 %. After bonus or split, prior EPS must be restated. (Orient Cables had a 9:1 bonus, 6-Jan-2025.) | Have / Partial | Detects un-restated share counts, a common IPO valuation error |
| **Equity roll-forward** | ΔRetained earnings ≈ PAT − dividends ± OCI ± other | Partial | Flags unexplained equity movements |
| **Segment sum** | Σ segments − eliminations = consolidated revenue and EBIT | Partial | Flags cherry-picked segment claims |
| **Ratio recomputation** | Every ratio claim (margin, ROE, D/E, P/E) is recomputed from its operand claims. The LLM emits `{operands:[claim ids], op}`, never a value (program-of-thought [4]). | Have (fincalc) | Removes the arithmetic error class |

**Implementation.**
- Add a `verify/identities.py`. It runs after the streams and before synthesis.
- Each failed identity creates a `gate_warning` (or a *block* when high-importance claims are involved), naming the claim ids.
- The XBRL US DQC rule set (196 rules, e.g. DQC_0004 A = L + E, DQC_0118 calculation checks) is a template for the design, not a rule set to run directly: it is US-GAAP [5].

### C.2 Unit and scale detection

- Normalise every numeric claim to rupees with an explicit scale token.
- **Scale-shift test:** for a new claim value v, if v × 10ⁿ for n ∈ {±2, ±3, ±5, ±7} matches another claim of the same metric and period within 0.5 %, flag a probable lakh/crore/million/thousand confusion.
- **Ratio sanity bands:**
  - PAT margin within −200 % to +80 %;
  - P/E within 0 to 500;
  - receivable days within 0 to 730;
  - otherwise a soft flag.
- The gate already has `rupee_scale` and `candidate_forms`; extend it to *cross-claim* detection.
- Have. Size S. Evidence: this is the error class already seen (₹mn vs ₹cr, USD read as INR; FUNCTIONAL_TESTING 29-Sep).

### C.3 Restated vs audited vs later filings (IPO-specific)

- An RHP's restated financials (ICDR, ICAI Guidance Note 2019) should match:
  - the audited statements for the same period, **except for disclosed restatement adjustments**;
  - the DRHP for overlapping periods.
- After listing, they should match the first XBRL results.
- **Check:** for each (metric, period) seen in two documents, the difference must be zero or explained by a line in the "restatement adjustments" note. Otherwise raise a hard flag.
- Data: RHP, DRHP and annual reports are already ingested per company (Partial).
- Benefit: catches both extraction errors and genuine restatement red flags.
- Evidence: accounting practice; no efficacy study **[W]**.

### C.4 Multi-source reconciliation (market data)

The app already does this for lots (NSE page vs BSE) and bands (NSE list vs issue page). Generalise it into a `reconcile(metric, sources[])` helper with per-source priority and a logged diff. Candidates:

| Metric | Sources to compare |
|---|---|
| Subscription | NSE `activeCat` vs BSE book vs `ipo-active-category` |
| Shareholding | XBRL vs NSE summary |
| Fund NAV | AMFI NAVAll vs scheme history |
| Bond price | NSE CM last trade vs report |

The research memo found that NSE `bidDetails` and `ipo-active-category` disagreed for the same live issue (115.7x vs 182.8x QIB). Per the adapter's docstring, `bidDetails` is the NSE-only book and `activeCat` is combined NSE+BSE, so these are different scopes (and possibly different times). That shows why a timestamped reconciliation matters.

### C.5 Forensic and quality scores

These are deterministic, saved as baseline claims with `fincalc:` citations. None has been validated on Indian data in anything I could verify **[W]**. Present them as *screening flags*, never as buy/sell triggers.

- **Beneish M-score (8-variable)** [6]:
  M = −4.84 + 0.920·DSRI + 0.528·GMI + 0.404·AQI + 0.892·SGI + 0.115·DEPI − 0.172·SGAI + 4.679·TATA − 0.327·LVGI
  - Flag if M > −1.78. Some sources use −2.22 **[U]**.
  - Needs two years of receivables, sales, COGS, current assets, PP&E, securities, depreciation, SG&A, CFO, PAT, current liabilities and long-term debt.
  - Data: Partial (annual reports and RHP).
  - Not for banks and NBFCs.
- **Altman Z''-score (EM)** [7]:
  Z'' = 6.56·X1 + 3.26·X2 + 6.72·X3 + 1.05·X4
  - X1 = WC/TA; X2 = RE/TA; X3 = EBIT/TA; X4 = book equity / TL.
  - Zones: safe > 2.6; grey 1.1–2.6; distress < 1.1.
  - The EM score adds a constant of +3.25 and maps to rating equivalents.
  - Not for financials.
- **Piotroski F-score (0–9)** [8]:
  - ROA > 0; CFO > 0; ΔROA > 0; CFO/TA > ROA; Δleverage < 0; Δcurrent ratio > 0; no equity issued; Δgross margin > 0; Δasset turnover > 0.
  - Originally tested within high book-to-market stocks.
- **Sloan accruals** [9]:
  Accruals/avg TA = (ΔCA − ΔCash − ΔCL + ΔSTD + ΔTP − Dep) / avg TA
  - Simpler cash-flow form: (PAT − CFO)/avg TA.
  - High accruals predict lower future returns in the US; India is unverified.
- **Cash conversion:** CFO/EBITDA over 3 years (`fincalc.ratios.cfo_to_ebitda` exists). Flag if below 0.6 with rising receivable days.
- **India-specific governance flags:**
  - promoter pledge % and its change (from the shareholding XBRL; Have via `shp_xbrl`);
  - auditor resignation or change (announcements);
  - related-party transactions as % of revenue (RHP / annual report, Partial);
  - contingent liabilities as % of net worth;
  - CARO qualifications (the Orient run found a CARO receivables gap).

### C.6 Outlier and anomaly detection, and the limits of Benford

- **Robust z (Iglewicz–Hoaglin)** [10]:
  Mᵢ = 0.6745 (xᵢ − median)/MAD; flag |M| > 3.5.
  - Apply to growth rates and ratios across **peers** (cross-section). RHPs give 3–5 periods, too few for time-series MAD.
- **Benford** [11]:
  P(d) = log₁₀(1 + 1/d).
  - Nigrini MAD cut-offs for first digits: 0.006 / 0.012 / 0.015 (close / acceptable / marginal) [12].
  - Needs data spanning several orders of magnitude and roughly ≥ 500–1,000 numbers **[U]**.
  - One company's statements have a few hundred numbers, which is too few. Use it at most as an "indicative" pooled test over all line items across years, and never in a verdict.
  - **Recommendation: low priority.**

### C.7 Valuation triangulation

- **EV bridge** (Damodaran-style; exact citation not verified **[U]**):
  Equity value = EV + cash + non-operating assets − debt (incl. leases) − NCI − ESOP value.
  - For an IPO: add fresh-issue proceeds to cash and use post-issue diluted shares (`fincalc.valuation.post_issue_shares`).
- **DCF with sensitivity:**
  - FCFF = EBIT(1−t) + D&A − capex − ΔNWC.
  - TV = FCFF₍n+1₎/(WACC − g) with the hard constraint WACC > g.
  - Report a two-way grid (WACC ±1 pp × g ±1 pp) and a tornado chart.
- **Monte Carlo** (Damodaran probabilistic valuation [13]):
  - Draw growth, margin and WACC from bounded (triangular/beta) distributions with a fixed seed.
  - Model correlations or vary only one of a correlated pair.
  - Don't double-count risk by also using a risk-adjusted discount rate.
  - Output P5/P50/P95 and P(value > price).
  - His pitfalls: "garbage in, garbage out"; the distribution choice is the hardest step.
- **Reverse DCF** (Mauboussin & Rappaport [14]):
  - Solve for the FCF growth g* (or the competitive-advantage period) at which DCF value = price.
  - Compare g* with the ledger's 5-year revenue/FCF CAGR and the peer distribution.
  - Best single "is the price demanding?" number, and needs only verified claims plus fincalc.
- **Relative multiples:**
  - Peers by industry code, revenue within 0.3–3× of the company, ≥ 3 peers.
  - Report the median and IQR, and the company's percentile.
  - Peer-selection rule is standard practice, unsourced **[W]**.
- **Triangulation output:** a fair-value *band* equal to the intersection or union of the methods, plus a note when methods disagree by more than 30 %. Replaces today's list of mixed point estimates.

### C.8 IPO-specific checks

- **WACA and pre-IPO pricing** (from the RHP "Basis for Offer Price", verified in the Orient RHP [15]):
  - cap price / WACA over 18 months and 3 years;
  - cap price / last primary or secondary transaction price;
  - recompute WACA = Σ(shares × price)/Σ shares from the transaction table and compare it with the disclosed value.
  - A large step-up within months is a flag.
  - "WACA = NA" (bonus or gift only) means there is no market anchor; record that explicitly.
- **OFS share** = OFS / issue size:
  - high OFS with no fresh capital means promoters or PE are cashing out;
  - disclosure metric only, no verified return evidence **[W]**.
- **Anchor book:**
  - anchor presence and IPO grading were **not** significant for underpricing in India (Mahalakshmi et al. 2021 [16]; Bubna & Prabhala: 5.9 % vs 2.3 %, not significantly different [17]);
  - don't score "marquee anchors" as a positive signal;
  - SEBI's 2026 study shows cumulative anchor exits of 72 % by day 365 for issues under ₹250 cr, and bigger price falls at the 30-day unlock when selling is heavy [18]. That is an *exit-timing* flag for the monitor's lock-in checks.
- **Grey market premium:**
  - evidence is thin and mixed. Brooks et al. find when-issued prices aid price discovery [19]; a small study finds GMP insignificant for listing price [20];
  - there is no auditable archive;
  - keep the current "unofficial" label, and use it at most as a logged low-weight feature.
- **Timing of observable subscription:** QIB bids arrive mostly on the last afternoon, near the 5 pm UPI cut-off. A model trained on *final* subscription overstates what is knowable at decision time (see §D.1).

### C.9 LLM-side verification: what is worth the plan window

| Technique | Evidence | Recommendation |
|---|---|---|
| Deterministic quote-at-lines (have) | Stronger than LLM attribution judges: AttributionBench ≈ 80 % macro-F1 [21] | Keep as the backbone |
| Self-consistency (k samples, majority) [22] | +4 to +18 pp on reasoning benchmarks | Use only for high-importance *extractions*, with k = 3 on a cheap model. Agreement rate becomes a confidence feature. |
| Chain-of-Verification [23] | Reduces hallucination when verification questions are answered *independently* of the draft | Done (#243, 2026-10-07): the second verifier (`verifier_blind`, STANDARD tier) gets the metric, period, a masked statement and the citation locations only, never the value, status, gate notes or the first verdict, and re-derives the figure; agreement is decided in code. Before #243 it was the same role and saw the first verdict. |
| Multi-agent debate [24] | Not reliably better than self-consistency, and hyper-parameter sensitive [25] | Don't add |
| LLM-as-judge [26] | Position, verbosity and self-preference biases | For the critic: use a checklist rubric, a different model class if possible, and both orders for pairwise judgments |
| Verbalised confidence | Overconfident (Xiong et al. [27]); P(True) calibrates better in-format (Kadavath [28]) | Never display the model's "high/medium" as a probability. Derive confidence from observable features (C.10). |
| Retrieval scoping | DocFinQA: long contexts (123k words) hurt [29] | Keep section-scoped retrieval (ICDR sections) as the default |

### C.10 Confidence calibration

- **Today:** `confidence: Literal["low","medium","high"]` in schemas. There is no probability, so nothing can be scored.
- **Step 1 (now, size S).** Every verdict must also emit a *fixed-mapped* probability for a *defined, checkable event* with a horizon. Examples:
  - IPO: P(listing open > issue price).
  - Stock: P(12-month total return > Nifty 50 TRI).
  - Fund: P(3-year return ≥ category median).
  - Bond: P(no default or downgrade below A in horizon).
  - Map low / medium / high to 0.55 / 0.65 / 0.75 until data says otherwise. Log it in a new `forecasts` table with the resolution date.
- **Step 2.** A monitor job resolves each forecast at its date, using the existing listing/price/NAV adapters.
- **Step 3: metrics.**
  - Brier = (1/N)Σ(pᵢ − oᵢ)². Always shown next to the base-rate reference ō(1−ō) and the skill score BSS = 1 − BS/BS_ref.
  - Hit rate with the **Wilson interval**: centre (p̂ + z²/2n)/(1 + z²/n), half-width z√(p̂(1−p̂)/n + z²/4n²)/(1 + z²/n). Example: 7/10 gives about 40–89 %.
  - Reliability diagram with about 5 equal-count bins [30].
- **Step 4: recalibration.**
  - n < 50–100: don't fit anything (threshold is my judgment **[W]**).
  - 100 ≤ n < 1,000: Platt scaling with smoothed targets y₊ = (N₊+1)/(N₊+2), y₋ = 1/(N₋+2).
  - n ≥ 1,000: isotonic regression (Niculescu-Mizil & Caruana 2005 [31]).
- **Honest note:** at one person's volume (tens of reports a year), stock and fund verdicts will take **years** to accumulate enough outcomes. IPO listing outcomes accumulate faster, and the historical base-rate model (§D.1) gives calibration evidence immediately.
- **Abstention:** allow "NO CALL – insufficient evidence" and report risk–coverage (accuracy vs % of cases called). Selective prediction with a separate calibrator beats thresholding model confidence [32].

---

## D. Buy/sell decision signals by asset class

**Ground rules for every signal.**
1. Each signal is a *probability of a defined event over a horizon, with a range*, not a command.
2. Every signal has a pre-registered validation method and a displayed track record. Show "uncalibrated" until n is adequate.
3. Python computes the signal. The LLM explains it and adds qualitative evidence, but cannot override a deterministic rule. This matches the existing `suggest.advisor.enforce` design.
4. Present it as personal and non-advisory (§D.8).

### D.1 IPOs: apply or skip, then hold or sell at listing

**Evidence base (India)**
- **Institutional subscription dominates.**
  - Neupane et al. studied 329 book-built IPOs from 2004–13 [33]. Mean first-day market-adjusted return was 19 % (median 9 %).
  - Coefficient on log(1 + institutional subscription) ≈ 0.20 (t = 9.5), with adjusted R² ≈ 0.23–0.29.
  - Domestic-institution demand carries the effect.
  - Once subscription is in the model, the pre-listing market return and market volatility were *not* significant.
  - Larger issues and reputable underwriters had lower underpricing.
- **QIB demand leads the other categories.** QIB demand drives NII and retail demand (an information cascade). Anchors and grading were not significant [16]. Issues priced in the lowest price range are less likely to be fully subscribed [62].
- **Regime breaks.**
  - Allocation-discretion rules changed in Nov-2005 [34].
  - The NII allotment reform took effect in Apr-2022. NII oversubscription fell from about 38x to about 17x (SEBI study, secondary sources) [35].
  - Pooling across these breaks biases coefficients. Include post-2022 dummies, or train on post-2022 data only.
- **Selling behaviour.** Investors sold 54 % of allotted shares (by value, excluding anchors) within one week. When the listing return exceeded 20 %, 67.6 % was sold within a week [35]. **[U: SEBI primary PDF returned 404; figures from press coverage]**
- **Long-run returns.**
  - 197 mainboard IPOs from 2016–22: mean 780-day BHAR was +26.4 %, but the **median was −31.2 %**. Only 41 % beat the market, and there was no calendar-time alpha [36].
  - Underperformance clusters in hot IPO years [37].
  - Implication: *the listing is the trade*, unless a separate long-term thesis passes the stock-signal rules in D.2.

**Data: fully available from the app's adapters (probed 30-Sep-2026, GET only)**

| Field | Source | Status |
|---|---|---|
| Issue list, dates, issue price, listing date | `NseClient.past_issues()` — 462 EQ issues from 2016 to 2026; SME is a separate `securityType` | Have |
| Final QIB / NII (bHNI, sHNI) / retail / employee shares offered and bid (`activeCat` = combined NSE+BSE book; `bidDetails` is NSE-only, so always record the scope) | `/api/ipo-detail?symbol=…&series=EQ` → `activeCat` (confirmed for 2016, 2019, 2023 and 2026 listings; one agent saw a 2016 gap, so expect some missing rows) | Have (parser exists: `parse_ipo_detail`) |
| Issue size, fresh issue vs OFS, price band | `issueInfo` from the same payload, plus `baseline.parse_baseline` | Have for recent issues; `issueInfo` coverage for pre-2020 issues not probed |
| Listing-day open, close and VWAP | `NseEquity.history(symbol, listing_date, …)` (INNOVACAP: issue ₹448, open ₹452.1, close ₹541.4) or NSE/BSE bhavcopy [38] | Have |
| Market mood | Nifty 50 history; IPO count in the trailing 90 days (from `past_issues`) | Have / derivable |
| Intraday subscription path (what was knowable before the 5 pm UPI cut-off) | **Not available historically.** The monitor stores snapshots only for watched issues. | **Start archiving now** |

**Model (fit offline, deterministic, in `evals/ipo_model.py` or `fincalc/ipo_model.py`)**
- **Targets:**
  - y₁ = 1[listing open > issue price], estimated by logistic regression.
  - y₂ = listing-open return r = open/issue − 1, winsorised at 1 % and 99 %, estimated by quantile regression at τ = 0.1, 0.5 and 0.9.
- **Features** (x):

  | Feature | Notes |
  |---|---|
  | ln(1 + QIB×) | |
  | ln(1 + NII×) | |
  | ln(1 + Retail×) | |
  | ln(issue size ₹ cr) | |
  | OFS share | |
  | Issue-price position | Upper-band dummy (nearly always 1, so likely dropped) |
  | Nifty 20-day return | |
  | IPO count in the trailing 90 days | |
  | Post-Apr-2022 dummy | Regime break |
  | Listing-year effects | Only if the sample allows |

- **Logistic form:** P(y₁ = 1 | x) = 1 / (1 + e^{−(β₀ + βᵀx)}), with an L2 penalty.
- **Sample size:** roughly 400–460 events supports about 10–20 effective parameters at 10–20 events per parameter.
- **Validation:**
  - Walk-forward with an expanding window: train on years < t, test on year t, for t = 2019…2026.
  - Report AUC, Brier score vs the base-rate Brier, a reliability curve, and quantile pinball loss.
  - Compare against naive rules: "QIB > 10x → apply" and "total > 20x → apply".
  - **Pass bar for use:** positive Brier skill score in at least 5 of 7 test years. Otherwise show only the empirical base-rate table below.
- **Simpler first output (effort S): an empirical base-rate table.**
  - For issues with final QIB in bands [<1x, 1–10x, 10–50x, 50–100x, >100x], split pre- and post-2022, show:
    - n;
    - median listing return and IQR;
    - P(loss at open), with a Wilson CI [61].
  - This is honest and understandable, and needs no fitted model. Put it on each IPO card and in the report as a verified `fincalc:` claim.
- **Decision-time caveat.** Final QIB is unknown at the retail decision time. Retail can bid until 5 pm on the last day, and QIB books fill late. Until the archived intraday data has enough history, condition on the **last available snapshot**. Tell the user that the historical table uses *final* numbers and is optimistic about what can be known.

**Decision rule shown to the user (personal)**
- Expected value of applying for one lot:

  E[value] ≈ P(allot) × lot cost × E[r]

  - P(allot) ≈ min(1, 1/retail×) under the SEBI retail lottery (`fincalc.ipo.allotment_probability_floor`).
  - Example: at 30x, P(allot) ≈ 3.3 %. On a ₹14,960 lot with E[r] = 20 %, E[value] ≈ ₹99.
  - Opportunity cost of blocked UPI funds is about zero for the few days they are blocked.
  - This framing makes clear that retail IPO applications are small-EV lotteries when oversubscribed.
- **Apply** if:
  - P(open > issue) from the base-rate table or model is ≥ the user's threshold (a new rule metric, `p_listing_gain`); and
  - the existing rules (QIB floor, gate, lot) pass.
- **Listing-day sell/hold:**
  - Default **sell at or near the open** unless the stock report's D.2 rules would independently rate it a buy at the listing price.
  - Evidence: median long-run BHAR is negative [36].
  - For issues under ₹250 cr, add an alert before the 30-day anchor unlock [18].
- **GMP:** display only, labelled unofficial. Not a model feature until snapshots have been archived and validated [19][20].

### D.2 Listed stocks: buy, hold or sell

**Evidence (India first)**
- **IIMA four-factor data (Agarwalla, Jacob & Varma)** [39][40]. Free daily and monthly Market, SMB, HML and WML returns from Oct-1993 to Dec-2025.
  - 1994–2014 averages: WML (momentum) about 21.9 % a year, HML (value) about 15.3 % a year, SMB (size) about 0.
  - **Momentum and value are supported in India. Size is not.**
- **Time-series momentum and trend** [41][42]. Evidence is strong globally. Rules are "12-month excess return > 0" and "price > 10-month SMA". They reduce drawdowns but whipsaw in sideways markets. India-specific evidence for the 200-DMA was not found **[W]**.
- **Low volatility and quality.** NSE runs Nifty Alpha Low-Vol 30 and Quality 30 indices [63]. Their methodology gives a rule template, but index backtests are not independent evidence **[W]**.
- **Valuation vs own history** (P/E percentile). Forward-return studies for India were not found **[W]**. Show it as context, not a trigger.
- **Earnings revisions and PEAD.** India evidence was not found, and consensus-estimate data is not in the app's adapters. Skip.
- **Forensic scores** (C.5). These are screening flags. India validation was not found **[W]**.

**Signal design (deterministic, `fincalc/signals.py`)**
- **Composite rank (Stockopedia/Trendlyne-style, but transparent)**, computed across a universe (Nifty 500 from NSE):
  - Momentum: 12-1 month return / σ₁y.
  - Value: earnings yield and B/P.
  - Quality: ROE, CFO/PAT, low accruals.
  - Low-vol: σ₁y.
  - Each is a percentile. Composite = mean of the momentum, value and quality percentiles.
  - Needs universe-wide fundamentals: Nifty 500 XBRL results, New (bulk fetch; M–L). Start with momentum and low-vol, which need only prices.
- **Trend state:** price vs 200-DMA, and 12-month excess return vs the 91-day T-bill.
- **Actions (personal rules, not advice):**
  - **Buy/accumulate zone:** price inside the report's verified entry zone, trend not negative (or explicitly accepted by the user as a value entry), no forensic red flag, composite ≥ 60th percentile.
  - **Hold:** thesis intact; no report downgrade trigger fired (e.g. run 9's "Q2 FY27 results" trigger).
  - **Review/sell triggers:**
    - price > the upper end of the fair-value band;
    - trend turns negative *and* composite < 40th percentile;
    - forensic flag appears;
    - pledge increases by more than 5 pp;
    - thesis trigger events (results, guidance cut).
- **Position sizing:**
  - Volatility-scaled [43][44]: weight = (target portfolio risk contribution) / σ̂ᵢ, capped by the user's single-stock limit (e.g. 10 %).
  - Kelly f* = μ/σ² is dominated by estimation error. If shown at all, show ¼-Kelly as a *ceiling* [45].
- **Stops:** ATR stop = entry − k·ATR₁₄ with k = 2–3. Stops add value only when returns trend or regimes switch, not under a random walk [46]. Present them as risk control, not alpha.
- **Validation:**
  - Walk-forward backtest on the Nifty 500 universe using point-in-time data. Beware survivorship bias: NSE's current list excludes delisted names, while the IIMA data is survivorship-adjusted.
  - Monthly rebalance, transaction costs included (STT 0.1 % on delivery both sides, plus stamp duty and brokerage).
  - Benchmark: Nifty 500 TRI.
  - Report CAGR, volatility, max drawdown, turnover, and the t-stat of alpha with Newey–West errors.
  - For the user's own calls, record them in the forecasts table (C.10) and compare 12-month outcomes vs Nifty 50 TRI with Wilson CIs.
  - **Honest claim:** "rules consistent with factor evidence in India", never "beats the market".

### D.3 Mutual funds: invest, hold or switch

**Evidence**
- **SPIVA India** [47][48]. Most active funds underperform over long horizons:

  | Category | Underperformed over 5 y | Underperformed over 10 y |
  |---|---|---|
  | Large-cap | 90 % | 73 % |
  | Mid/small-cap | 67 % | 82 % |
  | ELSS | 68 % | 87 % |

  - These figures are year-end 2024 per secondary coverage **[U]**; the S&P PDFs were blocked.
  - Mid-year 2025: mid/small-cap was the exception, with only 34 % underperforming in H1 2025.
- **Persistence is weak** and mostly explained by expenses and momentum. Only persistent *under*performance of the worst funds survives (Carhart [49]). An India-specific persistence scorecard was not found **[W]**.
- **Cost.** A direct plan must have a lower TER than the regular plan (SEBI master circular). TER is published daily per scheme [P27]. Cost drag compounds: `fincalc.funds.expense_drag` exists.
- **Style drift.**
  - Holdings-based: compare monthly holdings with AMFI's half-yearly large/mid/small list and the SEBI category minimums (e.g. mid-cap ≥ 65 % in stocks ranked 101–250) [P27].
  - Returns-based: Sharpe (1992) constrained regression on style indices [50].

**Signal design**
- **Category choice before fund choice.** For large-cap exposure, the default recommendation is an index fund, given the SPIVA evidence. Active funds must justify their cost with *rolling* alpha.
- **Rolling alpha:** 36-month rolling regression of fund excess returns on benchmark TRI excess returns (plus IIMA factors):

  r_f − r_rf = α + β(r_b − r_rf) + ε

  - Flag if α < 0 in more than 60 % of windows.
- **Rules:**
  - **Invest:** category fits the goal and horizon; TER in the category's cheapest half; rolling 3-year return above category median in ≥ 60 % of windows; no style drift.
  - **Hold:** otherwise.
  - **Switch review:** rolling alpha negative for 18 or more months *and* the switch passes a tax-cost check. Selling triggers STCG at 20 % or LTCG at 12.5 % above ₹1.25 lakh, plus any exit load. Rule: switch only if expected TER/alpha gain × horizon > tax + load.
- **Validation:** backtest the rule on AMFI NAV history (survivorship bias: merged or closed schemes vanish from NAVAll). Compare against "buy the category index fund".
- **Honest claim:** the "cheap, consistent, no drift" filter reduces the chance of a bad fund; it does not predict the winners.

### D.4 Bonds and NCDs: buy or hold

**Evidence and data**
- **CRISIL average cumulative default rates, FY16–26** [P29] (a 5-year CDR is not published):

  | Rating | 1-year | 2-year | 3-year |
  |---|---|---|---|
  | AAA | 0.00 % | 0.00 % | 0.00 % |
  | AA | 0.02 % | 0.07 % | 0.14 % |
  | A | 0.07 % | 0.33 % | 0.58 % |
  | BBB | 0.43 % | 1.15 % | 1.97 % |
  | BB | 2.80 % | 5.98 % | 9.70 % |

  - One-year rating stability: AAA 99.03 %, AA 96.05 %.
  - ICRA and CARE studies were not fetched.
- **G-sec par curve:** FBIL [65]. **New adapter.**
- **Corporate spreads:** no verified source for current values **[W]**.

**Signal design**
- **Credit-adjusted, post-tax yield:**

  y_net = after_tax_ytm − (PD_annual × LGD)

  - `fincalc.bonds.after_tax_ytm` already exists.
  - Annual PD ≈ 1 − (1 − CDR₃)^{1/3}.
  - LGD assumption: 60 % for senior unsecured, 40 % for secured. These are **[U]** assumptions; label them.
  - Example: BBB, 3-year CDR 1.97 % → about 0.66 %/yr × 0.6 ≈ **0.40 pp/yr** deducted.
- **Compare three benchmarks, all post-tax:**
  - the matching-tenor G-sec (FBIL);
  - an FD rate from a sourced bank table;
  - a target-maturity or gilt fund. Specified debt MFs are taxed at slab rate.
- **Buy** if y_net − y_net(G-sec) ≥ the user's required credit spread (e.g. 1.5 pp for AA, 3 pp for A) *and* liquidity is adequate (traded on ≥ N of the last 20 sessions).
- **Hold** otherwise. **Sell review** on:
  - a rating downgrade or a negative outlook (the monitor could poll rating-agency announcements);
  - price rising so that YTM falls below the G-sec + spread floor.
- **Duration:** ΔP/P ≈ −D_mod·Δy + ½·C·Δy² (implemented). Don't predict rates. Match duration to the holding horizon.
- Online bond platforms (OBPPs) are SEBI-regulated channels for buying listed NCDs and G-secs [64].
- **Tax notes (current law as researched):**
  - Coupons are taxed at slab rate.
  - Listed bonds held > 12 months: LTCG at 12.5 % without indexation.
  - Unlisted bonds and debentures: slab rate regardless of holding period (s.50AA from 23-Jul-2024) [55].
- **Validation:** mostly arithmetic, checked with golden tests. Default risk is a base rate, not a forecast.
- **Honest claim:** "expected-loss-adjusted yield under CRISIL's historical default rates", with the rates' date.

### D.5 F&O: enter or exit strategies (analysis only)

**Evidence and regulation**
- **SEBI study, 20-Aug-2026 (FY25–26)** [P30]:
  - **87.7 %** of individual traders lost money in FY26, a net loss of ₹91,685 cr.
  - About 92 % of losses came from options. 97 % of traders were mainly option buyers.
  - 59 % of index-option turnover was in contracts expiring the same day.
  - About 90 % of traders who lost in two consecutive years lost again in the next.
  - Only mainly-option sellers (about 2 % of traders) had a positive median return.
- **Earlier SEBI studies:**
  - Jan-2023: 89 % lost in FY22.
  - Sep-2024: 93 % lost over FY22–24, about ₹1.8 lakh cr.
  - Both **[U]**: PDFs not re-fetched.
- **SEBI, 1-Oct-2024 measures** [51]:
  - index contract value of ₹15–20 lakh;
  - one weekly-expiry index per exchange;
  - upfront premium collection;
  - no calendar-spread benefit on expiry day;
  - intraday monitoring of position limits.
- **STT from 1-Apr-2026 (Finance Bill 2026)** [56][60]:
  - option sale: 0.15 % of premium;
  - option exercise: 0.15 % of intrinsic value;
  - futures: 0.05 %.
  - **[U: enactment of the Finance Act 2026 not confirmed]**
- **Variance risk premium:** implied variance exceeds realised variance on average (Carr & Wu 2009 [52]). This fits SEBI's finding that sellers do better in median terms. It does not remove the tail risk of short options.

**Maths to add to `fincalc/options.py`**
- **Probability of profit.**
  - Current: risk-neutral, with drift r.
  - Add a real-world variant:

    P(S_T > K) = N(d₂*), where d₂* = [ln(S/K) + (μ − q − σ²/2)T] / (σ√T)

  - Use σ = IV (market-implied) *and* σ = realised volatility (20/60-day), and show both.
  - Label the result "model probability, not a forecast".
- **Expected value including costs:**

  EV = ∫ payoff(S_T) f(S_T) dS_T − |net premium| effects − costs

  - Costs cover STT, exchange charges, SEBI fee, stamp duty, GST and brokerage.
  - Under risk-neutral pricing, EV ≈ −costs by construction. Say so explicitly: *"at fair prices, the expected P&L of any strategy is minus its costs."*
- **Expected move:** ±S·σ_IV·√T, the 1-σ band. Draw it on the payoff chart.
- **IV rank and IV percentile.** Needs a stored daily ATM IV per underlying (new table and a daily monitor job; nothing historical is available).
  - IVR = (IV − IV_min,252) / (IV_max,252 − IV_min,252)
  - IVP = share of the last 252 days with IV < today's IV
- **Skew:** 25-delta put IV − 25-delta call IV, from the current chain.
- **Margin:** SPAN is not available offline. Link out, or approximate and label the approximation.
- **Risk of ruin:**
  - With fixed-fraction bets, edge e and even-money payoffs: RoR ≈ ((1 − e)/(1 + e))^{C/u}, where C/u is capital in units of the bet.
  - More useful: a Monte Carlo of the user's strategy P&L distribution under realised-vol paths. Show P(drawdown > X % of capital within N expiries).

**Presentation and guardrails**
- A permanent banner with SEBI's current statistics.
- A pre-trade checklist:
  - max loss as % of capital, against a profile limit (e.g. ≤ 2 %);
  - defined-risk strategies only by default;
  - a warning on naked short options (unlimited loss) and same-day expiry.
- F&O experience and a loss budget go in the profile.
- **Honest claim:** the app computes payoffs and model probabilities; it has no edge-generating F&O signal. Evidence says most retail participants lose, mainly through costs and option buying. Treat any "signal" (IV rank high → sell premium) as an *uncertain* VRP harvest with tail risk. **No India-specific VRP study was verified [W].**

### D.6 Cross-asset: sizing and uncertainty display
- **Sizing hierarchy:**
  1. Goal and asset allocation (profile).
  2. Per-position cap.
  3. Volatility scaling within sleeves.
  4. ¼-Kelly as a ceiling only.
- **Display:** probability with a range. Illustrative format only (numbers invented, not computed): "P(listing gain) 72 % (base rate, n = 83 post-2022 issues with QIB > 50x; 95 % CI 62–81 %)".
- **Visuals:** quantile dot-plots or fan charts for ranges [P25]. Never a single-point target without its band.

### D.7 Tax facts used by signals (as researched 30-Sep-2026)
- **Listed equity and equity MFs:**
  - STCG 20 %.
  - LTCG 12.5 % above ₹1.25 lakh per year.
  - Holding period 12 months.
  - Effective 23-Jul-2024 [55].
- **Other assets:** 24 months for long-term. No indexation.
- **Income-tax Act 2025 (tax year 2026-27):**
  - Same rates.
  - The capital-gains rate sections appear renumbered (≈ ss.196–198 replacing 111A/112/112A) [56][57]. The mapping is **[U]** (inferred).
  - **Code should key on the rule, not the section number.**
- **Specified MFs** (> 65 % debt, redefined): slab rate [55]. The effective-date wording is **[U]**.
- **SGB:** redemption exemption only for original subscribers who hold to maturity, from tax year 2026-27. Secondary buyers pay LTCG [58][59].
- **F&O:** non-speculative business income (ITR-3; loss carry-forward 8 years) **[U]**.
- **Grandfathering:** FMV on 31-Jan-2018 for pre-2018 equity **[U]** under the new Act.

### D.8 Regulatory constraints (SEBI) and required disclaimers
- **Scope of RA regulation.** The SEBI RA master circular (6-Feb-2026) [53] defines a research report to *exclude* "internal communications that are not given to current or prospective clients". Buy/sell/hold calls on specific securities are covered whatever the method, including technical analysis.
  - A purely personal tool is outside RA/IA registration.
  - **Publishing its calls, even for free (blog, X, Telegram), would require registration and would bring in the finfluencer rules** (Intermediaries amendment, 29-Aug-2024) and PaRRVA past-performance verification (operational from May-2026) [54].
  - This is my interpretation, not legal advice.
- **AI.** RA Reg 24(7): a registered RA using AI is fully responsible for the output, and must disclose that use (Reg 19(vii)) [53]. This doesn't bind a personal user, but it is a sound standard to adopt.
- **App requirements:**
  - Keep the current footer.
  - Add to every exported PDF/HTML: "Personal research generated with AI assistance; not investment advice; the author is not a SEBI-registered RA/IA; do not distribute."
  - Watermark exports with "PERSONAL – NOT FOR DISTRIBUTION".
  - Don't add sharing features.
  - Don't display "track record" claims publicly.

---

## E. Prioritised implementation roadmap

**Key.**
- Effort: S ≤ 1 day, M = 2–5 days, L > 1 week of focused work.
- ★ = build first.
- Every item follows the repo workflow: issue → branch → PR with regression tests → PRODUCT_REQUIREMENTS row.
- Items 1–9 use **no Claude plan window**.

### Phase 1: Trust and measurement (★ build first)

| # | Item | Scope | Files / modules | Data | Tests / validation | Effort | Risk |
|---|---|---|---|---|---|---|---|
| 1★ | **Forecast ledger and calibration** | Add a `forecasts` table (run_id, event definition, horizon, p, resolve_on, outcome). Synthesizer schemas gain `probability` and `event`, with a fixed low/med/high → p mapping until calibrated. A monitor job resolves forecasts. Show Brier, BSS and Wilson CIs in `/runs` and the report. Extend `evals/backtest.py` from IPO to all kinds. | `db/models.py` + migration, `agents/schemas.py`, `evals/calibration.py` (new), `evals/backtest.py`, `monitor/jobs.py`, `api/app.py` `/api/calibration`, `web/src/app/runs` | Existing adapters | Golden Brier/Wilson values; a fake-outcome resolution test; a replay test that old runs still load | M | Low. Small n for years (be explicit) |
| 2★ | **IPO history harvester and base-rate table** | One-off and nightly job. For each `past_issues` EQ/SME row: fetch `ipo-detail` (final categories, issue info) and listing-day OHLC. Store in an `ipo_history` table. Build a base-rate table by QIB band × regime, with Wilson CIs. Show it on IPO cards and in the report as a `fincalc:` baseline claim. | `adapters/nse.py` (reuse), `evals/ipo_history.py` (new), `fincalc/ipo.py` (`base_rates`), `verify/baseline.py`, `api/app.py`, `web/src/app/ipos` | NSE `past_issues`, `ipo-detail`, history | Recorded fixtures for 3 historical issues; a dedupe test; coverage report (rows missing `activeCat`) | M | NSE rate limits and bot manager: use the polite client, one request every few seconds, cached |
| 3★ | **Accounting-identity and scale checks** | `verify/identities.py`: P&L subtotals, EPS identity, BS balance, cash roll-forward, cross-document restated vs audited, scale-shift detector. Failures become gate warnings or blocks. | `verify/identities.py` (new), `verify/gate.py`, `fincalc/ratios.py` | Ledger claims, XBRL, RHP tables | Seeded-error tests (lakh/crore swap, standalone vs consolidated, un-restated EPS after a bonus); replay runs 5/9/11/12 must still pass or explain | M | False positives on rounding: tolerance design |
| 4★ | **Report freshness strip** | "Since this report": days elapsed, price vs entry zone and fair-value band, new filings since, downgrade triggers due. Suggest a re-run. | `api/insights.py`, `web/src/components/workspace/report/*`, stock/fund/bond pages | Live quote, announcements | API tests with frozen time | S | None |
| 5★ | **F&O risk banner, cost model, dual PoP and EV** | Banner with SEBI Aug-2026 statistics (configurable text with source and date). STT/charges model. Real-world PoP alongside risk-neutral, using realised vol from history. Expected-move band. Max-loss vs capital check. | `fincalc/options.py`, `fincalc/charges.py` (new), `api/app.py`, `web/src/app/fno` | Chains, price history | Golden tests (N(d₂) values; STT on a sample trade) | S–M | STT rates change by budget: keep a dated config table |
| 6★ | **Backups** | Nightly `pg_dump` with rotation, plus documents manifest | `scripts/backup.sh`, launchd plist docs | — | Restore test on `finresearch_test` | S | None |
| 7 | **UI fixes from §B** | Label "Success rate" → "Completed"/"Gate pass rate"; "Evidence" → "Citations verified"; paused state for runs; 7-day plan meter; colour-only encodings; sticky chain headers; bond chart scale; monitor time axis; load-time investigation | `web/src/...` | — | Screenshot checks (existing CDP script) at 1440 and 390, both themes; lint and typecheck | M | Low |

### Phase 2: Personal portfolio and tax

| # | Item | Scope | Files | Data | Tests | Effort | Risk |
|---|---|---|---|---|---|---|---|
| 8★ | **Portfolio import and lots** | Parse CAS PDF (password typed per import, never stored in the repo) with `casparser`; tradebook CSV import; FIFO lots; holdings valued from AMFI/NSE; XIRR per holding and overall (`fincalc.funds.xirr`); allocation | `portfolio/` package (new: `importers.py`, `lots.py`, `valuation.py`), models + migration, `/api/portfolio`, `web/src/app/portfolio` | CAS PDFs, CSV, AMFI, NSE | Synthetic CAS fixture (no real personal data in the repo); FIFO golden tests; reconciliation units = CAS closing units | M–L | Personal data: keep in `data/` (gitignored) and never send to the LLM unless the user asks |
| 9 | **Tax engine** | STCG/LTCG per lot with dated rules (rule table keyed by effective date, not section numbers); ₹1.25 L headroom meter; harvest suggestions; SGB rule; debt-MF slab; F&O turnover and P&L summary; CSV export | `fincalc/tax.py` (new), `portfolio/tax.py`, UI | Lots | Golden cases around 23-Jul-2024 and 1-Apr-2026 boundaries; grandfathering case | M | Tax law changes: dated rule table plus a "verify with a CA" note |
| 10 | **Rules for all asset classes and alert delivery** | Widen the `Metric` literal to a registry per kind (stock/fund/bond/F&O/portfolio); evaluate in the monitor; deliver via macOS notification and optional ntfy/Telegram | `suggest/profile.py`, `suggest/rules.py`, `monitor/jobs.py`, `monitor/notify.py` (new) | Existing | Rule fire/clear tests per kind; de-duplication | M | Alert fatigue: default to few rules |

### Phase 3: Deterministic analytics that feed the agents

| # | Item | Scope | Files | Data | Tests | Effort | Risk |
|---|---|---|---|---|---|---|---|
| 11 | **Forensic scorecard** | Beneish, Altman Z''-EM, Piotroski, accruals, CFO/EBITDA, pledge Δ, auditor-change keyword; saved as baseline claims; shown as "screening flags" | `fincalc/forensic.py` (new), `verify/stock_baseline.py`, prompts told to cite them | RHP and annual-report tables (Partial); XBRL; SHP XBRL | Textbook examples; not for banks/NBFCs (guard test) | M | Needs full BS/CF lines; extraction quality |
| 12 | **Valuation triangulation** | Reverse DCF (solve g*), DCF grid, seeded Monte Carlo, peer percentile (median/IQR), EV bridge incl. IPO proceeds | `fincalc/valuation.py` (extend), `fincalc/montecarlo.py` | Ledger claims | Golden DCF/reverse-DCF cases; seed reproducibility | M | Garbage in, garbage out: show inputs |
| 13 | **Stock signal layer** | Trend (200-DMA, 12-1 momentum), vol, valuation percentile vs own history, composite percentile over a universe; ATR; vol-scaled size suggestion | `fincalc/signals.py`, `adapters/nse_equity.py` (bulk history), universe job | NSE history (Nifty 500) | Walk-forward backtest harness `evals/stock_backtest.py` with costs; survivorship caveat in output | L | Universe data volume; overfitting (pre-register rules) |
| 14 | **IPO listing model** | Logistic and quantile regression on the item-2 dataset; walk-forward; ship only if Brier skill > 0 in ≥ 5/7 years | `evals/ipo_model.py`, serialized coefficients in `data/` | Item 2 | Walk-forward report committed as an artefact | M | Decision-time vs final subscription gap (#244: the subscription features are the final book, published after the 17:00 UPI cut-off; the harvest has no intraday history, so the model is labelled "uses post-cutoff data, not usable at the decision point", never makes the call and runs only as a shadow test; `evals/timing.py` checks every feature's availability); regime breaks |
| 15 | **Intraday subscription archive** | Snapshot every open issue's category book 3–4 times per bidding day (not just watched ones) | `monitor/scheduler.py`, `monitor/jobs.py` | NSE/BSE | Scheduler tests | S | Rate limits |
| 16 | **Fund depth** | TER daily file, monthly portfolio holdings, benchmark TRI, rolling alpha, overlap, style-drift check vs AMFI cap list | `adapters/amfi.py` (TER, portfolio), `fincalc/funds.py` | AMFI/AMC files, NSE indices TRI | Parser fixtures; regression golden values | M–L | AMC portfolio files are heterogeneous Excel |
| 17 | **Bond context** | FBIL G-sec curve adapter; spread; CRISIL CDR table (dated config); expected-loss-adjusted yield; liquidity stats | `adapters/fbil.py` (new), `fincalc/bonds.py` | FBIL, CRISIL study | Golden spread/EL calcs | M | FBIL format changes |
| 18 | **IV history** | Daily ATM IV per underlying; IVR/IVP; skew | `adapters/nse_fno.py`, new table, monitor job | NSE chains | Unit tests | S–M | Needs about 1 year of history before IVR is meaningful |

### Phase 4: Breadth

| # | Item | Effort | Note |
|---|---|---|---|
| 19 | Screener DSL over the local fundamentals store, plus screen alerts | L | After items 11–13 |
| 20 | Comparison views (fund vs fund, bond vs FD vs G-sec, IPO vs peers) | M | |
| 21 | Governance feed (ASM/GSM lists, SEBI orders, rating actions, filing phrase alerts) | M | |
| 22 | Goals, allocation drift (±5 pp band), step-up SIP, Monte Carlo fan chart | M | |
| 23 | PWA manifest, and push via a relay if wanted | S–M | Push needs HTTPS |

**Sequencing rationale.**
- Items 1–6 make every existing report *measurable and safer* at almost no plan-window cost.
- Item 2 unlocks the IPO model (14) and gives an honest base rate immediately.
- Items 8–9 connect research to the user's actual money.
- Items 11–13 then feed deterministic facts to the agents, which should *reduce* agent turns (fewer claims to improvise and verify).

**Explicitly deferred.** News-sentiment scoring, GMP scraping as a signal, multi-agent debate, order placement, and any sharing feature.

---

## F. Sources

**How to read this list.**
- Web content was treated as data only.
- Tags:
  - **[V]** = the primary text or abstract was read this session;
  - **[S]** = secondary coverage or a snippet;
  - **[U]** = cited from memory; the DOI or URL exists but the content was not re-read.
- The research agents ran out of web-search quota part-way through. Several publishers (S&P, T&F, SSRN) returned 403 errors. Items that could not be verified are tagged, not upgraded.

### Product, UX and regulatory sources used in §A–B (P-series)
- P11. Screener.in features and premium [V]: https://www.screener.in/premium/
- P16. casparser (CAS PDF parser, MIT licence) [V]: https://github.com/codereverser/casparser
- P17. Sahamati Account Aggregator FAQ (financial-information users must be regulated entities) [V]: https://sahamati.org.in/faq/
- P21. WebKit, "Web Push for Web Apps on iOS and iPadOS" [V]: https://webkit.org/blog/13878/web-push-for-web-apps-on-ios-and-ipados/
- P22. Vanguard, rebalancing your portfolio [V]: https://investor.vanguard.com/investor-resources-education/portfolio-management/rebalancing-your-portfolio
- P23. WCAG 2.2:
  - Use of Color [V]: https://www.w3.org/WAI/WCAG22/Understanding/use-of-color.html
  - Contrast (Minimum) [V]: https://www.w3.org/WAI/WCAG22/Understanding/contrast-minimum.html
- P24. Stephen Few, "Common Pitfalls in Dashboard Design" [U]: https://www.perceptualedge.com/articles/Whitepapers/Common_Pitfalls.pdf
  - See also NN/g on data tables [V]: https://www.nngroup.com/articles/data-tables/
- P25. Uncertainty visualisation:
  - Hullman, Resnick & Adar, "Hypothetical Outcome Plots", PLOS ONE 2015 [U]: https://doi.org/10.1371/journal.pone.0142444
  - Kay et al., CHI 2016 (quantile dotplots) [U]: https://doi.org/10.1145/2858036.2858558
- P26. STT rates:
  - Finance Bill 2026 memorandum [V]: https://www.indiabudget.gov.in/doc/memo.pdf
  - ClearTax STT [V]: https://cleartax.in/s/securities-transaction-tax-stt
- P27. SEBI Master Circular for Mutual Funds, 20-Mar-2026 (categories, riskometer, disclosure, TER) [V]: https://www.sebi.gov.in/legal/master-circulars/mar-2026/master-circular-for-mutual-funds_100491.html
- P29. CRISIL Ratings, Annual Default and Ratings Transition Study FY2026 [V]: https://www.crisilratings.com/content/dam/crisil/our-analysis/publications/default-study/crisil-ratings-annual-default-and-ratings-transition-study-fy-2026.pdf
- P30. SEBI press release 50/2026, equity derivatives study (20-Aug-2026) [V]: https://www.sebi.gov.in/media-and-notifications/press-releases/aug-2026/sebi-studies-indicate-key-trends-in-retail-participation-trading-behaviour-and-profitability-in-the-equity-derivatives_103838.html

### Numbered sources (§C–E)
1. Islam et al. 2023, FinanceBench [V]: https://arxiv.org/abs/2311.11944
2. Gao et al. 2023, ALCE (citation evaluation) [V]: https://arxiv.org/abs/2305.14627
3. Huang et al. 2023, "LLMs cannot self-correct reasoning yet" [V]: https://arxiv.org/abs/2310.01798
4. Chen et al. 2022, Program-of-Thoughts [V]: https://arxiv.org/abs/2211.12588
5. XBRL US Data Quality Committee rules [V]: https://xbrl.us/data-quality/rules-guidance/
6. Beneish 1999, FAJ [U]: https://doi.org/10.2469/faj.v55.n5.2296
   - Formula checked against [S]: https://en.wikipedia.org/wiki/Beneish_M-score
7. Altman 2005, Emerging Markets Review [U]: https://doi.org/10.1016/j.ememar.2005.09.007
   - Formula checked against [S]: https://en.wikipedia.org/wiki/Altman_Z-score
8. Piotroski 2000, Journal of Accounting Research [U]: https://doi.org/10.2307/2672906
9. Sloan 1996, "Do stock prices fully reflect information in accruals and cash flows about future earnings?", The Accounting Review 71(3):289–315 [U; no URL verified this session].
10. NIST/SEMATECH e-Handbook, modified z-score [V]: https://www.itl.nist.gov/div898/handbook/eda/section3/eda35h.htm
11. Benford's law overview [S]: https://en.wikipedia.org/wiki/Benford%27s_law
12. Nigrini MAD thresholds as coded in benford_py [S]: https://raw.githubusercontent.com/milcent/benford_py/master/benford/constants.py
13. Damodaran, "Probabilistic Approaches: Scenario Analysis, Decision Trees and Simulations" [V]: https://pages.stern.nyu.edu/~adamodar/pdfiles/papers/probabilistic.pdf
14. Mauboussin & Rappaport, Expectations Investing [V]: https://www.expectationsinvesting.com/ and https://www.expectationsinvesting.com/tutorials
15. Orient Cables RHP (Sep-2026), "Basis for Offer Price" / WACA disclosures [V, local extract]: `OrientCables_IPO_Research/_text_extracts/OrientCables_RHP_Sep2026.txt`, lines 9310–9400 and 7322–7330.
16. Mahalakshmi, Gupta, Kashiramka & Jain 2021, Global Business Review [V, abstract]: https://journals.sagepub.com/doi/10.1177/09721509211019707
17. Bubna & Prabhala, "Anchor Investors in IPOs" [V]: https://w4.stern.nyu.edu/finance/docs/WP/2014/AnchorIPOs_BubnaPrabhala.pdf
18. SEBI anchor lock-in study (Aug-2026), via Business Standard [S]: https://www.business-standard.com/markets/news/smaller-ipos-see-sharper-anchor-investor-exits-after-lock-in-periods-sebi-126081301818_1.html
19. Brooks, Mathew & Yang 2014, "When-issued trading in the Indian IPO market", J. Financial Markets [V, abstract]: https://ideas.repec.org/a/eee/finmar/v19y2014icp170-196.html
20. Oraon, grey market premium study, Indian Journal of Research in Capital Markets [V, abstract]: https://indianjournalofcapitalmarkets.com/index.php/ijrcm/article/view/175891
21. AttributionBench [V]: https://arxiv.org/abs/2402.15089
22. Wang et al. 2022, self-consistency [V]: https://arxiv.org/abs/2203.11171
23. Dhuliawala et al. 2023, Chain-of-Verification [V]: https://arxiv.org/abs/2309.11495
24. Du et al. 2023, multi-agent debate [V]: https://arxiv.org/abs/2305.14325
25. Smit et al. 2023, debate vs ensembling [V]: https://arxiv.org/abs/2311.17371
26. Zheng et al. 2023, LLM-as-a-judge [V]: https://arxiv.org/abs/2306.05685
27. Xiong et al. 2023, verbalised confidence [V]: https://arxiv.org/abs/2306.13063
28. Kadavath et al. 2022, "Language models (mostly) know what they know" [V]: https://arxiv.org/abs/2207.05221
29. Reddy et al. 2024, DocFinQA [V]: https://arxiv.org/abs/2401.06915
30. Guo et al. 2017, "On Calibration of Modern Neural Networks" [V]: https://arxiv.org/abs/1706.04599
31. Niculescu-Mizil & Caruana 2005, "Predicting good probabilities with supervised learning" [V]: https://www.cs.cornell.edu/~alexn/papers/calibration.icml05.crc.rev3.pdf
32. Kamath, Jia & Liang 2020, selective question answering under domain shift [V]: https://arxiv.org/abs/2006.09462
33. Neupane, Neupane, Paudyal & Thapa, "Domestic and Foreign Institutional Investors' Investment in IPOs" [V]: https://gala.gre.ac.uk/id/eprint/22595/1/22595%20NEUPANE_Domestic_and_Foreign_Institutional_Investors_Investment_in_IPOs_2016.pdf
34. Bubna & Prabhala 2011, "IPOs with and without allocation discretion", J. Financial Intermediation [S]: https://www.sciencedirect.com/science/article/abs/pii/S1042957310000549
35. SEBI, "Analysis of Investor Behaviour in IPOs" (Sep-2024), via press coverage [S]:
    - https://www.business-standard.com/markets/news/54-ipo-shares-allotted-to-investors-sold-within-a-week-shows-sebi-study-124090200856_1.html
    - https://www.moneylife.in/article/54-percentage-of-shares-allotted-to-investors-were-sold-within-a-week-from-listing-sebi-study-on-ipos/75059.html
36. Gupta, Patel, Saruparia & Atwe 2026, JRFM, long-run IPO performance [V, abstract]: https://www.mdpi.com/1911-8074/19/9/729
37. Shukla & Shaw 2023, Vikalpa [V, abstract]: https://journals.sagepub.com/doi/10.1177/02560909231157976
38. NSE bhavcopy archive (listing-day prices; alternative to the history API, cited in §D.1 data table) [V, HTTP check]: https://nsearchives.nseindia.com/content/cm/
39. IIM Ahmedabad Indian Fama-French-Momentum factor data library [V]: https://faculty.iima.ac.in/iffm/Indian-Fama-French-Momentum/
40. Agarwalla, Jacob & Varma, four-factor working paper [V]: https://faculty.iima.ac.in/iffm/Indian-Fama-French-Momentum/four-factors-India-90s-onwards-IIM-WP-Version.pdf
41. Moskowitz, Ooi & Pedersen 2012, time-series momentum, JFE [V, abstract]: https://doi.org/10.1016/j.jfineco.2011.11.003
42. Faber 2007, "A Quantitative Approach to Tactical Asset Allocation", J. Wealth Management [U]: https://doi.org/10.3905/jwm.2007.674809
43. Moreira & Muir 2017, volatility-managed portfolios, JF [V, abstract]: https://doi.org/10.1111/jofi.12513
44. Harvey et al. 2018, "The Impact of Volatility Targeting", JPM [U]: https://doi.org/10.3905/jpm.2018.45.1.014
45. MacLean, Thorp & Ziemba 2010, "Good and bad properties of the Kelly criterion" [U]: https://doi.org/10.1080/14697688.2010.506108
46. Kaminski & Lo 2014, "When do stop-loss rules stop losses?" [U]: https://doi.org/10.1016/j.finmar.2013.07.001
47. SPIVA India mid-year 2025, via ETF Trends [S]: https://www.etftrends.com/spiva-2025-mid-year-report-throws-spotlight-active-smidcap-funds/
    - Original PDF (blocked): https://www.spglobal.com/spdji/en/documents/spiva/spiva-india-scorecard-mid-year-2025.pdf
48. SPIVA India, via Cafemutual [S]: https://cafemutual.com/news/industry/35917-73-of-indian-large-cap-and-82-of-mid-and-small-cap-funds-have-underperformed-sp-india-benchmarks-over-a-10-year-period
49. Carhart 1997, "On Persistence in Mutual Fund Performance", JF [U]: https://doi.org/10.1111/j.1540-6261.1997.tb03808.x
50. Sharpe 1992, "Asset Allocation: Management Style and Performance Measurement", JPM [U]: https://doi.org/10.3905/jpm.1992.409394
51. SEBI circular SEBI/HO/MRD/TPD/P/CIR/2024/132, 1-Oct-2024 [V]: https://www.sebi.gov.in/legal/circulars/oct-2024/measures-to-strengthen-equity-index-derivatives-framework-for-increased-investor-protection-and-market-stability_87208.html
52. Carr & Wu 2009, "Variance Risk Premiums", RFS [U]: https://doi.org/10.1093/rfs/hhn038
53. SEBI Master Circular for Research Analysts, 6-Feb-2026 [V]: https://www.sebi.gov.in/legal/master-circulars/feb-2026/master-circular-for-research-analysts_99571.html
54. SEBI circular on PaRRVA enrolment timeline (Aug-2026) [V]: https://www.sebi.gov.in/legal/circulars/aug-2026/extension-of-timeline-for-enrolment-with-parrva-as-specified-in-sebi-circular-no-ho-38-14-4-2026-mirsd-pod-i-10557-2026-dated-april-29-2026_103314.html
55. Union Budget 2024-25 memorandum (Finance (No.2) Bill 2024) [V]: https://www.indiabudget.gov.in/budget2024-25/doc/memo.pdf
56. Finance Bill 2026 memorandum [V]: https://www.indiabudget.gov.in/doc/memo.pdf
57. TaxGuru, capital gains under the Income-tax Act 2025 [S]: https://taxguru.in/income-tax/capital-gains-income-tax-act-2025-tax-period-2026-27.html
58. Upstox, SGB tax changes 2026 [S]: https://upstox.com/news/personal-finance/tax/sovereign-gold-bond-tax-changes-2026-sgb-tax-rate-old-vs-new-rules-applicable-date-facts-and-fa-qs/article-188770/
59. NISM, Budget 2026 and SGB taxation [S]: https://www.nism.ac.in/blog/how-budget-2026-changes-sovereign-gold-bond-sgb-taxation
60. ClearTax, STT rates [S]: https://cleartax.in/s/securities-transaction-tax-stt
61. Binomial proportion confidence intervals (Wilson; Brown, Cai & DasGupta 2001) [S]: https://en.wikipedia.org/wiki/Binomial_proportion_confidence_interval
62. Sandhu & Guhathakurta 2020, JRFM [V, abstract]: https://www.mdpi.com/1911-8074/13/11/279
63. NSE Indices, factor index methodologies (Momentum 30, Alpha Low-Vol 30, Quality 30) [U]: https://www.niftyindices.com
64. SEBI circular on online bond platform providers (14-Aug-2026) [V]: https://www.sebi.gov.in/legal/circulars/aug-2026/modification-in-the-regulatory-framework-for-online-bond-platform-providers-obpps-including-measures-for-promoting-ease-of-doing-business_103647.html
65. FBIL (G-sec par yield curve, T-bill benchmarks) [U]: https://www.fbil.org.in

### Repository and live-app evidence (read-only)
- **Code:**
  - `src/finresearch/adapters/nse.py` (`past_issues`, `ipo_detail`)
  - `adapters/nse_equity.py` (`history`)
  - `adapters/xbrl.py` (P&L-only tags)
  - `suggest/profile.py` (IPO-only `Metric`)
  - `agents/schemas.py` (verbal confidence)
  - `evals/backtest.py`
  - `fincalc/options.py` (risk-neutral PoP)
- **Live probes (GET only, 30-Sep-2026):**
  - `past_issues`: 1,466 rows, 462 EQ listings.
  - `ipo-detail` returned `activeCat` for LAURUSLABS (2016), PRINCEPIPE (2019), INNOVACAP (2023) and ELEVATE (2026).
  - INNOVACAP listing-day OHLC was returned.
- **Screenshots:** taken during the review, not kept in the repository.

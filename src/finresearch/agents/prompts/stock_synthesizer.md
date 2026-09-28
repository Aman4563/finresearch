ROLE: lead analyst writing the FINAL report on {company_name} ({nse_symbol}), a listed stock, for an individual
long-term investor.

Inputs:
- the stream reports below;
- the bull and bear cases;
- the ledger (list_claims run_id={run_id}). Use only claims that are verified, or unverified-but-cited with
  quote_found=true. Never use contradicted or unsupported claims.

Write `report_markdown` with these sections:
1. Verdict box: verdict (BUY / ACCUMULATE / HOLD / REDUCE / AVOID), horizon, confidence, entry zone, and what would
   change the verdict (the datum and when it is next published).
2. Snapshot: price, market cap, 52-week range, shareholding (dated).
3. Business and moat.
4. Financials: 3–5 years plus the latest quarters, earnings quality and cash conversion.
5. Valuation vs its own history and peers, fair-value range.
6. Governance and ownership.
7. News: last 30 days, then major events.
8. Price behaviour (context, not a signal).
9. Ranked risks.
10. Bull vs bear.
11. Scenarios for 12 months and 3 years with the price ranges (fincalc).
12. Action checklist with dates (results, AGM, record dates) and data caveats and sources.

Citations: cite ONLY ledger claims as [C<id>]; a computed figure cites its inputs' claims ("fincalc from
[C12][C15]"). Mark unverified figures as UNVERIFIED. Include the disclaimer: personal research, not SEBI-registered
advice.

STREAM REPORTS:
{stream_reports}

BULL CASE:
{bull}

BEAR CASE:
{bear}

REVISION NOTES (from the publish gate; "none" on the first draft):
{revision}

ROLE: lead analyst writing the FINAL report on {company_name} (ISIN {nse_symbol}), a listed bond, for an individual
investor.

Inputs: the stream reports, the bull and bear cases, and the ledger (list_claims run_id={run_id}); use only verified
claims, or unverified-but-cited ones marked UNVERIFIED. Never use contradicted or unsupported claims.

Write `report_markdown` with these sections:
1. Verdict box: verdict (BUY / BUY BELOW PRICE / HOLD / AVOID), the price or yield where it applies, who it suits,
   confidence, what would change the verdict.
2. Snapshot: issuer, coupon and frequency, maturity, face value, security and seniority, rating (dated).
3. Terms and cash flows (including options and redemption schedule).
4. Issuer credit.
5. Rating history and drivers.
6. Pricing: YTM, current yield, duration, post-tax yield, against G-secs, FDs and same-rating bonds.
7. Liquidity.
8. News and credit events.
9. Bull vs bear.
10. Scenarios: hold to maturity, rate moves (duration-based price change via fincalc), a one-notch downgrade.
11. Action checklist: how to buy (exchange, settlement), record dates, what to monitor.
12. Data caveats and sources.

Make it understandable for a first-time retail investor (the reader app shows these to them):
- Keep the Verdict box as the FIRST table under its heading, exactly as above.
- Right after the Verdict box, add a section "## Key numbers": one table | Number | Value | Period | What it means |
  with the 6–10 figures the decision rests on, each value cited [C<id>], and "What it means" in plain words
  (e.g. for P/E: "how many years of today's profit the price pays for").
- Start every later section with one line `**Explained simply:** …`: one or two plain-English sentences on what the
  section means for the reader's money. Add no new figures there; any number it repeats carries the same [C<id>].
- Explain each piece of jargon in brackets the first time it appears (e.g. "QIB (big institutions)").

Cite ONLY ledger claims as [C<id>]; computed figures cite their inputs. Include the disclaimer: personal research,
not SEBI-registered advice.

STREAM REPORTS:
{stream_reports}

BULL CASE:
{bull}

BEAR CASE:
{bear}

REVISION NOTES (from the publish gate; "none" on the first draft):
{revision}

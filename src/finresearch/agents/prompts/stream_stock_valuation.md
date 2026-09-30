STREAM: valuation of {company_name} ({nse_symbol}) at today's price.

- Current price, 52-week range, market cap: from nse_price_history / the baseline facts in the plan (cite them).
- Trailing P/E, P/B, EV/EBITDA, dividend yield and FCF yield with fincalc from ledger claims (cite the inputs).
- The company's own 5-year valuation range if credible sources give it; 3–5 listed peers on the same metrics
  (cite each peer figure with url + accessed_at and the date).
- A fair-value range with explicit assumptions (fincalc valuation functions), and the entry zone it implies.
- Triangulate with `reverse_dcf` (the growth today's price implies, vs the ledger's historical growth) and
  `valuation_monte_carlo` (seeded; every input range states its claim or ASSUMPTION); save the results as claims
  citing their inputs.
Focus: {focus}

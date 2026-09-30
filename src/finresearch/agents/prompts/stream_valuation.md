STREAM: offer structure, use of proceeds and valuation vs peers for {company_name}.

Sources: THE_OFFER, CAPITAL_STRUCTURE, OBJECTS_OF_THE_OFFER, BASIS_FOR_OFFER_PRICE, OFFER_STRUCTURE, the price-band ad and anchor documents; nse_ipo_detail for issue info.
0. Baseline offer facts: the pipeline has already recorded the NSE issue information (price band, lot, face value,
   fresh-issue amount, OFS amount or shares, anchor shares) as verified claims (stream "facts"; see list_claims). Do not
   duplicate them. Save EACH of the following as its own atomic claim, citing the RHP / price-band ad lines:
   total issue size (₹ million); fresh-issue shares and OFS shares at the upper band (fincalc shares_from_amount if the
   document states amounts); OFS amount; pre-issue and post-issue share count; post-issue market cap at both band
   ends; retail, NII and QIB quotas; anchor amount (₹ million); promoter holding pre and post issue (%).
1. Issue maths via fincalc: pre/post shares, fresh vs OFS, market cap at both band ends, reservation split and retail lots.
2. Objects of the issue (amounts and timing), and changes from the DRHP (size cuts and why).
3. Selling shareholders and their weighted-average cost vs the issue price; last private-round price; down- or up-round.
4. Valuation via fincalc:
   - P/E on reported, adjusted and TTM EPS, pre and post dilution;
   - P/B post-issue;
   - EV/EBITDA and P/S where relevant.
5. Peers: take the RHP peer set, then fetch CURRENT prices and metrics for relevant listed peers (WebSearch/WebFetch, cite URL + timestamp). Build a peer table and say where the RHP set flatters.
6. Fair-value range from 2–3 methods with the implied upside/downside.
7. Triangulate with `reverse_dcf` (the growth the cap price implies, vs the ledger's historical growth) and
   `valuation_monte_carlo` (a seeded fair-value distribution; every input range states its claim or ASSUMPTION).
   Save g*, P5/P50/P95 and P(value > price) as claims citing their inputs.
Focus: {focus}

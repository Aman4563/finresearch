"""Exchange disclosures for the stocks and bonds you hold or watch (FEATURE_RESEARCH items 1-3).

- `store`: the two tables (feed status, disclosure history), the tracked set (holdings, stock watches, bonds);
- `refresh`: fetch through `adapters.nse_disclosures` / `adapters.sebi_orders` and store (the monitor's daily pass
  and a stock page that has no recent read);
- `views`: what the API, the alert metrics and the morning brief show, from the database only. Every part carries
  its source URL and as-of date, and a feed with no recent good read is "unavailable", never "none".

Public market data only; the tracked set is read from the local portfolio tables to decide WHICH stocks to fetch and
is never sent anywhere (no LLM).
"""

---
name: ipo-deep-research
description: The FinResearch playbook for a deep, fact-checked Indian IPO report — what good looks like, the standard of evidence, and the mistakes past runs made. Use at the start of any IPO research task.
---

# IPO deep-research playbook

## What a good report found last time (the standard to beat)
- **Exceptional items decomposed** into their components, e.g. a ₹160 cr CEO incentive and a ₹46.65 cr cyber-fraud loss. Adjusted PAT was used for valuation.
- **Sellers' cost vs issue price per seller,** including a seller exiting below cost, and a down-round vs the last private round.
- **Who actually drove demand:** FII-led rather than MF-led QIB demand, read from the category breakdown.
- **Named concentration and governance flags:**
  - largest customer 38.5% of revenue;
  - receivables in bank returns ₹34.8 cr above the books (CARO);
  - a CRISIL B "issuer not cooperating" rating;
  - a trademark or name dispute and its hearing dates.
- **Live figures** timestamped and labelled INTERIM, with the correct bidding day.

## Mistakes past runs made (do not repeat)
- **Wrong day label:** a weekend counted as a bidding day. Use `dates.bidding_day_number`.
- **Stale or mixed data:** stale subscription and GMP figures, and NSE-only figures presented as combined.
- **Wrong totals:** promoter holding taken from a broker note instead of the price-band ad; ESOP-trust shares double-counted in a lock-in total.
- **Mismatched bases:** a pre-tax amount divided by post-tax profit; ROE on closing vs average equity not named.
- **Misread odds:** allotment odds presented as a ceiling instead of a floor.
- **Wrong input:** a page-break line shift that sent the model the wrong table. Always cite from the tool's line numbers.

## Evidence standard
- Every number is saved as a claim citing document lines (quote found) or a URL with timestamp.
- Ratios come from fincalc.
- What cannot be verified is labelled UNVERIFIED and listed in open_questions.

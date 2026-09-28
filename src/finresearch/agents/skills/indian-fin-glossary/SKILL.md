---
name: indian-fin-glossary
description: Indian IPO and finance conventions — units, share bases, subscription categories, lock-ins, lender metrics, SEBI terms. Use when interpreting any Indian filing or exchange figure.
---

# Indian IPO conventions

## Units and numbers
- 1 lakh = 100,000; 1 crore = 100 lakh = 10 million; 1,000 crore = 10 billion.
- Indian grouping: 1,17,64,705 = 11,764,705.
- In statements, (84.17) means negative and "-" means nil or not reported. Use fincalc `numbers.parse_number`.

## Offer mechanics
- **Fresh issue** is new shares, money to the company. **OFS** (offer for sale) is existing holders selling, money to them.
- **Shares from an amount** = amount / price, rounded down.
- **Categories:**
  - QIB is up to 50%; the anchor portion is up to 60% of QIB.
  - NII is at least 15%, split into sNII (₹2–10 lakh, one third) and bNII (over ₹10 lakh, two thirds).
  - Retail (RII) is at least 35%, and there may be an employee reservation with a discount.
  - Mainboard retail maximum is ₹2 lakh. Each successful retail applicant gets at most 1 lot, by lottery.
- **Subscription times:**
  - NSE computes them on the LOWER price-band share base, and many websites use the upper band.
  - `activeCat` is combined NSE+BSE; `bidDetails` is NSE only.
  - Always give the timestamp and the base.
- **Allotment odds:** 1/times is a FLOOR at that snapshot, because applicants may bid more lots than one. Odds fall as bids grow until the close.
- **Lock-ins (SEBI ICDR):**
  - anchors: 50% for 30 days and 50% for 90 days from allotment;
  - promoter minimum contribution: 18 months;
  - other pre-IPO shareholders: 6 months.
- **Timeline:**
  - UPI mandate cut-off is 5 PM on the closing day, allotment is T+1, listing is T+3 working days.
  - Bidding days exclude weekends and exchange holidays.
- **GMP / kostak / subject-to-sauda:** unofficial grey-market indicators. They are not evidence of value, and are easily manipulated for SME issues.

## Metrics
- **RoNW** in the RHP usually divides by closing net worth; ROE on average equity differs, so name the basis.
- **WACA:** weighted-average cost of acquisition of selling or primary shares. Compare it with the issue price.
- **Lenders:**
  - AUM, disbursals, NIM, cost of funds, credit cost as a % of average loans;
  - GS3/NS3 (over 90 DPD), Stage 2 (early stress), PCR, write-offs;
  - CRAR (NBFC minimum 15%);
  - DLG (default loss guarantee, capped at 5% of the covered portfolio).
- **CARO:** the auditor's statutory remarks. Mismatches in quarterly returns filed with banks are a control red flag.
- **Minimum public shareholding:** 25% within 3 years of listing, so a low float means more promoter selling later.

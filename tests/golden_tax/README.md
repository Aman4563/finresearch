# Golden tax corpus (#240)

Independent, hand-derived capital-gains cases for the tax and lot engine (`fincalc.tax`, `portfolio.lots`,
`portfolio.tax`). The expected values were worked out by hand from the Income-tax Act 1961 / Finance (No.2) Act 2024
by an author who did **not** read the engine; every case's `derivation` shows the arithmetic and `rules` cites the
section and Act. `test_golden_tax.py` maps each case's inputs onto the engine and compares field by field (only the
fields a case states are asserted).

Never edit an expectation to match the engine. When they disagree: fix the engine if it is wrong; otherwise record
the field under the case's `known_divergence` with the engine's value and the reason. The runner then requires
exactly that engine value, so any further change still fails.

## Scope of independence

The tax arithmetic (terms, rates, grandfathering, set-off, exemption, cess) was derived from the law alone. The input
model was not: the case schema and its input conventions were set by a brief written by someone who had read the
engine. These are the meaning of an `opening` row and of `broker_average`, that a bonus is dated on its allotment
day, that same-day buy/sell quantities net as speculative (intraday), and that sell charges are deducted from the
consideration. So a case can catch a wrong rule, rate, date or formula, but it cannot catch a wrong input
convention.

## Engine vs hand derivation (first run, 7-Oct-2026)

| case | field | hand | engine | outcome |
|---|---|---|---|---|
| g09 SGB RBI redemption | FY `exempt_total` | ₹90,000 | ₹0 | **engine bug, fixed**: `portfolio.tax.gains_of` passed the taxable gain (0) of an exempt transfer, so the year's exempt total was always ₹0; it now passes proceeds − cost (no tax changes) |
| g14 debt MF sold long-term before 23-Jul-2024 | gain, tax_cost, tax, cess, total | indexed (CII 301 → 348): gain ₹5,200, tax ₹1,081.60 | unindexed: gain ₹9,900, tax ₹2,059.20 | **known divergence**: indexation is not modelled (rule `other-unlisted-pre2024` is marked uncertain and the tax view says so); the engine errs towards more tax |

The two interpretive cases (g21 ELSS 29-Feb allotment, g26 order of the FY 2024-25 exemption between the 10 % and
12.5 % parts) agree with the engine.

## Bond cases g27-g32 (#294, 12-Oct-2026)

The implementer wrote the inputs and a conventions brief (`redemption`: the issuer or RBI redeems, a transfer under
s.2(47) unless a provision says otherwise; `interest`: income, not a capital-gains event; `debt_security`; tax class
`auto` = the app's classification from the ISIN and name; G-secs and T-bills treated as listed; the post-1-Apr-2026
SGB position as stated in fincalc.tax). A separate agent that did not open the engine derived the expected values
from the law; every value agreed with the engine on the first run, so there is no `known_divergence`. The runner maps
`redemption` and `interest` events, `tax_class: "auto"` (portfolio.tax.auto_tax_class) and `debt_security`, and takes
the SGB flags from portfolio.tax.redemption_flags. Its uncertainty notes: g27 a narrower reading of s.47(viic) would
cover only redemption on maturity; g28 under the old wording the redemption would be exempt (exempt_total 80,000);
g30 if a G-sec were not "listed" (most trading is on RBI's NDS-OM) the 24-month rule would make it short-term at slab
(total 53.04); g31 and g32 the discount or premium could be taxed as interest at the same slab rate.

## SGB cases g33-g37 (#316, 13-Oct-2026)

Derived by hand by the implementer from the decided rule (issue #316; ClearTax, a secondary source; the Budget 2026
memo returned HTTP 403) before the engine change, so not by an independent author: from 1-Apr-2026 RBI redemption is
exempt only for the original subscriber, at maturity (g33) or early after year 5 (g34); a lot bought by an exchange
trade is taxable even when the holding says subscriber (g35); with no record the term is unknown and the year
incomplete (g36, tax null as in g15); an exchange sale is taxable for anyone (g37). Inputs: a holding's
`sgb_original_subscriber` is three-way (absent = not known) and a lot's event may carry `meta.sgb_subscription` or a
broker `trade_id`/`order_id` (portfolio.tax.lot_subscription). g33, g34 and g36 failed before the change.

## Cases and conventions (from the derivation author)

These cases were derived by hand from the law, without looking at the code under test. Every transfer date is on or
before 31-Mar-2026, so all cases fall under the Income-tax Act 1961. From 1-Apr-2026 the Income-tax Act 2025 renumbers
the sections, and its changes could not be checked in this session. Charges are zero except in g02, and `fmv_2018` is null except in g07 and g08.

| File | Covers |
|---|---|
| g01_simple_fifo.json | one lot, partial LT sale after 23-Jul-2024, inside Rs 1.25L |
| g02_two_lots_mixed_term_with_charges.json | sale over two lots (LT 12.5 % + ST 20 %); buy charges to cost, sell charges apportioned by quantity |
| g03_intraday_plus_delivery.json | same-day buy/sell matched as intraday + FIFO remainder from an older lot |
| g04_split_before_sale.json | 10->2 split, then partial sale |
| g05_split_after_sale.json | sale in pre-split units, then split re-denominates the open lot |
| g06_bonus_nil_cost.json | 1:1 bonus, nil cost, dated at allotment; sale consumes original (LT) + bonus (ST) |
| g07_grandfather_fmv_higher.json | s.55(2)(ac): FMV > cost, sale > FMV; LTCG above Rs 1.25L |
| g08_grandfather_sale_below_fmv.json | cost capped at sale price (nil gain); FMV < actual cost -> LTCL carried |
| g09_sgb_rbi_redemption.json | SGB RBI redemption exempt (s.47(viic)), exempt_total |
| g10_sgb_exchange_sale.json | SGB exchange sale, LT 12.5 % (s.112), no threshold |
| g11_listed_ncd_st_and_lt.json | listed NCD: ST at slab, LT 12.5 % |
| g12_debt_mf_pre2023_lt_post_jul2024.json | debt MF bought pre-1-Apr-2023, 31 months, LT under the new 24-month rule, 12.5 % |
| g13_debt_mf_post2023_s50aa.json | s.50AA deemed STCG at slab |
| g14_debt_mf_indexation_pre_jul2024.json | old regime: > 36 months, 20 % with indexation (CII 301 -> 348) |
| g15_unknown_cost.json | opening with no price sold -> FY incomplete |
| g16_unknown_date_broker_average.json | broker-average opening sold -> gain known, term unknown, FY incomplete |
| g17_fy2025_rate_switch.json | FY 2024-25: 15 % / 10 % before 23-Jul and 20 % / 12.5 % after; LTCG under the threshold |
| g18_exemption_exhausted.json | LTCG 2.25L -> 1L taxed at 12.5 % |
| g19_stcl_setoff_ltcg.json | STCL against LTCG, then the threshold |
| g20_cross_fy_lots.json | buys in two FYs, sales in two FYs, two FY blocks |
| g21_elss_lockin.json | ELSS 3-year lock-in incl. 29-Feb (interpretive) |
| g22_equity_sale_without_stt.json | off-market sale: s.112 12.5 % (no threshold) + slab STCG |
| g23_fy2024_one_lakh_threshold.json | FY 2023-24: Rs 1L threshold, 10 % |
| g24_ltcl_cannot_offset_stcg.json | LTCL not set off against STCG, carried forward |
| g25_twelve_month_boundary.json | sale on the 12-month anniversary (ST) vs the next day (LT) |
| g26_fy2025_exemption_order.json | extra: FY 2024-25 threshold split between the 10 % and 12.5 % parts (interpretive) |
| g27_sgb_premature_rbi_redemption.json | #294: SGB redeemed early by RBI (after year 5, before 1-Apr-2026) exempt under s.47(viic); an interest row changes no lot |
| g28_sgb_secondary_buyer_redemption_after_apr2026.json | #294: SGB bought on the exchange, redeemed at maturity in tax year 2026-27: LTCG 12.5 % (interpretive: the 2026 rule is unverified) |
| g29_listed_ncd_redemption_premium_ltcl.json | #294: listed NCD bought at a premium, redeemed at face: LTCL set off against s.112A LTCG before the threshold |
| g30_gsec_exchange_sale_long_term.json | #294: dated G-sec (IN00 ISIN, tax class auto) sold after 12 months: 12.5 % under s.112, no s.112A threshold (interpretive: G-sec as listed) |
| g31_tbill_held_to_maturity.json | #294: 364-day T-bill (tax class auto) redeemed at face: STCG at slab (interpretive: head of income) |
| g32_unlisted_debenture_s50aa.json | #294: unlisted debenture redeemed after 23-Jul-2024: s.50AA deemed STCG at slab despite 36 months |
| g38_current_year_stcl_vs_stcg.json | #344: current-year equity STCL set off against STCG only (s.70(2)) |
| g39_bf_stcl_vs_stcg.json | #344: manual brought-forward STCL (filed on time) set off against STCG two years later (s.74(1)(a)) |
| g40_bf_stcl_vs_ltcg_above_threshold.json | #344: brought-forward STCL against equity LTCG, then the Rs 1.25L exemption |
| g41_bf_ltcl_only_vs_ltcg.json | #344: brought-forward LTCL absorbs the small LTCG only, never the STCG; balance carried (interpretive, see 16) |
| g42_engine_ltcl_chain.json | #344: LTCL from events in fy 2025 carried and absorbed by LTCG in fy 2026 (two FY blocks) |
| g43_eight_year_boundary_used.json | #344: 8-year limit: origin fy 2018 still set off in fy 2026 |
| g44_eight_year_boundary_lapsed.json | #344: 8-year limit: origin fy 2017 lapsed after fy 2025, not set off in fy 2026 |
| g45_late_filing_not_carried.json | #344: late filing (s.80): engine-computed STCL not carried, fy 2026 fully taxed |
| g46_unknown_filing_status.json | #344: unknown filing status: loss excluded from tax, carry_forward_complete false |
| g47_two_bf_stcl_oldest_first.json | #344: two brought-forward STCLs, partial absorption, oldest year first (interpretive, see 17) |
| g48_bf_stcl_and_ltcl_vs_ltcg.json | #344: brought-forward STCL and LTCL compete for one LTCG: LTCL first (interpretive, see 18) |
| g49_bf_stcl_vs_ltcg_below_threshold.json | #344: brought-forward STCL set off compulsorily against LTCG under Rs 1.25L (interpretive, see 16) |
| g50_current_stcl_low_slab_order.json | #344: slab 5 %: current-year STCL absorbs the 12.5 % LTCG before the 5 % slab STCG (interpretive, see 19) |
| g51_origin_year_incomplete_unknown_amount.json | #344: origin year incomplete: ledger unknown_amount, loss excluded in fy 2026 (interpretive, see 21) |

## Points where the law is uncertain, or where a convention was chosen

1. **The order in which the s.112A threshold is used in FY 2024-25 (g26, interpretive).** The amended s.112A(1) taxes
   LTCG "exceeding Rs 1,25,000" at 10 % before 23-Jul-2024 and 12.5 % from that date, but does not say which part
   the Rs 1.25L reduces first. The case takes the order better for the taxpayer: the 12.5 % part first, giving tax of
   7,500. The other order gives 9,375. g17 avoids the question by keeping total LTCG under Rs 1.25L.
2. **ELSS lock-in for an allotment on 29-Feb (g21, interpretive).** Counting from 1-Mar-2024 (General Clauses Act s.9), three
   years end on 28-Feb-2027, so units can be redeemed from 1-Mar-2027. Another reading treats 1-Mar-2027 as the
   "anniversary" and gives 2-Mar-2027. The other ELSS dates follow the brief's rule (anniversary + 1). The S.O. number
   of the ELSS 2005 notification is from memory and was not checked.
3. **The 12-month boundary (g25).** Excluding the day of acquisition, a sale on the anniversary is "not more than 12
   months", so it is short-term. This is the usual reading, but a few commentators count differently.
4. **Splits (g04, g05).** No specific clause in s.2(42A) or s.55 covers a share split. The case follows settled practice:
   no new asset, the cost is spread over the new shares, and the holding period runs from the original purchase.
5. **Intraday rows (g03).** The brief's data model treats the matched same-day quantity as speculative under s.43(5).
   These are conventions chosen here: the intraday row is listed before the FIFO row on the same date, `gain` is null
   (it is speculative business income, not a capital gain; the economic profit is 500) and `tax_cost` is omitted.
6. **SGB after 1-Apr-2026 (g09).** s.47(viic) as worded exempts any redemption "by an individual", whether or not the
   holder was an original subscriber, so `sgb_original_subscriber` makes no difference before 1-Apr-2026. I could not
   check whether the Income-tax Act 2025 or Finance Act 2026 limits this to original subscribers who hold to
   maturity, so the case is kept before 1-Apr-2026.
7. **CII notification numbers (g14).** The values 301 (FY 2020-21) and 348 (FY 2023-24) were confirmed. The notification
   numbers (No. 32/2020 for 301 and No. 21/2023 of 10-Apr-2023 for 348) are from memory and were not checked.
8. **`tax_cost` for indexation (g14).** `tax_cost` is set to the indexed cost (34,800) because that is the cost used for tax.
   The field definition only mentions grandfathering, so a harness that expects the actual cost there would differ.
9. **Broker-average opening (g16).** The gain of 15,000 is computed on the stated average cost. If the shares were in
   fact bought before 1-Feb-2018, s.55(2)(ac) could change the taxable gain, but `fmv_2018` is null, so no adjustment
   is made.
10. **Sell charges spread across lots (g02).** No rule in the Act says how one sale's brokerage is split across FIFO
    lots. The case splits it by quantity.
11. **Grandfathering uses gross or net consideration (g07, g08).** s.55(2)(ac) uses the "full value of consideration",
    which is the gross amount, not net of charges. Charges are kept at zero so this does not matter.
12. **LTCL created by the grandfathering formula (g08, INDIA LTD).** When FMV is below actual cost and the sale price
    is below both, the formula gives the actual cost, so the long-term loss of 30,000 is allowed and carried forward.
    This follows the text and the CBDT FAQ of 4-Feb-2018 on LTCG (as recalled).
13. **g15 and g16 FY blocks** leave out `losses_carried_*` and `exemption_used`, because these cannot be determined when the
    year is incomplete.
14. **g21 adds the holding key `"elss": true`, which is not in the brief's data model.** The brief has no field that marks a fund
    as ELSS, but `elss_unlock` is expected only for ELSS. The harness must map this flag, or detect ELSS some other way such
    as by name. Otherwise g21 will fail because of the schema, not because of the tax.
15. **g15/g16 still assert `exempt_total: "0.00"`.** This is independent of the unknown cost or date, because neither case
    has an exempt transfer.
16. **Brought-forward loss against LTCG the s.112A exemption would cover (g49, also g41 and g48, interpretive).** s.74(1)(a)
    and (b) say the carried loss "shall be set off" against the capital gains of the following year, with no election, and
    s.112A(1) taxes the LTCG included in total income, which is after that set-off. So the loss is used even when the gain
    sits inside Rs 1.25L. g49: bf_set_off_short 50,000, exemption_used 30,000, remaining 0. Under the optional reading:
    bf_set_off_short 0, exemption_used 80,000, remaining 50,000. Tax is 0 in both, so only the set-off and ledger fields
    differ. g41 under the optional reading: bf_set_off_long 0, exemption_used 30,000, remaining 80,000, tax unchanged 10,000.
17. **Which brought-forward year is used first (g47, interpretive).** s.74 does not say. The case uses the oldest first, because
    the older loss expires first and Schedule CFL lists years in that order. Newest first would leave fy 2020 with 30,000
    (last_fy 2028) and use fy 2023 in full. Tax is the same.
18. **Brought-forward STCL vs LTCL against the same LTCG (g48, interpretive).** The case applies the LTCL first, because it can
    absorb only LTCG, while a kept STCL can also absorb STCG taxed at 20 % or slab. Within one kind it uses the oldest
    first. Under oldest-first across kinds: bf_set_off_short 60,000 and bf_set_off_long 0; the fy 2022 STCL is used and the
    fy 2024 LTCL stays at 60,000. Tax is 0 in every reading.
19. **Which gain a current-year STCL absorbs (g50, interpretive).** s.70(2) allows "any other capital gain" and fixes no order.
    The case lets the assessee choose the most beneficial order: the gain taxed at the higher rate (LTCG at 12.5 % over
    the threshold) before slab STCG at 5 %. That gives a total of 11,440. STCL against STCG first gives 13,000 (tax
    12,500). The same principle would apply to a brought-forward STCL facing mixed gains; no case tests that.
20. **`losses_carried_short` in a late-filed origin year (g45).** The field is the year's own unabsorbed loss (30,000) whether
    or not s.80 lets it be carried. The `not_carried` status is shown only in the ledger.
21. **`amount` of an `unknown_amount` ledger entry (g51, interpretive).** The case uses the loss from the classified disposals
    (LTCL 40,000) as `amount` and `remaining`. The true amount is unknown, because the unknown-cost sale could absorb the
    loss or add to it. A harness that expects `null` there would differ. Either way the fy 2026 tax excludes the loss:
    total 10,400; applying it would give 5,200. The fy 2025 block leaves out `losses_carried_*` (as g15) but states
    `bf_set_off_*` 0.00 and `carry_forward_complete` true, because nothing was brought into that year.
22. **2025-Act numbers.** s.70 -> s.108, s.74 -> s.111 and s.80 -> s.121 are a secondary mapping given in the brief and were not
    checked against the Income-tax Act 2025. All transfers in g38-g51 are on or before 31-Mar-2026.

## Set-off and carry-forward cases g38-g51 (#344, 21-Oct-2026)

Derived by hand from the Income-tax Act 1961 by an author who did not read the engine or the runner: s.70 (same-year
set-off), s.74 (carry forward, LTCL only against LTCG, eight assessment years), s.80 with s.139(1)/(3) (a loss is carried
only if the return was filed on time), s.111A / s.112A as amended by the Finance (No.2) Act 2024, and 4 % cess. The s.74
text was checked only in paraphrase (HostBooks; incometaxindia.gov.in returned HTTP 403): the quoted words "shall be
set off" are from memory of the section, and the paraphrase agrees with them. New inputs:
`tax_losses.returns_filed_on_time` (per origin fy; a missing key means not known), `tax_losses.brought_forward` (manual
losses from Schedule CFL) and `through_fy`. New outputs: per-FY `bf_set_off_short`, `bf_set_off_long`,
`carry_forward_complete`, and `expected.ledger` (origin_fy, kind, amount, remaining, last_fy = origin_fy + 8, status).
Conventions used: brought-forward losses are set off after the year's own s.70 set-off and before the s.112A
exemption. A loss with late, unknown or unknown-amount status is left out of tax. `last_fy` is origin + 8: for example,
origin fy 2018 = AY 2018-19, and the eighth succeeding AY is AY 2026-27 = fy 2026 (g43); fy 2017 lapses after fy 2025
(g44). Interpretive cases g41, g47, g48, g49, g50 and g51 are explained in items 16-21 above, with the other reading's
figures.

First run against the engine (21-Oct-2026): 13 of the 14 cases agreed with the engine, interpretive ones included
(g50: the engine also sets a current-year STCL against the highest-taxed gain first; g48: brought-forward LTCL before
STCL; g47: oldest year first; g41/g49: compulsory set-off). **g51 is a known divergence of representation only**: the
engine records an incomplete year's unabsorbed loss as unknown (`amount` null) and adds a separate `unknown` entry for
the possible loss of the unknown-cost sale; the case shows the classified 40,000. The tax and
`carry_forward_complete` agree. The runner now compares disposals in (sale date, holding) order (a stable sort on both
sides), because these cases list sales by date while the engine lists them by holding; no expectation was changed.

# Golden tax corpus (#240)

Independent, hand-derived capital-gains cases for the tax and lot engine (`fincalc.tax`, `portfolio.lots`,
`portfolio.tax`). The expected values were worked out by hand from the Income-tax Act 1961 / Finance (No.2) Act 2024
by an author who did **not** read the engine; every case's `derivation` shows the arithmetic and `rules` cites the
section and Act. `test_golden_tax.py` maps each case's inputs onto the engine and compares field by field (only the
fields a case states are asserted).

Never edit an expectation to match the engine. When they disagree: fix the engine if it is wrong; otherwise record
the field under the case's `known_divergence` with the engine's value and the reason. The runner then requires
exactly that engine value, so any further change still fails.

## Engine vs hand derivation (first run, 7-Oct-2026)

| case | field | hand | engine | outcome |
|---|---|---|---|---|
| g09 SGB RBI redemption | FY `exempt_total` | ₹90,000 | ₹0 | **engine bug, fixed**: `portfolio.tax.gains_of` passed the taxable gain (0) of an exempt transfer, so the year's exempt total was always ₹0; it now passes proceeds − cost (no tax changes) |
| g14 debt MF sold long-term before 23-Jul-2024 | gain, tax_cost, tax, cess, total | indexed (CII 301 → 348): gain ₹5,200, tax ₹1,081.60 | unindexed: gain ₹9,900, tax ₹2,059.20 | **known divergence**: indexation is not modelled (rule `other-unlisted-pre2024` is marked uncertain and the tax view says so); the engine errs towards more tax |

The two interpretive cases (g21 ELSS 29-Feb allotment, g26 order of the FY 2024-25 exemption between the 10 % and
12.5 % parts) agree with the engine.

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

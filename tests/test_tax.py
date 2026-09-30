"""Golden tests for the capital-gains rule table (fincalc.tax) and the FIFO lot engine (portfolio.lots).

Every expected value is hand-computed in the comment next to it. Rates and dates: Finance (No.2) Act 2024 (transfers
on or after 23-Jul-2024), Income-tax Act 2025 (tax year 2026-27), s.50AA, s.55(2)(ac); see fincalc/tax.py.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal as D

import pytest

from finresearch.fincalc.tax import (
    Gain,
    add_months,
    classify,
    exemption_limit,
    fy_label,
    fy_tax,
    grandfathered_cost,
    is_long_term,
)
from finresearch.portfolio.lots import Event, build_lots


# --------------------------------------------------------------------------- holding period
def test_more_than_twelve_months_is_calendar_months():
    # bought 1-Aug-2023: sold 1-Aug-2024 is exactly 12 months (short); 2-Aug-2024 is long
    assert not is_long_term(date(2023, 8, 1), date(2024, 8, 1), 12)
    assert is_long_term(date(2023, 8, 1), date(2024, 8, 2), 12)
    # 31st rolls back to the month's last day; leap day
    assert add_months(date(2024, 1, 31), 1) == date(2024, 2, 29)
    assert add_months(date(2024, 2, 29), 12) == date(2025, 2, 28)
    assert fy_label(2025) == "FY 2024-25"


# --------------------------------------------------------------------------- the 23-Jul-2024 boundary
@pytest.mark.parametrize(
    ("sold", "term", "rate", "rule"),
    [
        (date(2024, 7, 22), "short", D("0.15"), "equity-2018"),  # last day of the old 15 %
        (date(2024, 7, 23), "short", D("0.20"), "equity-2024"),  # "on or after 23-Jul-2024" -> 20 %
    ],
)
def test_equity_stcg_boundary(sold, term, rate, rule):
    c = classify("equity", date(2024, 3, 1), sold)
    assert (c.term, c.rate, c.rule.id) == (term, rate, rule)


@pytest.mark.parametrize(
    ("sold", "rate", "rule"),
    [(date(2024, 7, 22), D("0.10"), "equity-2018"), (date(2024, 7, 23), D("0.125"), "equity-2024")],
)
def test_equity_ltcg_boundary(sold, rate, rule):
    c = classify("equity", date(2020, 1, 1), sold)
    assert (c.term, c.rate, c.rule.id) == ("long", rate, rule)


def test_tax_year_2026_27_same_rates_new_rule_row():
    st = classify("equity", date(2026, 1, 10), date(2026, 4, 1))
    lt = classify("equity", date(2024, 1, 10), date(2026, 4, 1))
    assert (st.rate, lt.rate, st.rule.id) == (D("0.20"), D("0.125"), "equity-2026")
    assert classify("equity", date(2024, 1, 10), date(2026, 3, 31)).rule.id == "equity-2024"


def test_other_assets_24_months_no_indexation_from_23_jul_2024():
    # an unlisted asset bought 1-Jan-2023: 24 months later is 1-Jan-2025; 2-Jan-2025 is long-term at 12.5 %
    assert classify("other", date(2023, 1, 1), date(2025, 1, 1), listed=False).term == "short"
    c = classify("other", date(2023, 1, 1), date(2025, 1, 2), listed=False)
    assert (c.term, c.rate, c.rule.id) == ("long", D("0.125"), "other-unlisted-2024")
    # before 23-Jul-2024 the same asset needed 36 months
    assert (
        classify("other", date(2021, 1, 1), date(2023, 12, 15), listed=False).term == "short"
    )  # 35.5 months
    assert (
        classify("other", date(2021, 1, 1), date(2024, 1, 5), listed=False).term == "long"
    )  # 36 months + 4 days
    # a listed security (e.g. an SGB sold on the exchange) is long-term after 12 months, slab rate before
    s = classify("sgb", date(2025, 1, 1), date(2025, 12, 1))
    assert (s.term, s.rate, s.bucket) == ("short", None, "slab_st")
    assert classify("sgb", date(2025, 1, 1), date(2026, 1, 2)).rate == D("0.125")


def test_debt_mf_bought_after_april_2023_is_always_slab():
    c = classify("debt_mf", date(2023, 4, 1), date(2026, 9, 1))  # 3.4 years, still short-term at slab
    assert (c.term, c.rate, c.bucket, c.rule.id) == ("short", None, "slab_st", "specified-mf-2023")
    # bought before 1-Apr-2023: the other-asset rules (24 months, 12.5 %) for a sale after 23-Jul-2024
    old = classify("debt_mf", date(2023, 3, 31), date(2025, 4, 1), listed=False)
    assert (old.term, old.rate) == ("long", D("0.125"))


def test_gold_fund_bought_after_april_2023_slab_only_until_march_2025():
    assert classify("other_mf", date(2023, 6, 1), date(2025, 3, 31), listed=False).bucket == "slab_st"
    after = classify("other_mf", date(2023, 6, 1), date(2025, 7, 1), listed=False)
    assert (after.term, after.rate) == ("long", D("0.125"))  # 25 months under the other-asset rules


def test_listed_units_needed_36_months_before_23_jul_2024():
    # a listed gold ETF bought 1-Jan-2022 and sold 1-Mar-2024 (26 months): short-term then (units: 36 months)
    assert classify("other_mf", date(2022, 1, 1), date(2024, 3, 1), listed=True).term == "short"
    # the same ETF sold on 1-Mar-2025: listed units now use 12 months
    assert classify("other_mf", date(2022, 1, 1), date(2025, 3, 1), listed=True).term == "long"


def test_equity_without_stt_falls_back_to_other_rules():
    c = classify("equity", date(2025, 1, 1), date(2025, 6, 1), stt_paid=False)
    assert (c.bucket, c.rate) == ("slab_st", None)


def test_sgb_redemption_original_vs_secondary_buyer():
    buy = date(2018, 1, 1)
    # up to 31-Mar-2026 RBI redemption is exempt for any individual, including a secondary buyer
    assert classify("sgb", buy, date(2026, 2, 1), rbi_redemption=True).term == "exempt"
    # from 1-Apr-2026 only an original subscriber who held to maturity [U: Finance Bill 2026]
    assert classify("sgb", buy, date(2026, 6, 1), rbi_redemption=True, original_subscriber=True,
                    held_to_maturity=True).term == "exempt"  # fmt: skip
    sec = classify("sgb", buy, date(2026, 6, 1), rbi_redemption=True)
    assert (sec.term, sec.rate) == ("long", D("0.125"))


def test_unknown_acquisition_date():
    assert classify("equity", None, date(2025, 1, 1)).term == "unknown"


# --------------------------------------------------------------------------- grandfathering
@pytest.mark.parametrize(
    ("sale", "cost"),
    [(D(200), D(150)), (D(120), D(120)), (D(90), D(100))],
)
def test_grandfathering_triple(sale, cost):
    # actual cost 100, FMV on 31-Jan-2018 150: sale 200 -> cost 150 (gain 50); 120 -> 120 (gain 0); 90 -> 100 (loss 10)
    assert grandfathered_cost(D(100), D(150), sale) == cost


# --------------------------------------------------------------------------- one financial year
def _g(amount, acquired, sold, cls="equity", **kw):
    return Gain(D(amount), classify(cls, acquired, sold, **kw), sold)


def test_fy_2024_25_one_exemption_across_both_rates():
    # LTCG ₹1,00,000 on 1-Jul-2024 (10 %) and ₹1,00,000 on 1-Sep-2024 (12.5 %): one ₹1.25 lakh limit for the year,
    # set against the 12.5 % part first -> taxable 1,00,000 at 10 % ... minus 25,000 exempt from it:
    # 12.5 % part fully exempt (1,00,000), 10 % part exempt 25,000 -> tax 75,000 × 10 % = 7,500; cess 300
    t = fy_tax([_g(100_000, date(2020, 1, 1), date(2024, 7, 1)), _g(100_000, date(2020, 1, 1), date(2024, 9, 1))],
               2025, "0.30")  # fmt: skip
    assert t.exemption_limit == D(125_000) and t.exemption_used == D(125_000)
    assert (t.tax, t.cess, t.total) == (D("7500.00"), D("300.00"), D("7800.00"))


def test_fy_boundary_exemption_limits():
    assert exemption_limit(2024) == D(100_000)  # FY 2023-24
    assert exemption_limit(2025) == D(125_000)  # FY 2024-25
    # a sale on 31-Mar-2025 is FY 2024-25; on 1-Apr-2025 FY 2025-26
    g1, g2 = _g(50_000, date(2020, 1, 1), date(2025, 3, 31)), _g(50_000, date(2020, 1, 1), date(2025, 4, 1))
    assert fy_tax([g1, g2], 2025).exemption_used == D(50_000)
    assert fy_tax([g1, g2], 2026).exemption_used == D(50_000)
    # FY 2023-24: 1,50,000 LTCG at 10 % above 1 lakh -> 5,000
    assert fy_tax([_g(150_000, date(2020, 1, 1), date(2024, 1, 10))], 2024).tax == D("5000.00")


def test_set_off_short_loss_hits_highest_rate_first_and_long_loss_only_long():
    sold = date(2025, 10, 1)  # FY 2025-26
    gains = [
        _g(200_000, date(2020, 1, 1), sold),  # equity LTCG 12.5 %
        _g(100_000, date(2025, 6, 1), sold),  # equity STCG 20 %
        _g(-60_000, date(2025, 5, 1), sold),  # equity STCL: against the 20 % STCG first
        _g(-50_000, date(2019, 1, 1), sold),  # equity LTCL: only against LTCG
    ]
    t = fy_tax(gains, 2026, "0.30")
    # STCG 1,00,000 - 60,000 = 40,000 × 20 % = 8,000; LTCG 2,00,000 - 50,000 = 1,50,000 - 1,25,000 exempt = 25,000 × 12.5 %
    # = 3,125; total 11,125 + 4 % cess 445
    assert (t.tax, t.cess) == (D("11125.00"), D("445.00"))
    assert t.losses_unabsorbed_long == 0 and t.losses_unabsorbed_short == 0


def test_losses_net_within_their_category_first_like_the_itr():
    # Schedule CG nets gains and losses of one kind (e.g. s.111A) before cross-category set-off: the 20 % STCL cancels
    # the 20 % STCG, leaving the 30 % slab gain: 50,000 × 30 % = 15,000
    sold = date(2025, 10, 1)
    gains = [_g(50_000, date(2024, 1, 1), sold, "debt_mf"), _g(50_000, date(2025, 6, 1), sold),
             _g(-50_000, date(2025, 5, 1), sold)]  # fmt: skip
    assert fy_tax(gains, 2026, "0.30").tax == D("15000.00")


def test_cross_category_short_loss_absorbs_the_highest_rate_first():
    # a slab-rate STCL of 50,000 (debt fund bought after 1-Apr-2023, no slab gains to net against) with equity STCG
    # 50,000 at 20 % and an other-asset LTCG 50,000 at 12.5 %: it absorbs the 20 % part; 50,000 × 12.5 % = 6,250
    sold = date(2025, 10, 1)
    gains = [_g(-50_000, date(2024, 1, 1), sold, "debt_mf"), _g(50_000, date(2025, 6, 1), sold),
             _g(50_000, date(2022, 1, 1), sold, "other", listed=False)]  # fmt: skip
    assert fy_tax(gains, 2026, "0.10").tax == D("6250.00")


def test_long_loss_left_over_is_reported():
    t = fy_tax([_g(-10_000, date(2019, 1, 1), date(2025, 10, 1)), _g(5_000, date(2025, 6, 1), date(2025, 10, 1))],
               2026)  # fmt: skip
    assert t.losses_unabsorbed_long == D(10_000) and t.tax == D("1000.00")


def test_loss_is_not_wasted_on_exempt_ltcg():
    # LTCG 1,00,000 (fully inside the exemption) and STCG 50,000 at 20 %; STCL 50,000 should absorb the STCG
    sold = date(2025, 10, 1)
    gains = [
        _g(100_000, date(2020, 1, 1), sold),
        _g(50_000, date(2025, 6, 1), sold),
        _g(-50_000, date(2025, 5, 1), sold),
    ]
    assert fy_tax(gains, 2026).tax == D("0.00")


# --------------------------------------------------------------------------- FIFO lots
def E(i, day, kind, qty=None, price=None, **kw):
    return Event(i, day, kind, None if qty is None else D(qty), None if price is None else D(price), **kw)


def test_fifo_golden():
    # buy 10 @100 (charges 10), buy 10 @120, sell 15 @150 (charges 15)
    book = build_lots([E(1, date(2024, 1, 1), "buy", 10, 100, charges=D(10)), E(2, date(2024, 2, 1), "buy", 10, 120),
                       E(3, date(2025, 3, 1), "sell", 15, 150, charges=D(15))])  # fmt: skip
    a, b = book.disposals
    assert (a.quantity, a.cost, a.proceeds) == (D(10), D(1010), D(1490))  # 2,250-15 = 2,235 net; 149/unit
    assert (b.quantity, b.cost, b.proceeds) == (D(5), D(600), D(745))
    assert a.acquired == date(2024, 1, 1) and b.acquired == date(2024, 2, 1)
    assert book.units == D(5) and book.open_cost == D(600)


def test_split_keeps_acquisition_date_and_cost():
    book = build_lots([E(1, date(2020, 1, 1), "buy", 10, 1000), E(2, date(2021, 6, 1), "split", meta={"from": 10, "to": 2}),
                       E(3, date(2022, 1, 1), "sell", 25, 300)])  # fmt: skip
    (lot,) = book.lots
    assert (lot.quantity, lot.open_quantity, lot.cost_per_unit, lot.acquired) == (
        D(50),
        D(25),
        D(200),
        date(2020, 1, 1),
    )
    assert book.disposals[0].cost == D(5000) and book.disposals[0].acquired == date(2020, 1, 1)


def test_bonus_is_a_nil_cost_lot_from_the_ex_date():
    book = build_lots([E(1, date(2020, 1, 1), "buy", 7, 100), E(2, date(2021, 1, 1), "bonus", meta={"a": 1, "b": 2}),
                       E(3, date(2021, 6, 1), "sell", 9, 80)])  # fmt: skip
    # 7 × 1/2 = 3.5 -> 3 bonus shares (fraction paid in cash by the company); FIFO: 7 original then 2 bonus
    assert [(x.origin, x.quantity, x.cost_per_unit) for x in book.lots] == [
        ("buy", D(7), D(100)),
        ("bonus", D(3), D(0)),
    ]
    assert [(d.origin, d.quantity, d.cost, d.acquired) for d in book.disposals] == [
        ("buy", D(7), D(700), date(2020, 1, 1)), ("bonus", D(2), D(0), date(2021, 1, 1))]  # fmt: skip


def test_intraday_same_day_is_netted_before_fifo():
    book = build_lots([E(1, date(2024, 1, 1), "buy", 10, 100), E(2, date(2025, 1, 2), "buy", 5, 200),
                       E(3, date(2025, 1, 2), "sell", 8, 210)])  # fmt: skip
    intra = [d for d in book.disposals if d.intraday]
    rest = [d for d in book.disposals if not d.intraday]
    assert [(d.quantity, d.cost) for d in intra] == [(D(5), D(1000))]
    assert [(d.quantity, d.acquired) for d in rest] == [(D(3), date(2024, 1, 1))]


def test_opening_balance_has_unknown_cost_until_entered():
    book = build_lots([E(1, date(2025, 4, 1), "opening", 50), E(2, date(2025, 6, 1), "sell", 10, 20)])
    assert book.disposals[0].cost is None and book.disposals[0].acquired is None and book.open_cost is None
    known = build_lots([E(1, date(2025, 4, 1), "opening", 50, 12, meta={"acquired": "2022-01-05"}),
                        E(2, date(2025, 6, 1), "sell", 10, 20)])  # fmt: skip
    assert known.disposals[0].cost == D(120) and known.disposals[0].acquired == date(2022, 1, 5)


def test_oversell_and_reversal():
    book = build_lots([E(1, date(2024, 1, 1), "buy", 5, 10), E(2, date(2024, 2, 1), "sell", 7, 11)])
    assert book.disposals[-1].lot is None and any("more units" in w for w in book.warnings)
    rev = build_lots([E(1, date(2024, 1, 1), "buy", 5, 10), E(2, date(2024, 2, 1), "buy", 3, 12),
                      E(3, date(2024, 2, 3), "remove", 3, meta={"reversal": True})])  # fmt: skip
    assert [x.open_quantity for x in rev.lots] == [D(5), D(0)] and not rev.disposals

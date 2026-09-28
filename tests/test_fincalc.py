from datetime import UTC, date, datetime
from decimal import Decimal as D

import pytest

from finresearch.fincalc import dates, growth, ipo, ratios, valuation
from finresearch.fincalc.numbers import (
    convert,
    format_indian,
    format_inr,
    format_pct,
    group_indian,
    parse_number,
    parse_percent,
    round_half_up,
    to_decimal,
    to_pct,
)


def r2(x):
    return round_half_up(x, 2)


# ---------------------------------------------------------------- numbers


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1,17,64,705", D("11764705")),
        ("11,716.54", D("11716.54")),
        ("(84.17)", D("-84.17")),
        ("( 2.32 )", D("-2.32")),
        ("-12.5", D("-12.5")),
        ("−3.1", D("-3.1")),
        ("₹ 272", D("272")),
        ("₹272.00", D("272.00")),
        ("Rs. 10", D("10")),
        ("0.94%", D("0.94")),
        ("2.32524175E8", D("232524175")),
        ("  535.61 ", D("535.61")),
    ],
)
def test_parse_number(text, expected):
    assert parse_number(text) == expected


@pytest.mark.parametrize("text", ["-", "—", "–", "Nil", "NIL", "NA", "", "  ", None])
def test_parse_number_nulls(text):
    assert parse_number(text) is None


@pytest.mark.parametrize("text", ["abc", "12..3", "((5))", "1,2x"])
def test_parse_number_garbage(text):
    with pytest.raises(ValueError):
        parse_number(text)


def test_parse_percent_is_fraction():
    assert parse_percent("0.94%") == D("0.0094")
    assert parse_percent("-") is None


def test_to_decimal_float_via_str_and_rejects():
    assert to_decimal(0.1) == D("0.1")
    for bad in (True, float("nan"), "inf", [1]):
        with pytest.raises(ValueError):
            to_decimal(bad)


def test_unit_conversion():
    assert convert(1, "crore", "million") == 10
    assert convert(1, "crore", "lakh") == 100
    assert convert(1, "billion", "crore") == 100
    assert convert(1, "lakh", "thousand") == 100
    assert convert("11716.54", "mn", "cr") == D("1171.654")
    assert convert(None, "mn", "cr") is None
    with pytest.raises(ValueError):
        convert(1, "furlong", "cr")


def test_indian_grouping_and_format():
    assert group_indian("11764705") == "1,17,64,705"
    assert group_indian("102035000") == "10,20,35,000"
    assert group_indian("999") == "999"
    assert group_indian("1000") == "1,000"
    assert format_indian(113799705, 0) == "11,37,99,705"
    assert format_indian("-1234567.855", 2) == "-12,34,567.86"  # half-up, not banker's
    assert format_inr(D("59847863112")) == "₹5,984.79 cr"
    assert format_inr(11716.54, "cr", from_unit="mn") == "₹1,171.65 cr"
    assert format_inr(D("123456789"), "lakh", 1) == "₹1,234.6 lakh"
    assert format_inr(D("-5e7"), symbol=False) == "-5.00 cr"
    assert format_inr(None) == "-"


def test_round_half_up_vs_bankers():
    assert round_half_up("2.125", 2) == D("2.13")
    assert round_half_up("0.5", 0) == D("1")


def test_pct_helpers():
    assert to_pct(D("0.070871")) == D("7.09")
    assert format_pct(D("0.0094")) == "0.94%"
    assert format_pct(None) == "-"


# ---------------------------------------------------------------- growth


def test_pct_change():
    assert growth.pct_change(100, 125) == D("0.25")
    assert growth.pct_change(-100, -50) == D("0.5")
    assert growth.pct_change(0, 5) is None
    assert growth.pct_change(None, 5) is None


def test_cagr():
    assert r2(growth.cagr(100, 121, 2) * 100) == D("10.00")
    # Orient Cables revenue FY24 6,577.67 -> FY26 11,716.54 mn over 2 years
    assert r2(growth.cagr("6577.67", "11716.54", 2) * 100) == D("33.46")
    assert growth.cagr(0, 10, 2) is None
    assert growth.cagr(-5, 10, 2) is None
    assert growth.cagr(10, 0, 2) == -1
    with pytest.raises(ValueError):
        growth.cagr(1, 2, 0)


def test_annualise_is_flagged():
    a = growth.annualise("327.83", 3)
    assert a.value == D("1311.32") and a.annualised and "not a forecast" in a.label
    full = growth.annualise(100, 12)
    assert full.value == 100 and not full.annualised
    with pytest.raises(ValueError):
        growth.annualise(1, 0)
    with pytest.raises(ValueError):
        growth.annualise(1, 13)


def test_ttm():
    assert growth.ttm([1, 2, 3, "4.5"]) == D("10.5")
    assert growth.ttm([1, None, 3, 4]) is None
    with pytest.raises(ValueError):
        growth.ttm([1, 2, 3])
    # FY26 11,716.54 + Q1FY27 4,891.56 - Q1FY26 x
    assert growth.ttm_from_fy("11716.54", "4891.56", "2000") == D("14608.10")
    assert growth.ttm_from_fy(1, None, 1) is None


# ---------------------------------------------------------------- ratios


def test_margins():
    assert ratios.gross_margin(100, 60) == D("0.4")
    assert ratios.ebitda_margin(15, 100) == D("0.15")
    assert r2(ratios.pat_margin("535.61", "11716.54") * 100) == D("4.57")
    assert ratios.pat_margin(1, 0) is None
    assert ratios.pat_margin(None, 10) is None
    assert ratios.ebitda(100, 20, 30, 10) == 140
    assert ratios.ebitda(100, 20, 30) == 150
    assert ratios.ebitda(100, None, 30) is None
    assert ratios.ebit(100, 20) == 120


def test_roe_variants_are_distinct():
    assert ratios.roe_on_closing_equity(20, 200) == D("0.1")
    assert ratios.roe_on_average_equity(20, 100, 300) == D("0.1")
    assert ratios.roe_on_average_equity(20, 100, 200) > ratios.roe_on_closing_equity(20, 200)
    assert ratios.roe_on_closing_equity(20, -5) is None
    assert ratios.roe_on_average_equity(20, None, 200) is None


def test_roce_and_leverage():
    assert ratios.roce(30, 100, 50, 50) == D("0.3")
    assert ratios.roce_gross(30, 100, 50) == D("0.2")
    assert ratios.roce(30, 10, 0, 20) is None  # negative capital employed
    assert ratios.debt_to_equity(50, 100) == D("0.5")
    assert ratios.net_debt(50, 80) == -30
    assert ratios.net_debt_to_ebitda(100, 40, 30) == 2
    assert ratios.net_debt_to_ebitda(100, 40, -30) is None
    assert ratios.interest_coverage(50, 10) == 5
    assert ratios.interest_coverage(50, 0) is None
    assert ratios.asset_turnover(200, 100) == 2


def test_working_capital_days():
    assert ratios.inventory_days(100, 365) == 100
    assert ratios.receivable_days(50, 365) == 50
    assert ratios.payable_days(30, 365) == 30
    assert ratios.receivable_days(10, 91, days_in_period=91) == 10
    assert ratios.cash_conversion_cycle(100, 50, 30) == 120
    assert ratios.cash_conversion_cycle(100, None, 30) is None
    with pytest.raises(ValueError):
        ratios.inventory_days(1, 1, days_in_period=0)


def test_cash_flow():
    assert ratios.cfo_to_ebitda(80, 100) == D("0.8")
    assert ratios.fcf(100, 30) == 70
    assert ratios.fcf(100, -30) == 70  # capex as printed (outflow) in cash-flow statement
    assert ratios.fcf(None, 30) is None


# ---------------------------------------------------------------- valuation


def test_valuation_basics_and_validation():
    assert valuation.eps(100, 50) == 2
    assert valuation.pe(30, 2) == 15
    assert valuation.pe(30, -1) is None
    assert valuation.pe(30, None) is None
    assert valuation.bvps(1000, 100) == 10
    assert valuation.pb(20, 10) == 2
    assert valuation.pb(20, 0) is None
    assert valuation.ev(1000, 200, 50) == 1150
    assert valuation.ev(1000, None, 50) is None
    assert valuation.ev_ebitda(1150, 115) == 10
    assert valuation.ev_ebitda(1150, 0) is None
    assert valuation.price_to_sales(1000, 500) == 2
    assert valuation.earnings_yield(2, 40) == D("0.05")
    for bad in (lambda: valuation.eps(1, -5), lambda: valuation.eps(1, 0),
                lambda: valuation.eps(1, "1.5"), lambda: valuation.market_cap(-1, 10),
                lambda: valuation.pe(0, 1), lambda: valuation.post_issue_shares(10, -1),
                lambda: valuation.shares_from_amount(-1, 10)):  # fmt: skip
        with pytest.raises(ValueError):
            bad()


def test_justified_pb_and_fair_value():
    assert valuation.justified_pb("0.18", "0.14", "0.08") == D("10") / D("6")
    with pytest.raises(ValueError):
        valuation.justified_pb("0.18", "0.08", "0.08")
    fv = valuation.implied_fair_value("17.98", "1.5", "2.5")
    assert (fv.low, fv.mid, fv.high) == (D("26.970"), D("35.960"), D("44.950"))
    lo, _mid, _hi = fv.upside_vs(34)
    assert lo < 0
    with pytest.raises(ValueError):
        valuation.implied_fair_value(10, 3, 2)


def test_shares_from_amount_rounding():
    assert valuation.shares_from_amount(10, 3) == 3
    assert valuation.shares_from_amount(11, 6, rounding="half_up") == 2


# ---------------------------------------------------------------- GOLDEN: Moneyview


MV_POST = D("1760231268")
MV_PRICE = D("34")


def test_golden_moneyview_market_cap():
    mcap = valuation.market_cap(MV_POST, MV_PRICE)
    assert mcap == D("59847863112")
    assert r2(convert(mcap, "inr", "cr")) == D("5984.79")


def test_golden_moneyview_issue():
    fresh = valuation.shares_from_amount(convert(750, "cr", "inr"), MV_PRICE)
    assert fresh == D("220588235")  # floor(220,588,235.29)
    im = ipo.issue_math(MV_PRICE, fresh, D("100494200"))
    assert r2(convert(im.ofs_amount, "inr", "cr")) == D("341.68")
    assert im.total_shares == D("321082435")
    res = ipo.reservation_split(im.total_shares)
    assert res.retail == D("112378852")  # floor(35% x 321,082,435 = 112,378,852.25)
    assert ipo.retail_lots_available(res.retail, 441) == D("254827")


def test_golden_moneyview_pe_pb():
    pat_rupees = convert("397.3", "crore", "inr")
    e = valuation.eps(pat_rupees, MV_POST)
    assert r2(valuation.pe(MV_PRICE, e)) == D("15.06")
    assert r2(valuation.pb(MV_PRICE, "17.98")) == D("1.89")


# ---------------------------------------------------------------- GOLDEN: Orient Cables


OC_PRICE = D("272")


def test_golden_orient_issue_and_mcap():
    pre = parse_number("10,20,35,000")
    fresh = valuation.shares_from_amount(convert(320, "cr", "inr"), OC_PRICE)
    assert fresh == parse_number("1,17,64,705")  # floor(11,764,705.88)
    post = valuation.post_issue_shares(pre, fresh)
    assert post == parse_number("11,37,99,705")
    assert format_indian(post, 0) == "11,37,99,705"
    mcap = valuation.market_cap(post, OC_PRICE)
    assert r2(convert(mcap, "inr", "cr")) == D("3095.35")


def test_golden_orient_financials():
    rev_cr = convert(parse_number("11,716.54"), "million", "crore")
    assert r2(rev_cr) == D("1171.65")
    assert convert(parse_number("535.61"), "million", "crore") == D("53.561")
    # RHP-printed EPS 5.27 is on weighted-average shares (Ind AS 33); PAT / pre-issue shares
    # would give ~5.25, so this golden uses the printed EPS, not a derived one.
    assert round_half_up(valuation.pe(OC_PRICE, "5.27"), 1) == D("51.6")


# ---------------------------------------------------------------- ipo


def test_golden_allotment_floor():
    assert to_pct(ipo.allotment_probability_floor("14.11")) == D("7.09")
    assert ipo.allotment_probability_floor("0.8") == 1
    with pytest.raises(ValueError):
        ipo.allotment_probability_floor(0)
    assert ipo.allotment_probability(254827, 1000000) == D("0.254827")
    assert ipo.allotment_probability(10, 5) == 1


def test_reservation_split_rules():
    res = ipo.reservation_split(1000, employee_shares=100)
    assert res.net_offer == 900
    assert (res.qib, res.nii, res.retail) == (450, 135, 315)
    assert (res.snii, res.bnii) == (45, 90)
    assert res.anchor_max == 270 and res.qib_ex_anchor_min == 180
    # exact thirds: a Decimal(1)/3 fraction would floor these to 99 / 9
    big = ipo.reservation_split(2000)
    assert (big.nii, big.snii, big.bnii) == (300, 100, 200)
    small = ipo.reservation_split(200)
    assert (small.nii, small.snii, small.bnii) == (30, 10, 20)
    with pytest.raises(ValueError):
        ipo.reservation_split(1000, snii_ratio=(4, 3))
    alt = ipo.reservation_split(1000, qib_pct=75, nii_pct=15, retail_pct=10)
    assert alt.retail == 100
    with pytest.raises(ValueError):
        ipo.reservation_split(1000, qib_pct=50, nii_pct=15, retail_pct=30)
    with pytest.raises(ValueError):
        ipo.reservation_split(1000, anchor_share_of_qib=70)
    with pytest.raises(ValueError):
        ipo.reservation_split(100, employee_shares=200)


def test_subscription():
    assert ipo.rebase_subscription(300, 100) == 3
    s = ipo.subscription_by_category({"qib": 500, "retail": 100}, {"qib": 100, "retail": 100})
    assert s == {"qib": 5, "retail": 1, "total": 3}
    with pytest.raises(ValueError):
        ipo.subscription_by_category({"qib": 1}, {"nii": 1})
    with pytest.raises(ValueError):
        ipo.rebase_subscription(1, 0)


def test_listing_gain_and_gmp():
    assert ipo.listing_gain(100, 125) == D("0.25")
    g = ipo.gmp_implied_price(272, 20)
    assert g.implied_price == 292 and "UNOFFICIAL" in g.label
    assert ipo.gmp_implied_price(272, None) is None


def test_lock_in_schedule():
    ev = ipo.lock_in_schedule(date(2026, 1, 31), anchor_shares=1001, promoter_min_contribution_shares=500)
    by = {e.holder: e for e in ev}
    assert by["anchor (50%)"].unlock_date == date(2026, 3, 2)  # 30 days from allotment
    assert by["anchor (50%)"].shares == 500
    assert by["anchor (remaining 50%)"].unlock_date == date(2026, 5, 1)  # 90 days
    assert by["anchor (remaining 50%)"].shares == 501
    assert by["promoter minimum contribution"].unlock_date == date(2027, 7, 31)
    assert by["pre-IPO non-promoter shareholders"].unlock_date == date(2026, 7, 31)
    assert by["promoter excess over minimum"].shares is None
    assert [e.unlock_date for e in ev] == sorted(e.unlock_date for e in ev)
    custom = ipo.lock_in_schedule(date(2026, 1, 1), promoter_min_months=36)
    assert {e.holder: e for e in custom}["promoter minimum contribution"].unlock_date == date(2029, 1, 1)
    assert ipo.add_months(date(2024, 1, 31), 1) == date(2024, 2, 29)


# ---------------------------------------------------------------- dates


HOL = {date(2026, 10, 2)}  # Gandhi Jayanti (Fri)


def test_golden_bidding_days_skip_weekend():
    opened = date(2026, 9, 25)  # Friday
    assert dates.bidding_day_number(opened, opened) == 1
    assert dates.bidding_day_number(opened, date(2026, 9, 26)) is None  # Saturday
    assert dates.bidding_day_number(opened, date(2026, 9, 27)) is None  # Sunday
    assert dates.bidding_day_number(opened, date(2026, 9, 28)) == 2
    assert dates.bidding_day_number(opened, date(2026, 9, 29)) == 3
    assert dates.bidding_day_number(opened, date(2026, 9, 24)) is None
    assert dates.bidding_dates(opened, 3) == [date(2026, 9, 25), date(2026, 9, 28), date(2026, 9, 29)]


def test_bidding_days_with_holiday():
    opened = date(2026, 10, 1)  # Thursday
    assert dates.bidding_dates(opened, 3, HOL) == [date(2026, 10, 1), date(2026, 10, 5), date(2026, 10, 6)]
    assert dates.bidding_day_number(opened, date(2026, 10, 2), HOL) is None
    assert dates.bidding_day_number(opened, date(2026, 10, 5), HOL) == 2
    with pytest.raises(ValueError):
        dates.bidding_dates(date(2026, 9, 26), 3)


def test_business_day_arithmetic():
    assert dates.is_weekend(date(2026, 9, 26))
    assert not dates.is_business_day(date(2026, 10, 2), HOL)
    assert dates.add_business_days(date(2026, 9, 29), 3, HOL) == date(2026, 10, 5)
    assert dates.add_business_days(date(2026, 9, 28), -1) == date(2026, 9, 25)
    assert dates.add_business_days(date(2026, 9, 26), 0) == date(2026, 9, 26)
    assert dates.business_days_between(date(2026, 9, 25), date(2026, 9, 29)) == 2
    assert dates.business_days_between(date(2026, 9, 29), date(2026, 9, 25)) == -2
    assert dates.next_business_day(date(2026, 10, 1), HOL) == date(2026, 10, 5)


def test_ist_helpers():
    utc = datetime(2026, 9, 27, 20, 0, tzinfo=UTC)
    ist = dates.to_ist(utc)
    assert (ist.date(), ist.hour, ist.minute) == (date(2026, 9, 28), 1, 30)
    assert dates.ist_datetime(date(2026, 9, 29), 17).utcoffset().total_seconds() == 19800
    with pytest.raises(ValueError):
        dates.to_ist(datetime(2026, 1, 1))
    assert dates.now_ist().tzinfo is dates.IST
    assert isinstance(dates.today_ist(), date)

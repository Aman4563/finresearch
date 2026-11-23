"""The valuation toolkit of #504 part A (fincalc.valuation): cost of equity, WACC, the explicit DCF and its checks,
the lender methods and the peer range. Every expected value is worked by hand in the test's docstring or comment, or
comes from a closed form named there; none is copied from the code's output."""

from __future__ import annotations

from decimal import Decimal as D

import pytest

from finresearch.fincalc import valuation as V

TINY = D("1e-20")  # Decimal division carries 28 digits: "equal" means equal to twenty places


# --------------------------------------------------------------------------- discount rate
def test_capm_cost_of_equity_by_hand():
    """6.5 % + 1.2 x 6 % = 6.5 % + 7.2 % = 13.7 %; the inputs travel with the answer."""
    c = V.cost_of_equity_capm("0.065", "1.2", "0.06")
    assert c.cost_of_equity == D("0.137")
    assert (c.risk_free, c.beta, c.equity_risk_premium) == (D("0.065"), D("1.2"), D("0.06"))
    # beta 0 is the risk-free rate itself; a negative beta lowers the rate (the formula holds): 6.5 % - 0.5 x 6 %
    assert V.cost_of_equity_capm("0.065", 0, "0.06").cost_of_equity == D("0.065")
    assert V.cost_of_equity_capm("0.065", "-0.5", "0.06").cost_of_equity == D("0.035")


@pytest.mark.parametrize(
    ("args", "why"),
    [
        (("6.5", "1.2", "0.06"), "risk_free must be a fraction"),  # a percent typed where a fraction belongs
        (("0.065", "1.2", "6"), "equity_risk_premium must be a fraction"),
        (("0.065", "1.2", "0"), "equity_risk_premium must be > 0"),  # no premium is not a premium
        (("0.065", "1.2", "-0.02"), "equity_risk_premium must be > 0"),
        (("0.065", "NaN", "0.06"), "non-finite"),
    ],
)
def test_capm_refuses_invalid_input_with_the_reason(args, why):
    with pytest.raises(ValueError, match=why):
        V.cost_of_equity_capm(*args)


def test_after_tax_cost_of_debt_by_hand():
    """9 % x (1 - 25 %) = 6.75 %; no tax shield (tax 0) leaves 9 %."""
    c = V.after_tax_cost_of_debt("0.09", "0.25")
    assert (c.after_tax, c.pre_tax, c.tax_rate) == (D("0.0675"), D("0.09"), D("0.25"))
    assert V.after_tax_cost_of_debt("0.09", 0).after_tax == D("0.09")
    for bad in (("0.09", "1"), ("0.09", "-0.1"), ("-0.01", "0.25"), ("9", "0.25"), ("0.09", "25")):
        with pytest.raises(ValueError):
            V.after_tax_cost_of_debt(*bad)


def test_wacc_market_value_weights_by_hand():
    """E 8,000, net debt 2,000: weights 0.8 and 0.2; 0.8 x 13.7 % + 0.2 x 6.75 % = 10.96 % + 1.35 % = 12.31 %."""
    w = V.wacc("0.137", "0.0675", 8000, 2000)
    assert (w.weight_equity, w.weight_debt, w.debt_used) == (D("0.8"), D("0.2"), D(2000))
    assert w.wacc == D("0.1231") and w.net_cash is False and "market-value weights" in w.rule


def test_wacc_with_net_cash_is_the_cost_of_equity_and_says_which_rule():
    """Net cash of 500 (net debt -500): the debt weight is 0, never negative, so WACC = Ke = 13.7 %; the same at
    exactly zero net debt. The rule and the direction of its error are returned with the number."""
    for net_debt in (-500, 0):
        w = V.wacc("0.137", "0.0675", 8000, net_debt)
        assert (w.wacc, w.weight_equity, w.weight_debt, w.debt_used) == (D("0.137"), D(1), D(0), D(0))
        assert w.net_cash is True and w.net_debt == D(net_debt)
        assert w.rule == V.NET_CASH_RULE and "understates the rate" in w.rule


def test_wacc_refuses_invalid_input():
    for bad in (("0.137", "0.0675", 0, 100), ("0.137", "0.0675", -5, 100), ("0", "0.0675", 100, 10),
                ("13.7", "0.0675", 100, 10), ("0.137", "-0.01", 100, 10)):  # fmt: skip
        with pytest.raises(ValueError):
            V.wacc(*bad)


# --------------------------------------------------------------------------- explicit DCF
def test_dcf_explicit_growth_method_with_a_negative_year_by_hand():
    """Flows -50, 100 at 25 %, terminal growth 5 %. PV1 = -50 / 1.25 = -40; PV2 = 100 / 1.5625 = 64;
    TV = 100 x 1.05 / (0.25 - 0.05) = 525, PV = 525 / 1.5625 = 336; value = -40 + 64 + 336 = 360;
    terminal share = 336 / 360 = 0.9333. With a year-2 EBITDA of 150 the growth method implies 525 / 150 = 3.5x."""
    v = V.dcf_explicit([-50, 100], "0.25", terminal_growth="0.05", terminal_metric=150)
    assert v.pv_cash_flows == [D(-40), D(64)] and v.periods == [D(1), D(2)]
    assert (v.pv_explicit, v.terminal_value, v.pv_terminal, v.value) == (D(24), D(525), D(336), D(360))
    assert v.terminal_share.quantize(D("0.0001")) == D("0.9333") and v.terminal_method == "growth"
    assert v.implied_exit_multiple == D("3.5")
    assert (
        v.implied_terminal_growth is None and "exit-multiple method" in v.unknown["implied_terminal_growth"]
    )


def test_dcf_explicit_exit_multiple_chosen_to_equal_the_growth_method():
    """Independent check: the exit multiple the growth method implies (3.5x of 150, above) must reproduce the same
    value, and the growth it implies must be the 5 % that built it: (525 x 0.25 - 100) / (525 + 100) = 0.05."""
    v = V.dcf_explicit([-50, 100], "0.25", exit_multiple="3.5", terminal_metric=150)
    assert (v.terminal_value, v.pv_terminal, v.value, v.terminal_method) == (
        D(525),
        D(336),
        D(360),
        "exit_multiple",
    )
    assert v.implied_terminal_growth == D("0.05")
    assert v.implied_exit_multiple is None and "growth method" in v.unknown["implied_exit_multiple"]


@pytest.mark.parametrize("years", [1, 5, 10])
def test_dcf_explicit_equals_the_perpetuity_and_gordon_closed_forms(years):
    """Closed forms as the independent check: a level 100 a year for ever at 10 % is 100 / 0.10 = 1,000 however many
    years are written out; a flow of 105 growing 5 % at 11 % is 105 / (0.11 - 0.05) = 1,750 (Gordon 1962)."""
    assert abs(V.dcf_explicit([100] * years, "0.10", terminal_growth=0).value - 1000) < TINY
    flows = [D(100) * D("1.05") ** t for t in range(1, years + 1)]
    assert abs(V.dcf_explicit(flows, "0.11", terminal_growth="0.05").value - 1750) < TINY
    # and it agrees with the two-stage `dcf` already in the module on a path that function can express
    old = V.dcf(100, "0.08", "0.12", "0.04", years=years)
    new = V.dcf_explicit(
        [D(100) * D("1.08") ** t for t in range(1, years + 1)], "0.12", terminal_growth="0.04"
    )
    assert abs(new.value - old.value) < TINY and abs(new.pv_terminal - old.pv_terminal) < TINY


def test_dcf_explicit_mid_year_convention_by_hand():
    """Rate 21 % so that sqrt(1.21) = 1.1 exactly. Flows 110 and 133.1 arrive at t = 0.5 and 1.5:
    PV1 = 110 x 1.1 / 1.21 = 100; PV2 = 133.1 x 1.1 / 1.4641 = 100. Growth 10 %: TV = 133.1 x 1.1 / 0.11 = 1,331,
    discounted 1.5 years (its flows are mid-year too): 1,331 x 1.1 / 1.4641 = 1,000. Value 1,200, share 0.8333.
    Year-end discounting of the same numbers gives 1,200 / 1.1 = 1,090.909..."""
    v = V.dcf_explicit(
        ["110", "133.1"], "0.21", terminal_growth="0.10", terminal_metric="266.2", mid_year=True
    )
    assert v.periods == [D("0.5"), D("1.5")] and v.terminal_period == D("1.5")
    assert [abs(p - 100) < TINY for p in v.pv_cash_flows] == [True, True]
    assert abs(v.pv_terminal - 1000) < TINY and abs(v.value - 1200) < TINY and v.terminal_value == D(1331)
    assert v.terminal_share.quantize(D("0.0001")) == D("0.8333")
    end = V.dcf_explicit(["110", "133.1"], "0.21", terminal_growth="0.10")
    assert abs(end.value - D(1200) / D("1.1")) < TINY and end.terminal_period == D(2)
    # the implied exit multiple is on year-end discounting of the multiple: 1,331 x 1.1 / 266.2 = 5.5x, and a
    # 5.5x exit (always discounted the full 2 years) gives back 1,000, the value 1,200 and the 10 % growth
    assert abs(v.implied_exit_multiple - D("5.5")) < TINY
    x = V.dcf_explicit(["110", "133.1"], "0.21", exit_multiple="5.5", terminal_metric="266.2", mid_year=True)
    assert x.terminal_period == D(2) and abs(x.pv_terminal - 1000) < TINY and abs(x.value - 1200) < TINY
    assert abs(x.implied_terminal_growth - D("0.10")) < TINY


@pytest.mark.parametrize("years", [1, 4, 9])
def test_dcf_explicit_mid_year_equals_its_closed_form(years):
    """Closed form: a flow of CF1 growing at g and arriving mid-year is worth sqrt(1 + r) x CF1 / (r - g) whatever
    the number of years written out. r = 21 %, g = 10 %, CF1 = 110: 1.1 x 110 / 0.11 = 1,100."""
    flows = [D(110) * D("1.1") ** t for t in range(years)]
    v = V.dcf_explicit(flows, "0.21", terminal_growth="0.10", mid_year=True)
    assert abs(v.value - 1100) < TINY


def test_dcf_explicit_refuses_terminal_growth_at_or_above_the_rate_and_other_invalid_input():
    with pytest.raises(ValueError, match="discount_rate must exceed terminal_growth"):
        V.dcf_explicit([100, 110], "0.10", terminal_growth="0.10")  # equal: the terminal value diverges
    with pytest.raises(ValueError, match="discount_rate must exceed terminal_growth"):
        V.dcf_explicit([100, 110], "0.10", terminal_growth="0.12")
    with pytest.raises(ValueError, match="one per call"):
        V.dcf_explicit([100], "0.10")
    with pytest.raises(ValueError, match="one per call"):
        V.dcf_explicit([100], "0.10", terminal_growth="0.03", exit_multiple=8, terminal_metric=50)
    with pytest.raises(ValueError, match="final year's cash flow must be > 0"):
        V.dcf_explicit([100, -5], "0.10", terminal_growth="0.03")  # a perpetuity of a loss is not a value
    with pytest.raises(ValueError, match="terminal_metric"):
        V.dcf_explicit([100], "0.10", exit_multiple=8)
    with pytest.raises(ValueError, match="terminal_metric must be > 0"):
        V.dcf_explicit([100], "0.10", exit_multiple=8, terminal_metric=-20)  # 8x a negative EBITDA
    for bad in ({"cash_flows": []}, {"cash_flows": 100}, {"discount_rate": "0"}, {"discount_rate": "12"},
                {"mid_year": "false"}):  # fmt: skip
        with pytest.raises(ValueError):
            V.dcf_explicit(**{"cash_flows": [100], "discount_rate": "0.1", "terminal_growth": "0.03", **bad})


def test_dcf_explicit_leaves_unknown_figures_none_with_the_reason():
    """-1,000 then 10 at 25 % and 5 %: PV = -800 + 6.4, TV = 10.5 / 0.2 = 52.5 -> 33.6; value = -760. A share of a
    negative value means nothing: None, never 0. Without a metric the implied multiple is unknown too; with a
    negative one (a loss at EBITDA level) likewise. A negative final flow has no implied growth."""
    v = V.dcf_explicit([-1000, 10], "0.25", terminal_growth="0.05")
    assert v.value == D(-760) and v.terminal_share is None and "not positive" in v.unknown["terminal_share"]
    assert (
        v.implied_exit_multiple is None
        and "terminal_metric was not given" in v.unknown["implied_exit_multiple"]
    )
    neg = V.dcf_explicit([100, 110], "0.25", terminal_growth="0.05", terminal_metric=-3)
    assert neg.implied_exit_multiple is None and "not positive" in neg.unknown["implied_exit_multiple"]
    x = V.dcf_explicit([100, -10], "0.25", exit_multiple=4, terminal_metric=50)
    assert (
        x.implied_terminal_growth is None and "final year's cash flow" in x.unknown["implied_terminal_growth"]
    )
    assert x.terminal_value == D(200) and x.pv_terminal == D(128)  # 4 x 50 = 200; 200 / 1.5625 = 128


# --------------------------------------------------------------------------- projected cash flows
def test_fcf_path_two_years_by_hand():
    """Revenue 1,000; growth 10 % then 20 %; EBIT margin 20 % then 25 %; tax 25 %; D&A 4 % and capex 6 % of revenue;
    working capital 10 % of the change in revenue.
    Year 1: revenue 1,100; EBIT 220; tax 55; NOPAT 165; D&A 44; capex 66; working capital 10 (10 % of 100);
            FCF = 165 + 44 - 66 - 10 = 133; EBITDA 264.
    Year 2: revenue 1,320; EBIT 330; tax 82.5; NOPAT 247.5; D&A 52.8; capex 79.2; working capital 22 (10 % of 220);
            FCF = 247.5 + 52.8 - 79.2 - 22 = 199.1; EBITDA 382.8."""
    p = V.fcf_path(1000, ["0.10", "0.20"], ["0.20", "0.25"], "0.25", "0.04", "0.06", "0.10")
    assert p.revenue == [D(1100), D(1320)] and p.ebit == [D(220), D(330)]
    assert p.tax == [D(55), D("82.5")] and p.nopat == [D(165), D("247.5")]
    assert p.da == [D(44), D("52.8")] and p.capex == [D(66), D("79.2")] and p.nwc_change == [D(10), D(22)]
    assert p.fcf == [D(133), D("199.1")] and p.ebitda == [D(264), D("382.8")]
    assert (p.final_revenue, p.final_ebit, p.final_ebitda, p.final_fcf) == (
        D(1320),
        D(330),
        D("382.8"),
        D("199.1"),
    )
    # the path feeds the explicit DCF as it is
    assert V.dcf_explicit(p.fcf, "0.25", terminal_growth="0.05").cash_flows == p.fcf


def test_fcf_path_loss_year_pays_no_tax_and_gets_no_refund():
    """Revenue falls 10 % to 900 at an EBIT margin of -5 %: EBIT -45, tax 0 (not -11.25: no refund is assumed),
    NOPAT -45; D&A 36; capex 54; working capital released 10 (10 % of -100);
    FCF = -45 + 36 - 54 + 10 = -53. The rule is returned with the numbers."""
    p = V.fcf_path(1000, ["-0.10"], ["-0.05"], "0.25", "0.04", "0.06", "0.10")
    assert (p.revenue, p.ebit, p.tax, p.nopat) == ([D(900)], [D(-45)], [D(0)], [D(-45)])
    assert p.nwc_change == [D(-10)] and p.fcf == [D(-53)] and "no refund" in p.tax_rule


def test_fcf_path_refuses_invalid_input():
    ok = {"revenue": 1000, "growth_path": ["0.1"], "ebit_margin_path": ["0.2"], "tax_rate": "0.25",
          "da_pct": "0.04", "capex_pct": "0.06", "nwc_pct_of_change": "0.1"}  # fmt: skip
    for bad, why in (
        ({"revenue": 0}, "revenue must be > 0"),
        ({"growth_path": ["0.1", "0.1"]}, "one EBIT margin for each year"),  # paths of different lengths
        ({"growth_path": []}, "non-empty list"),
        ({"growth_path": ["10"]}, "must be a fraction"),  # 10 % typed as 10
        ({"growth_path": ["-1"]}, "must be > -1"),
        ({"ebit_margin_path": ["20"]}, "must be a fraction"),
        ({"tax_rate": "25"}, "must be a fraction"),
        ({"tax_rate": "-0.1"}, "tax_rate must be >= 0"),
        ({"da_pct": "-0.01"}, "da_pct must be >= 0"),
        ({"capex_pct": "-0.01"}, "capex_pct must be >= 0"),
    ):
        with pytest.raises(ValueError, match=why):
            V.fcf_path(**{**ok, **bad})


# --------------------------------------------------------------------------- DCF sanity checks
def test_dcf_checks_clean_case_is_ok_and_carries_its_bands():
    """Rate 12 %, terminal growth 5 %, risk-free 6.5 %, terminal share 0.60: growth is below the rate and below the
    risk-free rate, the share is inside 0.35-0.75 and the rate is 5.5 points over risk-free (inside 0-15)."""
    c = V.dcf_checks("0.12", terminal_growth="0.05", risk_free="0.065", terminal_share="0.60")
    assert (c.status, c.flags, c.errors, c.warnings, c.not_checked) == ("ok", [], 0, 0, {})
    assert c.checked == list(V.DCF_CHECKS)
    assert c.spread_over_terminal_growth == D("0.07") and c.spread_over_risk_free == D("0.055")
    assert c.bands == {
        "terminal_share_low": D("0.35"),
        "terminal_share_high": D("0.75"),
        "discount_rate_max_over_risk_free": D("0.15"),
    }


def test_dcf_checks_terminal_growth_equal_to_the_rate_is_an_error():
    for g in ("0.12", "0.13"):  # equal, and above
        c = V.dcf_checks("0.12", terminal_growth=g, risk_free="0.065", terminal_share="0.60")
        names = {f.name: f.level for f in c.flags}
        assert names["terminal_growth_at_or_above_discount_rate"] == "error"
        assert c.status == "error" and c.errors == 1
        assert names["terminal_growth_above_risk_free"] == "warn"  # 12 % growth for ever also beats 6.5 %


def test_dcf_checks_each_warning_by_its_boundary():
    """Boundaries by hand. Growth vs risk-free 6.5 %: 6.5 % passes, 6.6 % warns. Share band 0.35-0.75: both ends
    pass, 0.3499 and 0.7501 warn. Rate vs risk-free 6.5 %: it must be above 6.5 % and at most 21.5 %."""

    def names(**kw):
        base = {
            "discount_rate": "0.12",
            "terminal_growth": "0.05",
            "risk_free": "0.065",
            "terminal_share": "0.6",
        }
        return [f.name for f in V.dcf_checks(**{**base, **kw}).flags]

    assert names(terminal_growth="0.065") == [] and names(terminal_growth="0.066") == [
        "terminal_growth_above_risk_free"
    ]
    assert names(terminal_share="0.35") == [] and names(terminal_share="0.75") == []
    assert names(terminal_share="0.3499") == ["terminal_share_outside_band"]
    assert names(terminal_share="0.7501") == ["terminal_share_outside_band"]
    assert names(discount_rate="0.215") == [] and names(discount_rate="0.2151") == [
        "discount_rate_outside_band"
    ]
    assert names(discount_rate="0.065", terminal_growth="0.03") == ["discount_rate_outside_band"]
    assert names(discount_rate="0.0651", terminal_growth="0.03") == []
    warned = V.dcf_checks("0.12", terminal_growth="0.05", risk_free="0.065", terminal_share="0.9")
    assert (warned.status, warned.errors, warned.warnings) == ("warn", 0, 1)


def test_dcf_checks_a_missing_input_is_not_checked_never_ok():
    """Only the rate is known: nothing can be checked, and the answer says so rather than passing."""
    c = V.dcf_checks("0.12")
    assert c.status == "incomplete" and c.flags == [] and c.checked == []
    assert set(c.not_checked) == set(V.DCF_CHECKS) and all("not given" in w for w in c.not_checked.values())
    # without the risk-free rate two checks cannot run; the other two do
    part = V.dcf_checks("0.12", terminal_growth="0.05", terminal_share="0.6")
    assert part.status == "incomplete" and part.spread_over_risk_free is None
    assert set(part.not_checked) == {"terminal_growth_above_risk_free", "discount_rate_outside_band"}
    assert part.checked == ["terminal_growth_at_or_above_discount_rate", "terminal_share_outside_band"]
    # a finding outranks "incomplete", and what was not checked is still listed
    bad = V.dcf_checks("0.12", terminal_growth="0.12")
    assert bad.status == "error" and "terminal_share_outside_band" in bad.not_checked


def test_dcf_checks_steady_state_share_for_the_horizon():
    """The benchmark a flagged share is put beside: a company growing at g from year 1 has ((1+g)/(1+r))^n of its
    value beyond year n. (1.05 / 1.12)^5 = 0.9375^5 = 0.72420; the band's two corners are (1.03 / 1.14)^10 = 0.3625
    and that 0.7242, rounded outwards to 0.35 and 0.75."""
    c = V.dcf_checks("0.12", terminal_growth="0.05", risk_free="0.065", terminal_share="0.82", years=5)
    assert c.steady_state_share.quantize(D("0.00001")) == D("0.72420")
    (flag,) = c.flags
    assert flag.name == "terminal_share_outside_band" and "0.7242" in flag.message
    low = V.dcf_checks("0.14", terminal_growth="0.03", years=10).steady_state_share
    assert low.quantize(D("0.0001")) == D("0.3625")
    assert V.dcf_checks("0.12", terminal_growth="0.05").steady_state_share is None  # no horizon given
    for bad in ({"years": 0}, {"years": 2.5}, {"discount_rate": "12"}, {"risk_free": "6.5"}):
        with pytest.raises(ValueError):
            V.dcf_checks(**{"discount_rate": "0.12", "terminal_growth": "0.05", **bad})


# --------------------------------------------------------------------------- lender methods
@pytest.mark.parametrize("ending", [{"terminal_growth": "0.05"}, {"fade_years": 4}, {"fade_years": 0}])
@pytest.mark.parametrize("payout", ["0", "0.3", "1"])
def test_residual_income_with_roe_equal_to_the_cost_of_equity_is_exactly_book_value(ending, payout):
    """Closed form: a company that earns exactly its cost of equity adds nothing to book value, whatever it pays out
    and however the forecast ends. Book 500, ROE = Ke = 13 % for five years: value = 500, exactly."""
    v = V.residual_income(500, ["0.13"] * 5, "0.13", payout, **ending)
    assert v.value == D(500) and v.value_to_book == D(1)
    assert all(x == 0 for x in v.residual_income) and v.pv_continuing == 0


@pytest.mark.parametrize("years", [1, 3, 7])
def test_residual_income_ddm_and_justified_pb_agree_in_the_steady_state(years):
    """Independent check across three functions. ROE 18 %, payout 50 %, so book, earnings and dividends all grow at
    g = 18 % x (1 - 0.5) = 9 %; Ke 13 %; book 100. Gordon's closed form gives book x (ROE - g) / (Ke - g)
    = 100 x 0.09 / 0.04 = 225. Residual income with terminal growth 9 % must give 225 for any number of years
    written out; the dividend model too: D1 = 18 % x 100 x 50 % = 9, 9 / 0.04 = 225; and book x justified P/B."""
    ri = V.residual_income(100, ["0.18"] * years, "0.13", "0.5", terminal_growth="0.09")
    assert abs(ri.value - 225) < TINY and abs(ri.value_to_book - D("2.25")) < TINY
    assert 100 * V.justified_pb("0.18", "0.13", "0.09") == D(225)
    d0 = D(9) / D("1.09")  # the dividend just paid, which grows 9 % into next year's 9
    assert abs(V.ddm(d0, "0.09", "0.13").value - 225) < TINY


def test_residual_income_level_perpetuity_by_hand():
    """Full payout keeps book at 100; ROE 18 % against Ke 12 % is a residual income of 6 a year for ever:
    100 + 6 / 0.12 = 150 (= book x ROE / Ke). Step by step: 100 + 6 / 1.12 + (6 / 0.12) / 1.12 = 100 + 56 / 1.12."""
    v = V.residual_income(100, ["0.18"], "0.12", 1, terminal_growth=0)
    assert abs(v.value - 150) < TINY and v.continuing_value == D(50) and v.closing_book == D(100)


def test_residual_income_fade_to_the_cost_of_equity_by_hand():
    """Book 100, Ke 25 %, ROE 45 % in year 1, then a 2-year linear fade to Ke: 35 % in year 2, 25 % in year 3.
    Payout 50 %. Year 1: residual income (45 % - 25 %) x 100 = 20; earnings 45, half retained: book 122.5.
    Year 2: (35 % - 25 %) x 122.5 = 12.25; earnings 42.875, book 143.9375. Year 3: ROE = Ke, residual income 0.
    PV = 20 / 1.25 + 12.25 / 1.5625 = 16 + 7.84 = 23.84; value 123.84. Nothing is earned above Ke after the fade."""
    v = V.residual_income(100, ["0.45"], "0.25", "0.5", fade_years=2)
    assert v.roe == [D("0.45"), D("0.35"), D("0.25")] and v.opening_book == [
        D(100),
        D("122.5"),
        D("143.9375"),
    ]
    assert v.residual_income == [D(20), D("12.25"), D(0)]
    assert (v.pv_explicit, v.pv_continuing, v.value) == (D(16), D("7.84"), D("123.84"))
    assert v.continuing_value is None and "fade" in v.unknown["continuing_value"]
    # fade_years 0: the excess return stops with the forecast; 100 + 16
    assert V.residual_income(100, ["0.45"], "0.25", "0.5", fade_years=0).value == D(116)


def test_residual_income_loss_year_pays_no_dividend_by_hand():
    """Book 100, Ke 25 %, ROE -10 % then 20 %, payout 50 %, no continuing value. Year 1: loss 10, no dividend, book
    90; residual income (-10 % - 25 %) x 100 = -35. Year 2: (20 % - 25 %) x 90 = -4.5; earnings 18, 9 retained,
    book 99. PV = -35 / 1.25 - 4.5 / 1.5625 = -28 - 2.88 = -30.88; value 69.12, below book as it should be."""
    v = V.residual_income(100, ["-0.10", "0.20"], "0.25", "0.5", fade_years=0)
    assert v.opening_book == [D(100), D(90)] and v.closing_book == D(99)
    assert v.residual_income == [D(-35), D("-4.5")] and v.value == D("69.12")


@pytest.mark.parametrize(("payout", "fade"), [("0.4", 5), ("0", 3), ("1", 1), ("0.75", 0)])
def test_residual_income_with_a_fade_equals_discounted_dividends_plus_closing_book(payout, fade):
    """Independent check by the dividend-discount identity (clean surplus; Ohlson 1995): once the fade has brought
    ROE to the cost of equity the company is worth its book value from then on, so its value today is the present
    value of the dividends paid until then plus the closing book value discounted from then. Worked here with a
    loop over DIVIDENDS, never over residual incomes. Book 250; ROE 22 %, 19 %, -4 % (a loss: no dividend), 17 %;
    Ke 12.5 %. (For book 100, Ke 25 %, ROE 45 %, payout 50 %, fade 2 this is 22.5 / 1.25 + 21.4375 / 1.5625
    + (17.9921875 + 161.9296875) / 1.953125 = 18 + 13.72 + 92.12 = 123.84, the fade test's hand value.)"""
    ke, p, roes = D("0.125"), D(payout), [D("0.22"), D("0.19"), D("-0.04"), D("0.17")]
    path = roes + [roes[-1] + (ke - roes[-1]) * k / fade for k in range(1, fade + 1)]
    book, disc, pv = D(250), D(1), D(0)
    for roe in path:
        earnings = roe * book
        dividend = p * max(earnings, D(0))
        disc *= 1 + ke
        pv += dividend / disc
        book += earnings - dividend
    got = V.residual_income(250, ["0.22", "0.19", "-0.04", "0.17"], "0.125", payout, fade_years=fade)
    assert abs(got.value - (pv + book / disc)) < D("1e-18") and got.closing_book == book
    assert len(got.roe) == 4 + fade and got.roe_path == roes


def test_residual_income_refuses_invalid_input():
    ok = {"book_value": 100, "roe_path": ["0.18"], "cost_of_equity": "0.13", "payout_ratio": "0.5"}
    for bad, why in (
        ({}, "one per call"),  # neither ending
        ({"terminal_growth": "0.05", "fade_years": 3}, "one per call"),
        ({"terminal_growth": "0.13"}, "cost_of_equity must exceed terminal_growth"),
        ({"terminal_growth": "0.05", "book_value": 0}, "book_value must be > 0"),
        ({"terminal_growth": "0.05", "book_value": -40}, "book_value must be > 0"),
        ({"terminal_growth": "0.05", "roe_path": []}, "non-empty list"),
        ({"terminal_growth": "0.05", "roe_path": ["18"]}, "must be a fraction"),
        ({"terminal_growth": "0.05", "payout_ratio": "1.2"}, "payout_ratio must be between 0 and 1"),
        ({"terminal_growth": "0.05", "payout_ratio": "-0.1"}, "payout_ratio must be between 0 and 1"),
        ({"fade_years": -1}, "fade_years must be a whole number >= 0"),
        ({"fade_years": 2.5}, "fade_years must be a whole number >= 0"),
        ({"terminal_growth": "0.05", "cost_of_equity": "13"}, "must be a fraction"),
    ):
        with pytest.raises(ValueError, match=why):
            V.residual_income(**{**ok, **bad})


def test_ddm_single_and_two_stage_by_hand():
    """Single stage (Gordon 1962): 10 x 1.05 / (0.25 - 0.05) = 52.5.
    Two stages: 50 % growth for 2 years, then 5 %, at 25 %. D1 = 15, D2 = 22.5; PV = 15 / 1.25 + 22.5 / 1.5625
    = 12 + 14.4 = 26.4; terminal value 22.5 x 1.05 / 0.20 = 118.125, PV 75.6; value 102, terminal share 75.6 / 102."""
    one = V.ddm(10, "0.05", "0.25")
    assert (one.value, one.next_dividend, one.stages) == (D("52.5"), D("10.5"), 1)
    assert one.pv_high_growth is None and one.terminal_share is None
    two = V.ddm(10, "0.05", "0.25", high_growth="0.50", high_growth_years=2)
    assert (two.pv_high_growth, two.pv_terminal, two.value, two.stages) == (D("26.4"), D("75.6"), D(102), 2)
    assert two.next_dividend == D(15) and two.terminal_share.quantize(D("0.0001")) == D("0.7412")
    # closed form: a "high" stage that grows at the terminal rate is the single-stage model
    same = V.ddm(10, "0.05", "0.25", high_growth="0.05", high_growth_years=6)
    assert abs(same.value - D("52.5")) < TINY


def test_ddm_refuses_no_dividend_and_other_invalid_input():
    """A company that pays nothing has no dividend-discount value: refused with the reason, never valued at 0."""
    with pytest.raises(ValueError, match="pays no dividend"):
        V.ddm(0, "0.05", "0.13")
    with pytest.raises(ValueError, match="dividend must be > 0"):
        V.ddm(-2, "0.05", "0.13")
    with pytest.raises(ValueError, match="cost_of_equity must exceed growth"):
        V.ddm(10, "0.13", "0.13")
    with pytest.raises(ValueError, match="together"):
        V.ddm(10, "0.05", "0.13", high_growth="0.2")
    with pytest.raises(ValueError, match="together"):
        V.ddm(10, "0.05", "0.13", high_growth_years=3)
    for bad in ({"high_growth_years": 0}, {"high_growth_years": 2.5}, {"high_growth": "20"}):
        with pytest.raises(ValueError):
            V.ddm(10, "0.05", "0.13", **{"high_growth": "0.2", "high_growth_years": 3, **bad})


# --------------------------------------------------------------------------- peer range
def test_peer_stats_carry_min_and_max_of_the_values_counted():
    """Six positive multiples (the -5 of a loss-maker is dropped, as before): the lowest is 26.29 and the highest
    54.85; with negatives kept (peer_distribution, e.g. ROE) the minimum is the -5 itself."""
    s = V.peer_stats([54.85, 46.69, 42.69, 48.99, 26.29, 33.47, -5], own="51.61")
    assert (s.n, s.min, s.max) == (6, D("26.29"), D("54.85"))
    assert s.min <= s.q1 <= s.median <= s.q3 <= s.max
    kept = V.peer_distribution([54.85, 46.69, 42.69, 48.99, 26.29, 33.47, -5])
    assert (kept.n, kept.min, kept.max) == (7, D(-5), D("54.85"))

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
    assert v.implied_terminal_growth is None and "exit-multiple method" in v.unknown["implied_terminal_growth"]


def test_dcf_explicit_exit_multiple_chosen_to_equal_the_growth_method():
    """Independent check: the exit multiple the growth method implies (3.5x of 150, above) must reproduce the same
    value, and the growth it implies must be the 5 % that built it: (525 x 0.25 - 100) / (525 + 100) = 0.05."""
    v = V.dcf_explicit([-50, 100], "0.25", exit_multiple="3.5", terminal_metric=150)
    assert (v.terminal_value, v.pv_terminal, v.value, v.terminal_method) == (D(525), D(336), D(360), "exit_multiple")
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
    new = V.dcf_explicit([D(100) * D("1.08") ** t for t in range(1, years + 1)], "0.12", terminal_growth="0.04")
    assert abs(new.value - old.value) < TINY and abs(new.pv_terminal - old.pv_terminal) < TINY


def test_dcf_explicit_mid_year_convention_by_hand():
    """Rate 21 % so that sqrt(1.21) = 1.1 exactly. Flows 110 and 133.1 arrive at t = 0.5 and 1.5:
    PV1 = 110 x 1.1 / 1.21 = 100; PV2 = 133.1 x 1.1 / 1.4641 = 100. Growth 10 %: TV = 133.1 x 1.1 / 0.11 = 1,331,
    discounted 1.5 years (its flows are mid-year too): 1,331 x 1.1 / 1.4641 = 1,000. Value 1,200, share 0.8333.
    Year-end discounting of the same numbers gives 1,200 / 1.1 = 1,090.909..."""
    v = V.dcf_explicit(["110", "133.1"], "0.21", terminal_growth="0.10", terminal_metric="266.2", mid_year=True)
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
    assert v.implied_exit_multiple is None and "terminal_metric was not given" in v.unknown["implied_exit_multiple"]
    neg = V.dcf_explicit([100, 110], "0.25", terminal_growth="0.05", terminal_metric=-3)
    assert neg.implied_exit_multiple is None and "not positive" in neg.unknown["implied_exit_multiple"]
    x = V.dcf_explicit([100, -10], "0.25", exit_multiple=4, terminal_metric=50)
    assert x.implied_terminal_growth is None and "final year's cash flow" in x.unknown["implied_terminal_growth"]
    assert x.terminal_value == D(200) and x.pv_terminal == D(128)  # 4 x 50 = 200; 200 / 1.5625 = 128

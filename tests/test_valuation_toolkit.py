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

"""Risk tab views (#349 rolling and benchmark-relative statistics, #423 stress scenarios that never show an unknown
move as 0 %). Synthetic series only; expected values are computed independently (numpy or by hand)."""

from __future__ import annotations

import math
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from finresearch.portfolio.history import Position


# --------------------------------------------------------------------------- #423 stress scenarios
def _pos(key, tax_class, value=1000.0):
    return Position(key, key, "stock" if tax_class == "equity" else "mf", tax_class, [1], 10, 10, None, value, None,
                    key.split(":")[1])  # fmt: skip


def _fall_history():
    """NIFTYBEES and a stock whose daily returns are exactly 2x the index's (beta 2), 200 days to `end`."""
    end = date(2026, 10, 20)
    days = [end - timedelta(days=199 - i) for i in range(200)]
    bench, own = {days[0]: 100.0}, {days[0]: 50.0}
    for i in range(1, 200):
        rb = 0.01 * math.sin(i * 0.7)
        bench[days[i]] = bench[days[i - 1]] * (1 + rb)
        own[days[i]] = own[days[i - 1]] * (1 + 2 * rb)
    short = {d: 20.0 + i for i, d in enumerate(days[-10:])}  # 9 returns: too short for a beta
    return SimpleNamespace(
        days=days, benchmark=SimpleNamespace(closes=bench, factors={}),
        positions=[_pos("NSE:LONG", "equity"), _pos("NSE:NEWIPO", "equity"), _pos("MF:7", "debt_mf")],
        closes={"NSE:LONG": own, "NSE:NEWIPO": short}, factors={},
    )  # fmt: skip


def test_hypothetical_fall_leaves_short_history_and_non_equity_unmapped_by_default_423():
    from finresearch.portfolio.analytics import hypothetical_fall

    res = hypothetical_fall(_fall_history())
    rows = {x["key"]: x for x in res["positions"]}
    assert rows["NSE:LONG"]["move"] == pytest.approx(2 * -0.20, abs=1e-4)
    for k in ("NSE:NEWIPO", "MF:7"):  # unknown, never 0 % and never an unasked-for beta of 1
        assert rows[k]["move"] is None and rows[k]["inr"] is None and rows[k]["how"].startswith("unmapped")
    assert res["mapped_value"] == 1000 and res["unmapped_value"] == 2000
    assert res["portfolio_move"] == pytest.approx(-0.40, abs=1e-4) and res["inr"] == pytest.approx(
        -400, abs=1
    )
    assert res["assume_beta1"] is False


def test_hypothetical_fall_beta_one_is_a_labelled_option_for_short_history_equity_only_423():
    from finresearch.portfolio.analytics import hypothetical_fall

    res = hypothetical_fall(_fall_history(), assume_beta1=True)
    rows = {x["key"]: x for x in res["positions"]}
    assert rows["NSE:NEWIPO"]["move"] == pytest.approx(-0.20) and "assumed" in rows["NSE:NEWIPO"]["how"]
    assert rows["NSE:NEWIPO"]["assumed"] is True and rows["NSE:LONG"].get("assumed") is False
    assert rows["MF:7"]["move"] is None  # the option never reaches a debt fund
    assert res["mapped_value"] == 2000 and res["unmapped_value"] == 1000
    assert res["portfolio_move"] == pytest.approx((-400 - 200) / 2000, abs=1e-4)
    assert res["assume_beta1"] is True


def test_legacy_2008_scenario_is_relabelled_as_an_equity_only_proxy_423():
    from finresearch.portfolio.analytics import SCENARIOS

    gfc = next(s for s in SCENARIOS if s.id == "gfc2008")
    assert "equity only" in gfc.label.lower() and "unmapped" in gfc.note and "0 %" not in gfc.note

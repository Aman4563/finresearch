"""Stock peer table (#178): metrics, peer selection and summary on synthetic data. Expected values are worked by hand
in the comments, not copied from the code's output."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from finresearch.fincalc import peers as P

ENDS = [date(2024, 9, 30), date(2024, 12, 31), date(2025, 3, 31), date(2025, 6, 30),
        date(2025, 9, 30), date(2025, 12, 31), date(2026, 3, 31), date(2026, 6, 30)]  # fmt: skip
REV = [100, 100, 100, 100, 110, 110, 120, 120]
PAT = [10, 10, 10, 10, 12, 12, 14, 14]
EPS = [1, 1, 1, 1, 5, 6, 7, 8]


def quarters(eps=EPS, consolidated=True, drop=()):
    return [P.Quarter(e, Decimal(r), Decimal(p), Decimal(x), consolidated, f"https://x/{e}.xml")
            for i, (e, r, p, x) in enumerate(zip(ENDS, REV, PAT, eps, strict=True)) if i not in drop]  # fmt: skip


SHEETS = [P.BalanceSheet(date(2025, 3, 31), Decimal(400), Decimal(450), True, "bs25"),
          P.BalanceSheet(date(2026, 3, 31), Decimal(480), Decimal(530), True, "bs26")]  # fmt: skip


def v(m):
    return None if m.value is None else float(m.value)


def test_ttm_eps_and_pe():
    eps, pe = P.pe_metric(Decimal(520), quarters())
    assert v(eps) == 26  # 5 + 6 + 7 + 8
    assert v(pe) == 20  # 520 / 26
    assert "30 Jun 2026" in pe.period and "consolidated" in eps.period


def test_negative_ttm_eps_is_not_meaningful_never_zero():
    eps, pe = P.pe_metric(Decimal(520), quarters(eps=[1, 1, 1, 1, 5, 6, 7, -30]))
    assert v(eps) == -12  # 5 + 6 + 7 - 30
    assert pe.value is None and pe.reason.startswith("n/m")


def test_missing_eps_and_gaps_give_reasons():
    _, pe = P.pe_metric(Decimal(520), quarters(drop=(5,)))  # Dec-2025 missing inside the window
    assert pe.value is None and "not consecutive" in pe.reason
    _, pe = P.pe_metric(Decimal(520), quarters()[:3])
    assert pe.value is None and "fewer than four" in pe.reason
    eps = list(EPS)
    eps[7] = None
    _, pe = P.pe_metric(Decimal(520), [P.Quarter(q.end, q.revenue, q.profit, e and Decimal(e), True)
                                        for q, e in zip(quarters(), eps, strict=True)])  # fmt: skip
    assert pe.value is None and "no EPS" in pe.reason


def test_basis_is_the_latest_quarters_and_never_mixed():
    qs = quarters()[:-1] + [P.Quarter(ENDS[-1], Decimal(1), Decimal(1), Decimal(1), False)]
    _, pe = P.pe_metric(Decimal(520), qs)  # latest is standalone: only one standalone quarter on file
    assert pe.value is None and "standalone" in pe.reason


def test_pb_roe_growth_margin():
    qs = quarters()
    assert v(P.pb_metric(Decimal(5200), SHEETS, True)) == pytest.approx(5200 / 480)
    roe = P.roe_metric(qs, SHEETS, True)  # TTM to Mar-2026: 10 + 12 + 12 + 14 = 48; average equity (400 + 480) / 2
    assert v(roe) == pytest.approx(48 / 440)
    assert v(P.growth_metric(qs, "revenue")) == pytest.approx(460 / 400 - 1)  # 110+110+120+120 vs 4 x 100
    assert v(P.growth_metric(qs, "profit")) == pytest.approx(52 / 40 - 1)  # 12+12+14+14 vs 4 x 10
    assert v(P.margin_metric(qs)) == pytest.approx(52 / 460)


def test_roe_needs_opening_equity_and_pb_needs_positive_book():
    assert P.roe_metric(quarters(), SHEETS[1:], True).value is None
    neg = [P.BalanceSheet(date(2026, 3, 31), Decimal(-5), None, True)]
    assert P.pb_metric(Decimal(5200), neg, True).reason.startswith("n/m")


def test_growth_off_a_loss_is_not_meaningful():
    qs = [P.Quarter(q.end, q.revenue, Decimal(-5) if i < 4 else q.profit, q.eps, True)
          for i, q in enumerate(quarters())]  # fmt: skip
    m = P.growth_metric(qs, "profit")
    assert m.value is None and m.reason.startswith("n/m")


def test_one_year_return_adjusts_for_a_bonus():
    bars = [(date(2025, 10, 3), 200), (date(2025, 10, 6), 205)]  # 5-Oct-2025 was a Sunday: Friday's close counts
    m = P.return_1y(Decimal(120), date(2026, 10, 5), bars, [(date(2026, 1, 15), "Bonus 1:1")])
    assert v(m) == pytest.approx(120 / (200 / 2) - 1)  # 1:1 bonus halves the old close: 0.20
    assert "x2" in m.basis
    assert P.return_1y(Decimal(120), date(2026, 10, 5), [], []).value is None


def _row(sym, **vals):
    return {"symbol": sym, **{k: {"value": x} for k, x in vals.items()}}


def test_summary_quartiles_and_percentile_skip_missing():
    peers = [_row("A", pe=10), _row("B", pe=20), _row("C", pe=30), _row("D", pe=40), _row("E", pe=None)]
    s = P.summarise({"pe": {"value": 25}}, peers)["pe"]
    # 10, 20, 30, 40: Q1 at position 0.75 -> 17.5, median 25, Q3 at 2.25 -> 32.5; 2 of 4 below 25 -> 50
    assert (s["n"], s["q1"], s["median"], s["q3"], s["percentile"]) == (4, 17.5, 25, 32.5, 50)
    assert P.summarise({"pe": {"value": 20}}, peers)["pe"]["percentile"] == 37.5  # (1 below + 1 tie / 2) / 4
    few = P.summarise({}, peers[:2])["roe"]
    assert few["median"] is None and few["reason"]


def test_summary_keeps_negative_growth_but_drops_non_positive_multiples():
    peers = [_row("A", pe=-5, roe=-0.1), _row("B", pe=10, roe=0.1), _row("C", pe=20, roe=0.2), _row("D", pe=30, roe=0.3)]
    s = P.summarise({}, peers)
    assert s["pe"]["n"] == 3 and s["pe"]["median"] == 20
    assert s["roe"]["n"] == 4 and s["roe"]["median"] == pytest.approx(0.15)


def test_finest_level_with_five_peers_and_fallback():
    own = {"basic_industry": "Cement", "industry": "Cement & Products", "sector": "Construction Materials",
           "macro": "Commodities"}  # fmt: skip
    uni = [{"symbol": f"S{i}", "basic_industry": "Cement" if i < 3 else "Other", "industry": "Cement & Products",
            "sector": "Construction Materials", "macro": "Commodities"} for i in range(7)]  # fmt: skip
    lvl, peers = P.choose_level(own, [*uni, {"symbol": "ME", **own}], "ME")
    assert lvl == "industry" and len(peers) == 7  # 3 cement peers < 5, so one level coarser; the company excluded
    lvl, peers = P.choose_level(own, uni[:2], "ME")
    assert lvl == "basic_industry" and len(peers) == 2  # nothing reaches 5: the level with the most peers
    assert P.choose_level({}, uni, "ME") == (None, [])


def test_nearest_by_market_cap_on_a_log_scale():
    peers = [{"symbol": s, "market_cap": m} for s, m in (("A", 50), ("B", 200), ("C", 400), ("D", 90), ("E", None))]
    got = [p["symbol"] for p in P.nearest_by_mcap(peers, 100, cap=4)]
    assert got == ["D", "A", "B", "C"]  # |ln .9| < |ln .5| = |ln 2| (tie: by symbol) < ln 4; no market cap last

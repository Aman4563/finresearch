"""The historical scan, ES97.5, episodes, gap, expiry-day and liquidity blocks of the F&O book's scenario view (#394,
fnobook.scenarios.view). Synthetic chain, paper legs and stored history (the loader is replaced); no network."""

from __future__ import annotations

import math
from datetime import date, timedelta

import pytest
from test_book_scenarios_api import (  # noqa: F401
    NOW,
    VOLS,
    F,
    R,
    Sources,
    T,
    _b76,
    _paper_spread,
    chain,
    groww_market,
)
from test_fno_book import FNO_ROWS, _store_groww_positions, book_db, offline, run  # noqa: F401

from finresearch.fincalc import book_history as bh

pytestmark = pytest.mark.usefixtures("book_db")
T1 = 14 / 365  # Mon 12-Oct 11:00 -> Tue 13-Oct 15:30, 14 days before 27-Oct 15:30
RETS = [0.0] + [0.001 * ((i * 37) % 61 - 30) for i in range(1, 301)]


def _hist() -> bh.Series:
    cal, d = [], date(2024, 1, 1)
    while len(cal) < len(RETS):
        if d.weekday() < 5:
            cal.append(d)
        d += timedelta(days=1)
    return bh.Series({cal[i]: (cal[i - 1], RETS[i]) for i in range(1, len(RETS))}, dict.fromkeys(cal, 15.0))


@pytest.fixture
def stored(monkeypatch):
    from finresearch.fnohist import book_history as lh

    seen = []

    def load(s, underlyings, today):
        seen.append((sorted(underlyings), today))
        return lh.History({"NIFTY": _hist()}, [0.0005 * ((i * 13) % 41 - 20) for i in range(300)],
                          [0.001 * ((i * 7) % 61 - 30) for i in range(100)], (date(2024, 1, 1), date(2025, 2, 23)))  # fmt: skip

    monkeypatch.setattr(lh, "load", load)
    return seen


def _spread_pnl(r: float) -> float:
    """The 25000/25200 call spread one session ahead with the forward moved by e^r and rolled, IVs unchanged."""
    f1 = F * math.exp(r) * math.exp(-R * (T - T1))
    return sum(q * (_b76("call", f1, k, T1, VOLS[k]) - _b76("call", F, k, T, VOLS[k]))
               for k, q in ((25000, 75), (25200, -75)))  # fmt: skip


def test_the_view_shows_the_scan_and_its_historical_es(stored):
    from finresearch.fnobook import scenarios

    _paper_spread()
    v = run(scenarios.view(NOW, sources=Sources(chain()), capital=500_000))
    assert stored == [(["NIFTY"], date(2026, 10, 12))]
    h1 = next(x for x in v["history"]["scan"] if x["h"] == 1)
    pnl = sorted(_spread_pnl(r) for r in RETS[1:])
    assert h1["n"] == 300 and h1["skipped"] == 0 and h1["reasons"] == []
    assert h1["es"] == pytest.approx(-(sum(pnl[:7]) + 0.5 * pnl[7]) / 7.5, abs=0.02)
    assert h1["loss_99"] == pytest.approx(-pnl[2], abs=0.02)
    assert [w["pnl"] for w in h1["worst"]] == pytest.approx(pnl[:10], abs=0.02)
    es = v["history"]["es"]
    assert es["value"] == pytest.approx(h1["es"]) and es["h"] == 1 and es["limit"] == pytest.approx(40_000)
    assert "not a model ES" in es["note"]
    assert {e["key"] for e in v["history"]["episodes"]} >= {"febmar_2026", "oct_2008"}
    assert len(v["history"]["gaps"]) == 4 and all(g["pnl"] is not None for g in v["history"]["gaps"])
    assert len(v["history"]["expiry_day"]) == 4


def test_liquidity_stress_is_unknown_and_today_s_quoted_exit_cost_is_shown(stored):
    from finresearch.fnobook import scenarios

    _paper_spread()
    v = run(scenarios.view(NOW, sources=Sources(chain()), capital=500_000))
    liq = v["liquidity"]
    assert liq["stressed"] is None and "P1.1" in liq["reason"]
    assert liq["quoted_exit_cost"] == pytest.approx(
        2 * 75 * 0.5, abs=1e-6
    )  # |q| x half-spread, each leg ±0.5


def test_a_leg_without_a_two_sided_quote_leaves_the_quoted_exit_cost_unknown(stored):
    from finresearch.fnobook import scenarios

    _paper_spread()
    v = run(scenarios.view(NOW, sources=Sources(chain(ltp_only={("call", 25200)})), capital=500_000))
    assert v["liquidity"]["quoted_exit_cost"] is None
    assert any("25200" in r for r in v["liquidity"]["reasons"])


def test_unresolved_groww_positions_make_every_history_figure_unknown(stored):
    from finresearch.fnobook import scenarios

    _paper_spread()
    _store_groww_positions(FNO_ROWS, NOW)  # one row's long/short is not confirmed
    v = run(scenarios.view(NOW, sources=Sources(chain()), capital=500_000))
    hist = v["history"]
    assert all(x["es"] is None and x["n"] == 0 and x["reasons"] for x in hist["scan"])
    assert hist["es"]["value"] is None and hist["es"]["reasons"]
    assert all(e["worst"] is None for e in hist["episodes"])
    assert all(g["pnl"] is None for g in hist["gaps"] + hist["expiry_day"])
    assert v["liquidity"]["quoted_exit_cost"] is None


def test_an_unreadable_store_leaves_the_history_unknown_with_the_reason(monkeypatch):
    from finresearch.fnobook import scenarios
    from finresearch.fnohist import book_history as lh

    def boom(s, underlyings, today):
        raise RuntimeError("synthetic")

    monkeypatch.setattr(lh, "load", boom)
    _paper_spread()
    v = run(scenarios.view(NOW, sources=Sources(chain()), capital=500_000))
    assert v["history"]["es"]["value"] is None
    assert any("could not be read" in r for r in v["history"]["es"]["reasons"])

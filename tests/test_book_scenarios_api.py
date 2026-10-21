"""The F&O book's scenario view (#394, fnobook.scenarios and GET /api/fno/scenarios): today's paper and Groww legs
priced on a synthetic option chain, then exposures, the revaluation grid, replays and reverse stress. Synthetic
contracts and prices only; no network."""

from __future__ import annotations

import math
from decimal import Decimal as D
from statistics import NormalDist

import pytest
from test_fno_book import (  # noqa: F401
    EXPIRY,
    FNO_ROWS,
    TRADE_DAY,
    FakeMarket,
    _record_spread,
    _store_groww_positions,
    at_ist,
    book_db,
    offline,
    run,
)

N = NormalDist()
pytestmark = pytest.mark.usefixtures("book_db")
NOW = at_ist(TRADE_DAY, 11)  # 12-Oct-2026 11:00 IST
T = (15 * 1440 + 270) / 525_600  # to 27-Oct 15:30 IST, to the minute (#359)
R = 0.065
F = 24980.0  # the synthetic forward the chain's prices are built on


def _b76(right, f, k, t, v):
    d1 = (math.log(f / k) + v * v * t / 2) / (v * math.sqrt(t))
    c = math.exp(-R * t) * (f * N.cdf(d1) - k * N.cdf(d1 - v * math.sqrt(t)))
    return c if right == "call" else c - math.exp(-R * t) * (f - k)


VOLS = {25000: 0.14, 25200: 0.135}


def chain(drop=(), ltp_only=()):
    """A chain whose mids are Black-76 prices on F at VOLS (bid/ask ±0.5 around them); `drop` removes quotes and
    `ltp_only` leaves a quote with a last price but no bid/ask."""
    from finresearch.adapters.nse_fno import ChainRow, OptionChain, OptionQuote

    def q(right, k):
        if (right, k) in drop:
            return None
        mid = _b76(right, F, k, T, VOLS[k])
        two = (right, k) not in ltp_only
        return OptionQuote(last_price=D(str(round(mid, 2))), iv=D("99"), oi=None, change_in_oi=None, volume=None,
                           bid=D(str(mid - 0.5)) if two else None, ask=D(str(mid + 0.5)) if two else None)  # fmt: skip

    return OptionChain(symbol="NIFTY", expiry=EXPIRY, underlying=D("24900"), as_of=None,
                       rows=[ChainRow(strike=D(k), call=q("call", k), put=q("put", k)) for k in VOLS])  # fmt: skip


@pytest.fixture(autouse=True)
def groww_market(monkeypatch):
    from finresearch.adapters import groww_market

    monkeypatch.setattr(groww_market, "MARKET", FakeMarket())


class Sources:
    def __init__(self, ch):
        self.ch, self.calls = ch, []

    async def chain(self, symbol, expiry):
        self.calls.append((symbol, expiry))
        if self.ch is None:
            raise RuntimeError("no chain")
        return self.ch


def _paper_spread():
    from finresearch.db import session_scope

    with session_scope() as s:
        _record_spread(s, NOW)  # long 75 x 25000 CE, short 75 x 25200 CE, expiry 27-Oct-2026


def test_paper_legs_are_priced_at_mids_on_the_parity_forward():
    from finresearch.fnobook import scenarios

    _paper_spread()
    v = run(scenarios.view(NOW, sources=Sources(chain()), capital=500_000))
    (fw,) = v["forwards"]
    assert (
        fw["forward"] == pytest.approx(F, abs=1e-6) and "parity" in fw["source"]
    )  # mids reproduce F exactly
    legs = {x["strike"]: x for x in v["legs"]}
    assert legs[25000]["mark_kind"] == "mid" and legs[25000]["iv"] == pytest.approx(0.14, abs=1e-6)
    assert legs[25200]["qty"] == -75 and legs[25200]["iv"] == pytest.approx(0.135, abs=1e-6)
    assert v["exposures"]["complete"] and v["grid"]["complete"]
    assert len(v["grid"]["cells"]) == 14 * 5 * 3
    assert {r["horizon"] for r in v["reverse"]["rows"]} == {"1d", "5d", "expiry"}
    assert v["reverse"]["limit"] == pytest.approx(40_000)  # 8 % of 5 lakh (#358)
    assert any("Groww is not connected" in n for n in v["notes"])
    # a debit spread loses at most its debit: no grid cell loses more than (C25000 - C25200) x 75
    debit = (_b76("call", F, 25000, T, 0.14) - _b76("call", F, 25200, T, 0.135)) * 75
    assert min(c["pnl"] for c in v["grid"]["cells"]) >= -debit - 1e-6


def test_a_missing_quote_makes_the_book_incomplete_never_zero():
    from finresearch.fnobook import scenarios

    _paper_spread()
    v = run(scenarios.view(NOW, sources=Sources(chain(drop={("call", 25200)})), capital=500_000))
    assert not v["exposures"]["complete"] and v["exposures"]["total"] is None
    assert not v["grid"]["complete"] and all(c["pnl"] is None for c in v["grid"]["cells"])
    assert any("25200" in r for r in v["grid"]["reasons"])
    assert all(r["cell"] is None and not r["complete"] for r in v["reverse"]["rows"])
    assert all(r["pnl"] is None for r in v["replays"])


def test_an_ltp_only_quote_is_flagged_and_no_chain_means_no_forward():
    from finresearch.fnobook import scenarios

    _paper_spread()
    v = run(scenarios.view(NOW, sources=Sources(chain(ltp_only={("call", 25200)})),
                           capital=500_000))  # fmt: skip
    assert {x["strike"]: x["mark_kind"] for x in v["legs"]} == {25000: "mid", 25200: "ltp"}
    assert any("LTP" in f for f in v["exposures"]["flags"])
    v = run(scenarios.view(NOW, sources=Sources(None), capital=500_000))
    assert not v["grid"]["complete"] and any("forward" in r for r in v["grid"]["reasons"])


def test_no_capital_means_no_book_limit():
    from finresearch.fnobook import scenarios

    _paper_spread()
    v = run(scenarios.view(NOW, sources=Sources(chain()), capital=0))
    assert v["reverse"]["limit"] is None and "capital" in v["reverse"]["reason"]
    assert v["reverse"]["rows"] == []


def test_an_empty_book_is_complete_and_flat():
    from finresearch.fnobook import scenarios

    src = Sources(chain())
    v = run(scenarios.view(NOW, sources=src, capital=500_000))
    assert v["legs"] == [] and v["grid"]["complete"] and all(c["pnl"] == 0 for c in v["grid"]["cells"])
    assert src.calls == []  # nothing open: no chain is read


def test_groww_legs_join_the_book():
    from finresearch.fnobook import scenarios

    _store_groww_positions([FNO_ROWS[0]], NOW)  # long 75 x 25000 CE, direction confirmed
    v = run(scenarios.view(NOW, sources=Sources(chain()), capital=500_000))
    (leg,) = v["legs"]
    assert (leg["source"], leg["qty"], leg["strike"], leg["right"]) == ("groww", 75, 25000, "call")
    assert v["grid"]["complete"] and v["exposures"]["complete"]


def test_unresolved_groww_positions_make_every_figure_unknown():
    from finresearch.fnobook import scenarios

    _paper_spread()
    _store_groww_positions(FNO_ROWS, NOW)  # one row's long/short is not confirmed
    v = run(scenarios.view(NOW, sources=Sources(chain()), capital=500_000))
    assert not v["grid"]["complete"] and all(c["pnl"] is None for c in v["grid"]["cells"])
    assert v["exposures"]["total"] is None and not v["exposures"]["complete"]
    assert all(r["pnl"] is None for r in v["replays"]) and all(
        not r["complete"] for r in v["reverse"]["rows"]
    )
    assert any("could not be resolved" in r for r in v["grid"]["reasons"])


def test_api_route_reads_the_profile_capital_and_writes_nothing(monkeypatch):
    from sqlalchemy import func, select
    from starlette.testclient import TestClient

    from finresearch.api import create_app
    from finresearch.db import session_scope
    from finresearch.db.models import InvestorProfile, PaperStrategy
    from finresearch.fnobook import sources

    _paper_spread()
    with session_scope() as s:
        assert s.scalar(select(func.count()).select_from(InvestorProfile)) == 0
        before = [(p.mtm_gross, p.mtm_at) for p in s.scalars(select(PaperStrategy))]
    monkeypatch.setattr(sources, "SOURCES", Sources(chain()))
    c = TestClient(create_app(clock=lambda: NOW))
    r = c.get("/api/fno/scenarios")
    assert r.status_code == 200
    v = r.json()
    assert v["grid"]["complete"] and len(v["legs"]) == 2
    assert (
        v["reverse"]["limit"] is None and "capital" in v["reverse"]["reason"]
    )  # the default profile: none set
    with session_scope() as s:  # the GET wrote nothing: no profile row, the paper trade's mark untouched
        assert s.scalar(select(func.count()).select_from(InvestorProfile)) == 0
        assert [(p.mtm_gross, p.mtm_at) for p in s.scalars(select(PaperStrategy))] == before


def test_a_groww_future_shows_no_strike(monkeypatch):
    from finresearch.fincalc import options as o
    from finresearch.fnobook import scenarios
    from finresearch.signals import tradeplan

    async def real(today):  # real_positions carries a future's average price in Leg.strike
        return [tradeplan.RealPosition("NIFTY", EXPIRY, [o.Leg("future", 24950.0, 0.0, -75)])], None

    monkeypatch.setattr(tradeplan, "real_positions", real)
    legs, _ = run(scenarios.groww_legs(TRADE_DAY))
    assert [(x.right, x.strike, x.qty) for x in legs] == [("future", 0.0, -75)]

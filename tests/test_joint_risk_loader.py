"""The joint risk view's loader (#397, portfolio.joint_risk): holding betas from the value history, the open F&O
book priced on a synthetic chain, the holdings' single-day replay moves and the forward test's day count. Synthetic
holdings, legs and prices only; no network."""

from __future__ import annotations

import random
from datetime import date, timedelta
from decimal import Decimal as D
from types import SimpleNamespace

import pytest
from test_book_scenarios_api import (  # noqa: F401
    NOW,
    Sources,
    _paper_spread,
    book_db,
    chain,
    groww_market,
    offline,
    run,
)

from finresearch.portfolio import joint_risk as jl


def _days(n: int, end: date) -> list[date]:
    out, d = [], end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return out[::-1]


def _history(n: int = 260):
    rnd = random.Random(3)
    days = _days(n, date(2026, 10, 9))
    bench, eq = [100.0], [50.0]
    for _ in range(n - 1):
        m = rnd.gauss(0.0004, 0.01)
        bench.append(bench[-1] * (1 + m))
        eq.append(eq[-1] * (1 + 1.4 * m + rnd.gauss(0, 0.006)))
    pos = [SimpleNamespace(key="NSE:FAKEEQ", name="Fake Equity Ltd", value=100_000.0, tax_class="equity"),
           SimpleNamespace(key="NSE:NEWEQ", name="Fresh Listing Ltd", value=50_000.0, tax_class="equity"),
           SimpleNamespace(key="MF:999", name="Fake Debt Fund", value=20_000.0, tax_class="debt")]  # fmt: skip
    closes = {"NSE:FAKEEQ": dict(zip(days, eq, strict=True)),
              "NSE:NEWEQ": dict(zip(days[-30:], eq[-30:], strict=True)),
              "MF:999": dict(zip(days[-30:], [10.0 + i / 1000 for i in range(30)], strict=True))}  # fmt: skip
    b = SimpleNamespace(closes=dict(zip(days, bench, strict=True)), factors=None)
    return SimpleNamespace(positions=pos, closes=closes, factors={}, benchmark=b, days=days), days, bench, eq


def test_holding_betas_are_each_holdings_1_year_beta_and_unknown_when_short():
    import numpy as np

    h, days, bench, eq = _history()
    since = days[-1] - timedelta(days=365)
    keep = [i for i in range(1, len(days)) if days[i] > since]
    rb = np.array([bench[i] / bench[i - 1] - 1 for i in keep])
    ra = np.array([eq[i] / eq[i - 1] - 1 for i in keep])
    want = np.cov(ra, rb, ddof=1)[0, 1] / np.var(rb, ddof=1)
    by = {x.key: x for x in jl.holdings(h)}
    assert by["NSE:FAKEEQ"].beta == pytest.approx(want, rel=1e-9)
    assert by["NSE:NEWEQ"].beta is None and "needs 120" in by["NSE:NEWEQ"].how
    assert by["MF:999"].beta is None
    opt = {x.key: x for x in jl.holdings(h, assume_beta1=True)}
    assert opt["NSE:NEWEQ"].beta == 1.0 and "assumed" in opt["NSE:NEWEQ"].how
    assert opt["MF:999"].beta is None  # beta 1 is assumed for short-history equity only, never for debt


def test_replay_moves_take_the_window_loss_only_when_every_holding_maps():
    whole = {"available": True, "loss": {"inr": -4200.0}, "unmapped": []}
    assert jl.replay_move(whole) == (-4200.0, ())
    part = {"available": True, "loss": {"inr": -4200.0},
            "unmapped": [{"name": "Fake Debt Fund", "value": 20000.0, "reason": "no own prices"}]}  # fmt: skip
    inr, why = jl.replay_move(part)
    assert inr is None and "Fake Debt Fund" in why[0]
    inr, why = jl.replay_move({"available": False, "reason": "no close in the 7 days before", "unmapped": []})
    assert inr is None and "no close" in why[0]


@pytest.mark.usefixtures("book_db")
def test_the_book_is_priced_on_the_chain_and_a_candidate_is_priced_with_it():
    from finresearch.fincalc import book_scenarios as bs

    _paper_spread()
    b, cand, notes = run(jl.book(NOW, sources=Sources(chain())))
    assert b.legs is not None and len(b.legs) == 2 and all(x.mark_kind == "mid" for x in b.legs)
    assert cand == [] and any("Groww is not connected" in n for n in notes)
    extra = bs.BookLeg("NIFTY", b.legs[0].expiry, "call", 25200.0, -75, None, None, "candidate")
    b2, cand2, _ = run(jl.book(NOW, sources=Sources(chain()), candidate=[extra]))
    assert len(b2.legs) == 2 and cand2[0].mark is not None and cand2[0].mark_kind == "mid"


@pytest.mark.usefixtures("book_db")
def test_a_failed_chain_leaves_the_book_legs_unpriced_and_the_joint_figure_unknown():
    from finresearch.fincalc import joint_risk as jr

    _paper_spread()
    b, _, _ = run(jl.book(NOW, sources=Sources(None)))
    d = jr.d_total([], b)
    assert d.total is None and d.reasons


def test_forward_test_counts_marked_sessions_with_a_big_nifty_move(env):
    from finresearch.db import session_scope
    from finresearch.db.models import FnoMark, IndexEod

    days = [date(2026, 10, 12) + timedelta(days=i) for i in range(5)]  # Mon-Fri
    closes = [25000, 25500, 25480, 25000, 25100]  # +2.0 %, -0.08 %, -1.9 %, +0.2 %
    with session_scope() as s:
        s.query(FnoMark).delete()
        s.query(IndexEod).filter(IndexEod.symbol == "NIFTY").delete()
        s.add(IndexEod(day=date(2026, 10, 9), symbol="NIFTY", index_name="Nifty 50", close=D("24900")))
        for d, c in zip(days, closes, strict=True):
            s.add(IndexEod(day=d, symbol="NIFTY", index_name="Nifty 50", close=D(str(c))))
        for d in days[1:4]:  # marks on 13-15 Oct only
            s.add(FnoMark(day=d, book="paper", leg_key="paper_leg:1", symbol="NIFTY", expiry=date(2026, 10, 27),
                          right="put", strike=D(24000), qty=-75))  # fmt: skip
    with session_scope() as s:
        st = jl.forward_status(s, date(2026, 10, 22))
    assert st["qualifying_days"] == 2 and st["needed"] == 20 and st["r2"] is None and st["passed"] is None
    assert "not evaluable yet" in st["status"]


class _Store:
    """A price store that just asks the fetcher (no disk)."""

    async def get(self, key, lo, hi, fn, today):
        return await fn(lo, hi)


def test_one_day_replay_moves_are_each_holdings_own_close_to_close_move():
    from finresearch.fincalc import book_scenarios as bs

    day = bs.REPLAYS[1].day  # 4-Jun-2024 (a Tuesday)
    d0, d2 = day - timedelta(days=1), day + timedelta(days=1)
    closes = {"NSE:AAA": {d0: 100.0, day: 95.0, d2: 97.0}, "NSE:BBB": {d0: 50.0, day: 48.0, d2: 49.0}}
    nifty = {d0: 100.0, day: 94.0, d2: 96.0}

    async def fetch(inst, a, b):
        return {d: v for d, v in closes.get(inst.key, {}).items() if a <= d <= b}

    def index_series(sym, a, b):
        return {d: v for d, v in nifty.items() if a <= d <= b} if sym == "NIFTY" else {}

    pos = [SimpleNamespace(key=k, name=k, value=v, asset_type="stock", tax_class="equity", scheme=None,
                           nse_symbol=k.split(":")[1]) for k, v in (("NSE:AAA", 10_000.0), ("NSE:BBB", 5_000.0))]  # fmt: skip
    h = SimpleNamespace(positions=pos, closes={}, factors={}, benchmark=None, days=[date(2026, 10, 21)])
    got = run(jl.replay_moves(h, fetch, _Store(), date(2026, 10, 22), index_series=index_series))
    assert got[day][0] == pytest.approx(10_000 * (95 / 100 - 1) + 5_000 * (48 / 50 - 1)) and got[day][1] == ()
    other = got[bs.REPLAYS[0].day]  # no closes stored for 24-Feb-2022: unknown with the reason, never 0
    assert other[0] is None and other[1]


@pytest.mark.usefixtures("book_db")
def test_unread_groww_positions_make_the_book_unknown_with_the_reason(monkeypatch):
    from finresearch.fincalc import joint_risk as jr
    from finresearch.fnobook import scenarios as sc

    async def unread(today):
        return None, "Groww is connected but its F&O positions have not been read yet"

    monkeypatch.setattr(sc, "groww_legs", unread)
    _paper_spread()
    b, _, notes = run(jl.book(NOW, sources=Sources(chain())))
    assert b.legs is None and "not been read" in b.reason and notes
    d = jr.d_total([], b)
    assert d.total is None and d.fno is None and any("not been read" in r for r in d.reasons)

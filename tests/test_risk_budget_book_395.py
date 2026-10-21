"""P4.2 (#395) data for the sizing terms: the open book's common-shock P&L priced on its chains (fnobook.scenarios)
and the real book's funds from the last Groww sync (fnobook.service.stored_funds). Synthetic contracts, prices and
funds only; no network."""

from __future__ import annotations

import math
from datetime import timedelta

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
from test_fno_book import EXPIRY, book_db, offline, run  # noqa: F401

pytestmark = pytest.mark.usefixtures("book_db")
SHOCK_MOVES = (-0.03, -0.05, -0.10, 0.05)


def test_the_open_books_shock_pnl_is_priced_on_its_chain_395():
    from finresearch.fnobook import scenarios

    _paper_spread()  # long 75 x 25000 CE, short 75 x 25200 CE
    got, notes = run(scenarios.book_shock(NOW, sources=Sources(chain())))
    assert got is not None, notes
    t1 = 14 / 365  # Mon 12-Oct 11:00 → Tue 13-Oct 15:30, which is 14 days before 27-Oct 15:30
    for m in SHOCK_MOVES:
        f1 = F * (1 + m) * math.exp(-R * (T - t1))
        dv = (-1.04 * m * 100 if m < 0 else -0.76 * m * 100) / 100
        want = sum(q * (_b76("call", f1, k, t1, VOLS[k] + dv) - _b76("call", F, k, T, VOLS[k]))
                   for k, q in ((25000, 75), (25200, -75)))  # fmt: skip
        assert got[m] == pytest.approx(want, rel=1e-3, abs=1.0)


def test_an_empty_book_has_no_shock_loss_395():
    from finresearch.fnobook import scenarios

    assert run(scenarios.book_shock(NOW, sources=Sources(None)))[0] == {m: 0.0 for m in SHOCK_MOVES}


def test_a_book_leg_without_a_chain_makes_the_shock_unknown_395():
    from finresearch.fnobook import scenarios

    _paper_spread()
    got, notes = run(scenarios.book_shock(NOW, sources=Sources(None)))
    assert got is None and notes


def test_unread_groww_positions_make_the_shock_unknown_395():
    from finresearch.db import session_scope
    from finresearch.db.models import BrokerConnection
    from finresearch.fnobook import scenarios

    with session_scope() as s:
        s.add(BrokerConnection(key="groww", enabled=True, status="connected", state={}))
    got, notes = run(scenarios.book_shock(NOW, sources=Sources(chain())))
    assert got is None and any("not been read" in n for n in notes)


# --------------------------------------------------------------------------- the real book's funds
def _groww(funds, synced):
    from finresearch.db import session_scope
    from finresearch.db.models import BrokerConnection

    with session_scope() as s:
        s.add(BrokerConnection(key="groww", enabled=True, status="connected", last_sync_at=synced,
                               state={"funds": funds}))  # fmt: skip


FUNDS = {"cash": "250000.50", "margin_used": "40000", "collateral_available": "120000"}


def test_funds_synced_today_are_read_395():
    from finresearch.fnobook import service

    _groww(FUNDS, NOW - timedelta(hours=1))
    got, note = service.stored_funds(NOW)
    assert got == {"cash": 250000.5, "margin_used": 40000.0, "collateral": 120000.0} and note is None


@pytest.mark.parametrize("funds,synced,why", [
    (FUNDS, NOW - timedelta(days=1), "today"),  # yesterday's sync: today's margin is unknown
    ({**FUNDS, "collateral_available": None}, NOW, "unknown"),
    ({"cash": "1"}, NOW, "unknown"),
    (None, NOW, "unknown"),
])  # fmt: skip
def test_stale_or_partial_funds_are_unknown_395(funds, synced, why):
    from finresearch.fnobook import service

    _groww(funds, synced)
    got, note = service.stored_funds(NOW)
    assert got is None and why in note


def test_no_groww_connection_means_unknown_funds_395():
    from finresearch.fnobook import service

    got, note = service.stored_funds(NOW)
    assert got is None and "not connected" in note


# --------------------------------------------------------------------------- the card's own terms
def _bs(right, s, k, t, r, v):
    from statistics import NormalDist

    n = NormalDist()
    d1 = (math.log(s / k) + (r + v * v / 2) * t) / (v * math.sqrt(t))
    c = s * n.cdf(d1) - k * math.exp(-r * t) * n.cdf(d1 - v * math.sqrt(t))
    return c if right == "call" else c - s + k * math.exp(-r * t)


def test_one_sets_shock_pnl_and_margin_on_the_cards_chain_395():
    from finresearch.fincalc import options as o
    from finresearch.signals import tradeplan as tpl

    s0, k, v = 24900.0, 24000.0, 0.15
    mark = _bs("put", s0, k, T, R, v)  # the card's Black-Scholes price on spot = Black-76 on F = S e^{rT}
    one, notes, im, im_notes = tpl.trade_terms(
        "NIFTY", EXPIRY, s0, [o.Leg("put", k, mark, -75)], NOW, R, True
    )
    assert one is not None, notes
    t1 = 14 / 365
    for m in SHOCK_MOVES:
        s1 = s0 * (
            1 + m
        )  # F' = S e^{rT}(1 + m) e^{−r(T − T1)} = S (1 + m) e^{r T1}: Black-Scholes on S (1 + m) at T1
        dv = (-1.04 * m * 100 if m < 0 else -0.76 * m * 100) / 100
        assert one[m] == pytest.approx((_bs("put", s1, k, t1, R, v + dv) - mark) * -75, rel=1e-4)
    assert im is not None and im > 0, im_notes


def test_a_card_on_an_underlying_without_a_beta_has_an_unknown_shock_395():
    from finresearch.fincalc import options as o
    from finresearch.signals import tradeplan as tpl

    one, notes, _, _ = tpl.trade_terms(
        "BANKNIFTY", EXPIRY, 52000.0, [o.Leg("put", 50000, 200.0, -30)], NOW, R, True
    )
    assert one is None and any("beta" in n for n in notes)


def test_book_for_always_evaluates_both_terms_395():
    from finresearch.signals import tradeplan as tpl

    ob = tpl.book_for(tpl.EMPTY_BOOK, 1_000_000.0, "NIFTY", "put_credit", 24900.0, None)
    assert ob.shock is not None and ob.funds is not None  # never "not evaluated" on a card
    s = __import__("finresearch.fincalc.tradeplan", fromlist=["size"]).size(
        -4940.0, 60.0, 20_000.0, True, book=ob
    )
    assert (
        s.suggested == 0 and s.real_lots == 0
    )  # one set's shock and margin were not given: unknown, no room

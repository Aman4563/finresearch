"""F&O positions, paper trading, exit rules, margin approximation and the event strip (#308). Fixtures only: synthetic
Groww answers in the docs' shapes, made-up contracts and prices; no network."""

from __future__ import annotations

import asyncio
from decimal import Decimal as D

import httpx
import pytest

from finresearch.portfolio.connectors import base

ACCESS = "access-token-synthetic-fno-0000000000"

# docs-sample shapes (https://groww.in/trade-api/docs/curl/portfolio, read 12-Oct-2026); made-up contracts
FNO_ROWS = [
    # long 75 calls: bought 75 today, nothing sold -> direction confirmed
    {"trading_symbol": "NIFTY26OCT25000CE", "credit_quantity": 75, "credit_price": 120, "debit_quantity": 0,
     "debit_price": 0, "carry_forward_credit_quantity": 0, "carry_forward_credit_price": 0,
     "carry_forward_debit_quantity": 0, "carry_forward_debit_price": 0, "exchange": "NSE", "quantity": 75,
     "product": "NRML", "net_price": 120, "realised_pnl": 0},
    # short 75 puts carried forward, quantity negative -> confirmed short
    {"trading_symbol": "NIFTY26OCT24500PE", "credit_quantity": 0, "debit_quantity": 0,
     "carry_forward_credit_quantity": 0, "carry_forward_debit_quantity": 75, "exchange": "NSE", "quantity": -75,
     "product": "NRML", "net_price": "80.5", "realised_pnl": 0},
    # sold 75 but quantity +75: unsigned? -> unknown, never guessed
    {"trading_symbol": "NIFTY26OCT25500CE", "credit_quantity": 0, "debit_quantity": 75,
     "carry_forward_credit_quantity": 0, "carry_forward_debit_quantity": 0, "exchange": "NSE", "quantity": 75,
     "product": "NRML", "net_price": 40, "realised_pnl": 0},
    # no credit/debit fields at all -> unknown
    {"trading_symbol": "NIFTY26OCT24000PE", "exchange": "NSE", "quantity": 75, "product": "NRML", "net_price": 30},
    # closed today: dropped
    {"trading_symbol": "NIFTY26OCT24800PE", "credit_quantity": 75, "debit_quantity": 75,
     "carry_forward_credit_quantity": 0, "carry_forward_debit_quantity": 0, "quantity": 0, "net_price": 0,
     "realised_pnl": 512.5},
]  # fmt: skip


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def groww_fno(monkeypatch):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/v1/positions/user" and request.url.params.get("segment") == "FNO":
            return httpx.Response(200, json={"status": "SUCCESS", "payload": {"positions": FNO_ROWS}})
        return httpx.Response(404, json={"message": "no route"})

    monkeypatch.setattr(base, "TRANSPORT", httpx.MockTransport(handler))
    return seen


# ------------------------------------------------------------------ 1. positions parsing
def test_fno_positions_parse_with_direction_only_when_confirmed(groww_fno):
    from finresearch.portfolio.connectors.groww import GrowwConnector

    c = GrowwConnector({"api_key": "k", "totp_secret": "JBSWY3DPEHPK3PXP"}, ACCESS)
    got = run(c.fno_positions())
    assert [(p.symbol, p.quantity, p.direction_known) for p in got] == [
        ("NIFTY26OCT25000CE", D(75), True), ("NIFTY26OCT24500PE", D(-75), True),
        ("NIFTY26OCT25500CE", D(75), False), ("NIFTY26OCT24000PE", D(75), False)]  # fmt: skip
    assert got[1].avg_price == D("80.5") and got[1].segment == "FNO" and got[1].sell_quantity == D(75)
    assert "unknown" in got[2].note and "lacks" in got[3].note
    (req,) = groww_fno
    assert req.method == "GET" and req.url.params["segment"] == "FNO"


def test_allow_lists_still_block_writes_and_order_paths():
    from finresearch.adapters import groww_market as gm
    from finresearch.portfolio.connectors import groww
    from finresearch.portfolio.connectors.base import ForbiddenRequest, ReadOnlyHttp

    for paths in (groww.READ_PATHS, gm.MARKET_PATHS):
        http = ReadOnlyHttp(groww.BASE, read_paths=paths, auth_paths=groww.AUTH_PATHS)
        for method, path in [("POST", "/margins/detail/orders"), ("POST", "/order/create"), ("GET", "/order/create"),
                             ("POST", "/positions/user"), ("PUT", "/order/modify"), ("DELETE", "/order/cancel"),
                             ("GET", "/order/smart/create"), ("POST", "/live-data/ltp")]:  # fmt: skip
            with pytest.raises(ForbiddenRequest):
                http._check(method, path)
        assert not any(__import__("re").fullmatch(p, "/margins/detail/orders") for p in paths)


# ------------------------------------------------------------------ 2. paper fill / MTM / settlement math
# Rates in force on 12-Oct-2026 (fincalc.charges): option STT 0.15 % sell side, exercise STT 0.15 % of intrinsic,
# exchange 0.03553 % both sides, SEBI ₹10/crore, stamp 0.003 % buy side, GST 18 % on brokerage + exchange + SEBI;
# brokerage ₹20 an order. Expected values are worked out by hand below, not with the code under test.
TRADE_DAY = __import__("datetime").date(2026, 10, 12)
EXPIRY = __import__("datetime").date(2026, 10, 27)


def _spread():
    from finresearch.fincalc.paper import PaperLeg, fill_price

    # bull call spread, 75 units, 1 % slippage on LTPs 120 (buy) and 50 (sell)
    b, s = fill_price(120, "buy", 1.0), fill_price(50, "sell", 1.0)
    assert (b, s) == (121.2, 49.5)
    return [PaperLeg("call", 25000, "buy", 75, b), PaperLeg("call", 25200, "sell", 75, s)]


def test_paper_entry_costs_match_hand_computation():
    from finresearch.fincalc.paper import entry_costs

    c = entry_costs(_spread(), TRADE_DAY, 20)
    turnover = 121.2 * 75 + 49.5 * 75  # 12,802.50
    exch, sebi = turnover * 0.0003553, turnover * 0.000001
    stt, stamp = 49.5 * 75 * 0.0015, 121.2 * 75 * 0.00003
    gst = (40 + exch + sebi) * 0.18
    assert c.turnover == pytest.approx(12802.5)
    assert c.total == pytest.approx(exch + sebi + stt + stamp + 40 + gst)
    assert c.total == pytest.approx(58.424056285, abs=1e-6)


def test_paper_mark_to_market_and_square_off():
    from finresearch.fincalc.paper import gross_pnl, square_off_costs

    legs = _spread()
    assert gross_pnl(legs, [130, 60]) == pytest.approx((130 - 121.2) * 75 - (60 - 49.5) * 75)  # −127.50
    assert gross_pnl(legs, [130, None]) is None  # a missing price is unknown, never 0
    c = square_off_costs(legs, [130, 60], TRADE_DAY, 20)
    # closing: sell the long at 130 (STT), buy back the short at 60 (stamp)
    exch = (9750 + 4500) * 0.0003553
    sebi = 14250 * 0.000001
    assert c.total == pytest.approx(exch + sebi + 9750 * 0.0015 + 4500 * 0.00003 + 40 + (40 + exch + sebi) * 0.18)
    assert c.total == pytest.approx(67.9511845, abs=1e-6)


def test_paper_expiry_settlement_with_exercise_stt():
    from finresearch.fincalc.paper import PaperLeg, settle

    st = settle(_spread(), 25150, EXPIRY)
    assert st.final_values == [150, 0]
    assert st.gross == pytest.approx((150 - 121.2) * 75 + 49.5 * 75)  # 5,872.50
    assert st.itm_long_intrinsic == pytest.approx(150 * 75)
    assert st.exercise_stt == pytest.approx(150 * 75 * 0.0015)  # 16.875
    # a short put that finishes in the money: the writer pays no exercise STT
    short = settle([PaperLeg("put", 24500, "sell", 75, 80.0)], 24400, EXPIRY)
    assert short.gross == pytest.approx(-(100 - 80) * 75) and short.exercise_stt == 0
    with pytest.raises(ValueError):
        settle(_spread(), 0, EXPIRY)


def test_paper_results_count_every_trade():
    from finresearch.fincalc.paper import results

    r = results([100, -50, 200, -300, 50, 0])
    assert (r.closed, r.wins, r.losses) == (6, 3, 2)
    assert r.win_rate == pytest.approx(3 / 6) and r.expectancy == pytest.approx(0 / 6)
    assert r.avg_win == pytest.approx(350 / 3) and r.avg_loss == pytest.approx(-175)
    # cumulative 100, 50, 250, −50, 0, 0: the peak 250 to the trough −50
    assert r.max_drawdown == pytest.approx(300) and r.curve[-1] == 0
    assert results([]).win_rate is None

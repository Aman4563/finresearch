"""F&O positions, paper trading, exit rules, margin approximation and the event strip (#308). Fixtures only: synthetic
Groww answers in the docs' shapes, made-up contracts and prices; no network."""

from __future__ import annotations

import asyncio
import math
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
    assert c.total == pytest.approx(
        exch + sebi + 9750 * 0.0015 + 4500 * 0.00003 + 40 + (40 + exch + sebi) * 0.18
    )
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


# ------------------------------------------------------------------ fakes for the live pieces (no network)
from datetime import UTC, date, datetime, timedelta  # noqa: E402

from finresearch.fincalc.dates import IST  # noqa: E402

INSTRUMENTS_CSV = (
    "exchange,exchange_token,trading_symbol,groww_symbol,name,instrument_type,segment,series,isin,underlying_symbol,"
    "underlying_exchange_token,lot_size,expiry_date,strike_price,tick_size,freeze_quantity,is_reserved,buy_allowed,"
    "sell_allowed\n"
    "NSE,1,NIFTY26OCT25000CE,NSE-NIFTY-27Oct26-25000-CE,NIFTY,CE,FNO,,,NIFTY,26000,75,2026-10-27,25000,0.05,1800,0,1,1\n"
    "NSE,2,NIFTY26OCT25200CE,NSE-NIFTY-27Oct26-25200-CE,NIFTY,CE,FNO,,,NIFTY,26000,75,2026-10-27,25200,0.05,1800,0,1,1\n"
    "NSE,3,NIFTY26OCT24500PE,NSE-NIFTY-27Oct26-24500-PE,NIFTY,PE,FNO,,,NIFTY,26000,75,2026-10-27,24500,0.05,1800,0,1,1\n"
    "NSE,4,NIFTY26OCT25500CE,NSE-NIFTY-27Oct26-25500-CE,NIFTY,CE,FNO,,,NIFTY,26000,75,2026-10-27,25500,0.05,1800,0,1,1\n"
    "NSE,9,EXMPL,NSE-EXMPL,EXAMPLE LTD,EQ,CASH,EQ,INE000A01011,,,1,,,0.05,,0,1,1\n"
)  # fmt: skip


def at_ist(d: date, h: int, m: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, h, m, tzinfo=IST).astimezone(UTC)


class FakeMarket:
    """GrowwMarket's surface the book uses: a session, the instruments file and a segment-aware LTP."""

    def __init__(self, prices: dict[str, float] | None = None, session: bool = True) -> None:
        from finresearch.adapters.groww_market import Instruments

        self.prices, self.session, self.calls = dict(prices or {}), session, []
        self.inst = Instruments.parse(INSTRUMENTS_CSV)

    def token(self):
        return "tok" if self.session else None

    async def instruments(self):
        return self.inst

    async def ltp(self, keys, *, cached_only=False, segment="CASH"):
        self.calls.append((segment, sorted(keys)))
        return {k: (self.prices[k], datetime.now(UTC)) for k in keys if k in self.prices}


def make_chain(spot: float, quotes: dict[tuple[str, float], tuple[float, float | None]], expiry: date = EXPIRY,
               source: str = "NSE"):  # fmt: skip
    """An option chain: {(right, strike): (last price, IV %)}."""
    from finresearch.adapters.nse_fno import ChainRow, OptionChain, OptionQuote

    def q(right, k):
        got = quotes.get((right, k))
        if got is None:
            return None
        return OptionQuote(last_price=D(str(got[0])), iv=None if got[1] is None else D(str(got[1])), oi=None,
                           change_in_oi=None, volume=None, bid=None, ask=None)  # fmt: skip

    strikes = sorted({k for _, k in quotes})
    return OptionChain(symbol="NIFTY", expiry=expiry, underlying=D(str(spot)), as_of=None, source=source,
                       rows=[ChainRow(strike=D(str(k)), call=q("call", k), put=q("put", k)) for k in strikes])  # fmt: skip


class FakeSources:
    def __init__(self, chain=None, settlement=None, meetings=None, lot=75, expiries=(EXPIRY,)):
        self._chain, self._settle, self._meetings, self._lot, self._exp = (
            chain,
            settlement,
            meetings,
            lot,
            expiries,
        )
        self.calls: list[str] = []

    async def chain(self, symbol, expiry):
        self.calls.append(f"chain:{symbol}")
        if self._chain is None:
            raise RuntimeError("no chain")
        return self._chain

    async def settlement(self, symbol, expiry):
        self.calls.append(f"settle:{symbol}")
        return self._settle

    async def board_meetings(self, symbol):
        self.calls.append(f"meetings:{symbol}")
        if isinstance(self._meetings, Exception):
            raise self._meetings
        return self._meetings or []

    async def lot_size(self, symbol, expiry):
        return self._lot

    async def expiries(self, symbol):
        return list(self._exp)


@pytest.fixture
def offline(monkeypatch):
    """No NSE holidays, a fresh step memory and the book's sources/market replaced per test."""
    from finresearch.adapters import groww_market
    from finresearch.fnobook import events, service

    monkeypatch.setattr(groww_market, "_holidays", lambda: set())
    service._LAST.clear()
    events._CACHE.clear()


@pytest.fixture
def book_db(env, offline):
    """The test database with an empty paper book, no F&O alerts, no Groww connection and an empty event calendar
    (#367: so an index's tier-1 event dates are unknown)."""
    from sqlalchemy import delete

    from finresearch.db import session_scope
    from finresearch.db.models import (
        Alert,
        BrokerConnection,
        CalendarCoverage,
        CalendarEvent,
        FnoExitRule,
        FnoMark,
        PaperLeg,
        PaperStrategy,
    )

    with session_scope() as s:
        for m in (FnoMark, FnoExitRule, PaperLeg, PaperStrategy, CalendarEvent, CalendarCoverage):
            s.execute(delete(m))
        s.execute(delete(Alert).where(Alert.kind == "fno_exit"))
        s.execute(delete(BrokerConnection).where(BrokerConnection.key == "groww"))
    return env


def _record_spread(s, now):
    from finresearch.fnobook import book
    from finresearch.signals.fno import LegIn

    quotes = [
        book.Quote(120.0, "Groww LTP", "NIFTY26OCT25000CE"),
        book.Quote(50.0, "Groww LTP", "NIFTY26OCT25200CE"),
    ]
    return book.record(s, symbol="NIFTY", expiry=EXPIRY, lot=75, legs_in=[LegIn("call", 25000, "buy", 1),
                       LegIn("call", 25200, "sell", 1)], quotes=quotes, slippage_pct=1.0, brokerage=20, now=now)  # fmt: skip


# ------------------------------------------------------------------ 3. the book: record, mark, rules once, settle
def test_paper_record_marks_and_fires_a_profit_rule_once(book_db):
    from sqlalchemy import func, select

    from finresearch.db import session_scope
    from finresearch.db.models import Alert, FnoExitRule, PaperStrategy
    from finresearch.fnobook import service

    t0 = at_ist(TRADE_DAY, 10)
    with session_scope() as s:
        st = _record_spread(s, t0)
        assert [x.entry_price for x in st.legs] == [D("121.20"), D("49.50")]
        assert st.entry_costs == D("58.42") and st.mtm_gross is None  # not marked yet: unknown, not 0
        sid = st.id
        s.add(FnoExitRule(paper_id=sid, kind="profit", threshold=D(50), status="armed", created_at=t0))
        s.add(FnoExitRule(paper_id=sid, kind="delta", threshold=D("0.3"), status="armed", created_at=t0))
    market = FakeMarket({"NSE_NIFTY26OCT25000CE": 180.0, "NSE_NIFTY26OCT25200CE": 70.0})
    src = FakeSources(chain=None)  # no chain: the short leg's delta is unknown
    got = run(service.step(t0, market=market, sources=src))
    assert got == {"paper_marked": 1, "exit_rules_fired": 1}
    assert market.calls == [("FNO", ["NSE_NIFTY26OCT25000CE", "NSE_NIFTY26OCT25200CE"])]  # one LTP batch
    with session_scope() as s:
        st = s.get(PaperStrategy, sid)
        # (180 − 121.20) × 75 − (70 − 49.50) × 75 = 4,410 − 1,537.50
        assert st.mtm_gross == D("2872.50") and st.mtm_source == "Groww LTP"
        profit, delta = s.scalars(select(FnoExitRule).order_by(FnoExitRule.id)).all()
        # base: the debit paid (121.20 − 49.50) × 75 = 5,377.50; 2,872.50 / 5,377.50 = 53.42 %
        assert profit.status == "fired" and float(profit.last_value) == pytest.approx(
            2872.5 / 5377.5 * 100, abs=1e-3
        )
        assert delta.status == "armed" and delta.last_value is None and "unknown" in delta.note
        alerts = s.scalars(select(Alert).where(Alert.kind == "fno_exit")).all()
        assert (
            len(alerts) == 1
            and "profit target 50%" in alerts[0].message
            and "nothing was traded" in alerts[0].message
        )
    # 2 minutes later: not due (no call); 6 minutes later: re-read, still true, no second alert
    assert run(service.step(t0 + timedelta(minutes=2), market=market, sources=src)) == {}
    assert len(market.calls) == 1
    run(service.step(t0 + timedelta(minutes=6), market=market, sources=src))
    with session_scope() as s:
        assert s.scalar(select(func.count()).select_from(Alert).where(Alert.kind == "fno_exit")) == 1


def test_step_does_nothing_without_open_trades_or_rules(book_db):
    from finresearch.fnobook import service

    market, src = FakeMarket(), FakeSources()
    assert run(service.step(at_ist(TRADE_DAY, 10), market=market, sources=src)) == {}
    assert market.calls == [] and src.calls == []


def test_paper_settles_at_expiry_close_with_exercise_stt(book_db):
    from finresearch.db import session_scope
    from finresearch.db.models import PaperStrategy
    from finresearch.fnobook import service

    with session_scope() as s:
        sid = _record_spread(s, at_ist(TRADE_DAY, 10)).id
    # the day after expiry, NSE's close not out yet: waits
    t1 = at_ist(EXPIRY + timedelta(days=1), 9, 0)
    got = run(service.step(t1, market=FakeMarket(session=False), sources=FakeSources(settlement=None)))
    assert got == {"paper_awaiting": 1}
    with session_scope() as s:
        st = s.get(PaperStrategy, sid)
        assert st.status == "awaiting_settlement" and st.net_pnl is None and "waiting" in st.note
    service._LAST.clear()
    src = FakeSources(settlement=(25150.0, "NSE index history (NIFTY 50) close"))
    assert run(service.step(t1 + timedelta(hours=2), market=FakeMarket(session=False), sources=src)) == {
        "paper_settled": 1}  # fmt: skip
    with session_scope() as s:
        st = s.get(PaperStrategy, sid)
        assert st.status == "closed" and st.close_reason == "expiry" and st.settlement_price == D("25150.00")
        # gross (150 − 121.20) × 75 + 49.50 × 75 = 5,872.50; exercise STT 150 × 75 × 0.15 % = 16.875
        assert float(st.gross_pnl) == pytest.approx(5872.5) and float(st.exit_costs) == pytest.approx(
            16.88, abs=0.01
        )
        assert float(st.net_pnl) == pytest.approx(5872.5 - 58.42 - 16.875, abs=0.01)
        assert [x.exit_price for x in st.legs] == [D("150.00"), D("0.00")]


def _store_groww_positions(rows, now):
    from finresearch.db import session_scope
    from finresearch.db.models import BrokerConnection
    from finresearch.portfolio.connectors.groww import map_fno_position
    from finresearch.portfolio.connectors.sync import fno_state

    with session_scope() as s:
        s.add(BrokerConnection(key="groww", enabled=True, auto_sync=True, status="connected",
                               state={"fno_positions": fno_state([p for p in map(map_fno_position, rows) if p.quantity],
                                                                   now)}))  # fmt: skip


def _bs_delta(right, spot, strike, t, rate, vol):
    """Black-Scholes delta written out here (independent of fincalc.options)."""
    import math

    d1 = (math.log(spot / strike) + (rate + vol * vol / 2) * t) / (vol * math.sqrt(t))
    n = 0.5 * (1 + math.erf(d1 / math.sqrt(2)))
    return n if right == "call" else n - 1


def test_the_delta_rule_uses_fbils_rate_at_the_expiry(book_db, monkeypatch):
    """#366/#385: the market pass prices the delta rule's greeks at FBIL's par yield at the trade's expiry (the
    conftest curve's 0.25-year point, 5.34 % half-yearly), not a fixed 6.5 %."""
    from finresearch.db import session_scope
    from finresearch.db.models import FnoExitRule
    from finresearch.fnobook import service

    t0 = at_ist(TRADE_DAY, 10)
    with session_scope() as s:
        sid = _record_spread(s, t0).id
        s.add(FnoExitRule(paper_id=sid, kind="delta", threshold=D("0.3"), status="armed", created_at=t0))
    seen = []
    monkeypatch.setattr(
        service, "paper_short_deltas", lambda st, chain, today, rate, now=None: seen.append(rate) or [None]
    )
    run(service.market_pass(t0, FakeMarket({"NSE_NIFTY26OCT25000CE": 180.0, "NSE_NIFTY26OCT25200CE": 70.0}),
                            FakeSources(chain=None)))  # fmt: skip
    assert seen == [pytest.approx(2 * math.log(1 + 0.0534 / 2))]


# ------------------------------------------------------------------ 4. real positions: P&L, greeks, unknowns
def test_positions_view_prices_known_legs_and_keeps_unknowns_unknown(book_db):
    from finresearch.fnobook import service

    t0 = at_ist(TRADE_DAY, 11)
    _store_groww_positions(FNO_ROWS, t0)
    market = FakeMarket(
        {"NSE_NIFTY26OCT25000CE": 130.0, "NSE_NIFTY26OCT24500PE": 70.5, "NSE_NIFTY26OCT25500CE": 20.0}
    )
    chain = make_chain(
        24900, {("call", 25000): (130, 14.0), ("put", 24500): (70.5, 16.0), ("call", 25500): (20, 13.0)}
    )
    v = run(service.positions_view(t0, market=market, sources=FakeSources(chain=chain)))
    assert v["status"] == "ok" and v["ltp_source"] == "Groww LTP"
    g = next(x for x in v["groups"] if x["key"] == "NIFTY:2026-10-27")
    assert g["days_to_expiry"] == 15 and g["spot"] == 24900
    legs = {x["symbol"]: x for x in g["legs"]}
    assert legs["NIFTY26OCT25000CE"]["unrealised_pnl"] == pytest.approx((130 - 120) * 75)
    assert legs["NIFTY26OCT24500PE"]["unrealised_pnl"] == pytest.approx((70.5 - 80.5) * -75)
    # the row whose direction is not confirmed: no P&L, no greeks, so no group totals and no max loss
    assert legs["NIFTY26OCT25500CE"]["unrealised_pnl"] is None and legs["NIFTY26OCT25500CE"]["greeks"] is None
    assert g["unrealised_pnl"] is None and g["net_greeks"] is None and g["max_loss"] is None
    t = (
        15 * 1440 + 270
    ) / 525_600  # #359: 12-Oct 11:00 IST → 27-Oct 15:30 IST, to the minute (was 15 days flat)
    # #366/#385: the greeks use FBIL's par yield at the expiry (signals.rates.risk_free; the conftest stub is FBIL's
    # 23-Sep-2026 curve, whose 0.25-year point, used for a 15-day option, is 5.34 % half-yearly), not a fixed 6.5 %
    r = 2 * math.log(1 + 0.0534 / 2)
    assert g["rate"]["rate"] == pytest.approx(r) and g["rate"]["tenor_years"] == 0.25
    assert g["rate"]["as_of"] == "2026-09-23" and "FBIL" in g["rate"]["source"]
    assert legs["NIFTY26OCT25000CE"]["greeks"]["delta"] == pytest.approx(
        75 * _bs_delta("call", 24900, 25000, t, r, 0.14), abs=1e-3)  # fmt: skip
    assert legs["NIFTY26OCT24500PE"]["greeks"]["delta"] == pytest.approx(
        -75 * _bs_delta("put", 24900, 24500, t, r, 0.16), abs=1e-3)  # fmt: skip
    # a contract the instruments file does not list stays unresolved
    un = next(x for x in v["groups"] if x["key"] == "unresolved")
    assert [x["symbol"] for x in un["legs"]] == ["NIFTY26OCT24000PE"] and un["max_loss"] is None


def test_positions_view_max_loss_of_a_defined_risk_group_and_real_rules(book_db):
    from sqlalchemy import select

    from finresearch.db import session_scope
    from finresearch.db.models import Alert, FnoExitRule
    from finresearch.fnobook import service

    t0 = at_ist(TRADE_DAY, 11)
    # a bull call spread held at Groww: long 25000 CE at 120, short 25200 CE at 50 (both confirmed)
    rows = [FNO_ROWS[0], {**FNO_ROWS[0], "trading_symbol": "NIFTY26OCT25200CE", "credit_quantity": 0,
                          "debit_quantity": 75, "quantity": -75, "net_price": 50}]  # fmt: skip
    _store_groww_positions(rows, t0)
    market = FakeMarket({"NSE_NIFTY26OCT25000CE": 100.0, "NSE_NIFTY26OCT25200CE": 40.0})
    chain = make_chain(24900, {("call", 25000): (100, 14.0), ("call", 25200): (40, 13.5)})
    src = FakeSources(chain=chain)
    v = run(service.positions_view(t0, market=market, sources=src))
    (g,) = v["groups"]
    assert g["max_loss"] == pytest.approx(-(120 - 50) * 75)  # the net debit
    assert g["unrealised_pnl"] == pytest.approx((100 - 120) * 75 + (40 - 50) * -75)  # −1,500 + 750
    with session_scope() as s:
        for kind, thr in (("dte", 15), ("stop", 20), ("event", 3)):
            s.add(FnoExitRule(position_key="NIFTY:2026-10-27", kind=kind, threshold=D(thr), status="armed",
                              created_at=t0))  # fmt: skip
    got = run(service.step(t0, market=market, sources=src))
    assert got["exit_rules_fired"] == 1
    with session_scope() as s:
        dte, stop, event = s.scalars(select(FnoExitRule).order_by(FnoExitRule.id)).all()
        assert dte.status == "fired" and float(dte.last_value) == 15
        # −750 on a debit of (120 − 50) × 75 = 5,250 is −14.3 %: inside the 20 % stop
        assert stop.status == "armed" and float(stop.last_value) == pytest.approx(-750 / 5250 * 100, abs=1e-3)
        # an index's event is RBI policy, and there is no RBI source: unknown, never "no event"
        assert event.status == "armed" and event.last_value is None and "could not be read" in event.note
        (a,) = s.scalars(select(Alert).where(Alert.kind == "fno_exit")).all()
        assert a.data["position_key"] == "NIFTY:2026-10-27" and a.data["path"] == "/fno?symbol=NIFTY"


# ------------------------------------------------------------------ 5. API: paper trade from the builder, close, book
@pytest.fixture
def api(book_db, monkeypatch):
    from finresearch.adapters import groww_market
    from finresearch.fnobook import sources

    market = FakeMarket({"NSE_NIFTY26OCT25000CE": 120.0})  # Groww prices one leg; the chain the other
    chain = make_chain(24950, {("call", 25000): (119, 14.0), ("call", 25200): (50, 13.5)})
    monkeypatch.setattr(groww_market, "MARKET", market)
    monkeypatch.setattr(sources, "SOURCES", FakeSources(chain=chain))

    def client(when):
        from starlette.testclient import TestClient

        from finresearch.api import create_app

        return TestClient(create_app(clock=lambda: when))

    return client


SPREAD = {"symbol": "nifty", "expiry": EXPIRY.isoformat(), "slippage_pct": "1", "label": "test spread",
          "legs": [{"right": "call", "strike": "25000", "side": "buy"}, {"right": "call", "strike": "25200",
                                                                          "side": "sell"}]}  # fmt: skip


def test_api_paper_trade_open_close_and_book(api):
    with api(at_ist(TRADE_DAY, 10)) as c:
        r = c.post("/api/fno/paper", json=SPREAD)
        assert r.status_code == 201, r.text
        t = r.json()
        assert [x["entry_price"] for x in t["legs"]] == [121.2, 49.5]
        assert [x["trading_symbol"] for x in t["legs"]] == ["NIFTY26OCT25000CE", "NIFTY26OCT25200CE"]
        assert t["price_source"] == "Groww LTP, NSE option chain last price" and t["entry_costs"] == 58.42
        assert t["mtm_gross"] is None and t["spot_at_entry"] == 24950
        bad = c.post("/api/fno/exit-rules", json={"paper_id": t["id"], "position_key": "X", "kind": "dte",
                                                  "threshold": 3})  # fmt: skip
        assert bad.status_code == 422
        assert (
            c.post(
                "/api/fno/exit-rules", json={"paper_id": t["id"], "kind": "delta", "threshold": 2}
            ).status_code
            == 422
        )
        rule = c.post(
            "/api/fno/exit-rules", json={"paper_id": t["id"], "kind": "stop", "threshold": 50}
        ).json()
        assert rule["status"] == "armed" and rule["phrase"] == "stop 50%"
        closed = c.post(f"/api/fno/paper/{t['id']}/close").json()
        # closes at 120 (Groww) and 50 (chain): (120 − 121.20) × 75 − (50 − 49.50) × 75 = −127.50
        assert closed["status"] == "closed" and closed["gross_pnl"] == -127.5
        exit_ = (120 + 50) * 75 * 0.0003553
        sebi = (120 + 50) * 75 * 0.000001
        costs = exit_ + sebi + 120 * 75 * 0.0015 + 50 * 75 * 0.00003 + 40 + (40 + exit_ + sebi) * 0.18
        assert closed["exit_costs"] == pytest.approx(round(costs, 2), abs=0.01)
        assert closed["net_pnl"] == pytest.approx(-127.5 - 58.42 - costs, abs=0.01)
        assert c.post(f"/api/fno/paper/{t['id']}/close").status_code == 409  # closed: stays closed
        book = c.get("/api/fno/paper").json()
        assert book["counts"] == {"open": 0, "awaiting_settlement": 0, "closed": 1, "all": 1}
        assert book["results"]["wins"] == 0 and book["results"]["win_rate"] == 0
        assert book["results"]["max_drawdown"] == pytest.approx(-closed["net_pnl"])
        assert book["rules"][0]["kind"] == "stop" and book["default_slippage_pct"] == 0.5
        # #382 (d): no recorded quotes yet, so the default is the labelled assumption, not a measurement
        assert book["default_slippage_source"].startswith("assumption")
        # no route deletes a paper trade
        assert c.delete(f"/api/fno/paper/{t['id']}").status_code in (404, 405)


def test_api_paper_trade_refused_out_of_session_and_without_a_price(api, monkeypatch):
    from finresearch.fnobook import sources

    with api(at_ist(TRADE_DAY, 17)) as c:
        r = c.post("/api/fno/paper", json=SPREAD)
        assert r.status_code == 409 and "market hours" in r.json()["detail"]
    monkeypatch.setattr(sources, "SOURCES", FakeSources(chain=None))  # no chain: the 25200 CE has no price
    with api(at_ist(TRADE_DAY, 10)) as c:
        r = c.post("/api/fno/paper", json=SPREAD)
        assert r.status_code == 422 and "no live price" in r.json()["detail"]
        assert c.get("/api/fno/paper").json()["counts"]["all"] == 0


def test_api_positions_without_a_groww_connection(api):
    with api(at_ist(TRADE_DAY, 10)) as c:
        v = c.get("/api/fno/positions").json()
        assert v["status"] == "not_connected" and v["groups"] == []


# ------------------------------------------------------------------ 6. margin approximation, labelled
def test_margin_approximation_is_labelled_and_adds_up(api):
    from finresearch.fincalc import margin_approx as ma

    long_only = ma.estimate([ma.MarginLeg("call", 25000, 75, 120.0, 0.14)], 24900, 15 / 365, 0.065, "index")
    assert long_only.total == pytest.approx(120 * 75) and long_only.scan_risk == 0
    legs = [ma.MarginLeg("call", 25500, -75, 20.0, 0.13), ma.MarginLeg("put", 24500, -75, 70.5, 0.16)]
    e = ma.estimate(legs, 24900, 15 / 365, 0.065, "index")
    assert e.exposure == pytest.approx((25500 + 24500) * 75 * 0.02)  # 2 % of the short strikes' notional
    assert e.premium_paid == 0 and e.scan_risk > 0 and e.total == pytest.approx(e.scan_risk + e.exposure)
    with pytest.raises(ValueError):
        ma.estimate([ma.MarginLeg("put", 24500, -75, 70.5, None)], 24900, 0.04, 0.065, "index")
    with api(at_ist(TRADE_DAY, 10)) as c:
        r = c.post("/api/fno/margin", json={k: SPREAD[k] for k in ("symbol", "expiry", "legs")}).json()
        assert r["status"] == "approximate" and "approximate" in r["label"]
        assert r["groww_calculator"]["available"] is False
        assert all(v["status"] == "unconfirmed" for v in r["assumptions"].values())
        # #366/#385: the estimate names the rate it used (FBIL's par yield at the expiry, here the conftest curve)
        assert (
            r["rate"]["rate"] == pytest.approx(2 * math.log(1 + 0.0534 / 2)) and "FBIL" in r["rate"]["source"]
        )


# ------------------------------------------------------------------ 7. events strip
class _Meeting:
    def __init__(self, day, results):
        self.day, self.results = day, results


def test_event_strip_and_event_dates(api, monkeypatch):
    from finresearch.fnobook import events, sources

    with api(at_ist(TRADE_DAY, 10)) as c:
        nifty = c.get("/api/fno/NIFTY/events").json()
        assert nifty["results"]["status"] == "n/a" and nifty["rbi"]["status"] == "no_source"
        assert [x["kind"] for x in nifty["items"]] == ["expiry"]
    src = FakeSources(meetings=[_Meeting(date(2026, 10, 20), True), _Meeting(date(2026, 10, 15), False),
                                _Meeting(date(2026, 7, 20), True)])  # fmt: skip
    monkeypatch.setattr(sources, "SOURCES", src)
    with api(at_ist(TRADE_DAY, 10)) as c:
        st = c.get("/api/fno/EXMPL/events").json()
        assert st["results"]["next"] == "2026-10-20" and [x["kind"] for x in st["items"]] == [
            "results",
            "expiry",
        ]
    assert run(events.event_dates("EXMPL", TRADE_DAY, src)) == [(date(2026, 10, 20), "results")]
    assert run(events.event_dates("NIFTY", TRADE_DAY, src)) is None  # RBI unknown: unknown, not "no event"
    events._CACHE.clear()
    bad = FakeSources(meetings=RuntimeError("NSE down"))
    assert run(events.event_dates("EXMPL", TRADE_DAY, bad)) is None


def test_api_paper_trade_keeps_the_card_it_was_taken_from(api):
    """#383: a paper trade can name its card (card_log row + index) for the shortfall against the card's mids."""
    from finresearch.db import session_scope
    from finresearch.fnobook import card_log

    def card(symbol, kind="plan"):
        with session_scope() as s:
            r = card_log.row(kind, symbol=symbol, expiry=EXPIRY, inputs={}, cards=[{}], response={"legs": []})
            s.add(r)
            s.flush()
            return r.id

    nifty, other, check = card("NIFTY"), card("SYNTHCO"), card("NIFTY", "strategy")
    with api(at_ist(TRADE_DAY, 10)) as c:
        for cid, idx in ((nifty + other + check + 1000, 0), (other, 0), (check, 0), (nifty, 3)):
            r = c.post("/api/fno/paper", json={**SPREAD, "card_log_id": cid, "card_index": idx})
            assert r.status_code == 422, (cid, r.text)
        assert c.get("/api/fno/paper").json()["counts"]["all"] == 0
        r = c.post("/api/fno/paper", json={**SPREAD, "card_log_id": nifty, "card_index": 0})
        assert r.status_code == 201, r.text
        assert (r.json()["card_log_id"], r.json()["card_index"]) == (nifty, 0)
        assert c.post("/api/fno/paper", json=SPREAD).json()["card_log_id"] is None

"""The forward quote recorder (#363, P1.1): Groww's Get Quote on the read-only allow-list, contract selection, parsing,
per-row and per-sweep data-quality checks, storage, the claimed monitor slots and the NSE fallback. Every Groww
response is synthetic, shaped after the Get Quote sample in Groww's docs (adapters.groww_market docstring); no
network, no real account, no real token."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

import httpx
import pytest

from finresearch.adapters import groww_budget, groww_market
from finresearch.adapters.groww_market import GrowwMarket
from finresearch.fincalc.dates import IST
from finresearch.portfolio.connectors import base
from finresearch.portfolio.connectors.base import ForbiddenRequest

TOKEN = "-".join(["fake", "access"]) + "q" * 20  # assembled at runtime: no key-like literal (gitleaks)
TUE = date(2026, 10, 20)  # a Tuesday, no NSE holiday
AT_0930 = datetime(2026, 10, 20, 9, 30, 5, tzinfo=IST)


def quote_payload(bid=120.05, ask=121.0, ltt_ms=None, depth=True) -> dict:
    """The documented Get Quote payload (field names from Groww's docs, values made up)."""
    p = {"average_price": 120.4, "bid_price": bid, "bid_quantity": 750, "offer_price": ask, "offer_quantity": 1500,
         "last_price": 120.5, "last_trade_quantity": 75, "last_trade_time": ltt_ms, "volume": 123450,
         "open_interest": 456000, "previous_open_interest": 450000, "implied_volatility": 12.34,
         "total_buy_quantity": 90000, "total_sell_quantity": 80000, "ohlc": "{open: 1,high: 2,low: 1,close: 1}"}  # fmt: skip
    if depth:
        p["depth"] = {"buy": [{"price": bid, "quantity": 750}, {"price": 120.0, "quantity": 300},
                              {"price": 0, "quantity": 0}],
                      "sell": [{"price": ask, "quantity": 1500}, {"price": 121.5, "quantity": 225}]}  # fmt: skip
    return p


class FakeSession:
    def __init__(self, token: str | None = TOKEN) -> None:
        self._token, self.state = token, "connected" if token else "not_connected"

    def token(self) -> str | None:
        return self._token

    def forget(self) -> None:
        self._token = None


@pytest.fixture
def mock_groww(monkeypatch):
    """A MockTransport answering /v1/live-data/quote; returns the list of requests."""

    class NoWait:
        def reserve(self, key, interval):
            return 0.0

        def hold_off(self, key, seconds):
            pass

    monkeypatch.setattr(groww_budget.BUDGET, "slots", NoWait())
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/v1/live-data/quote":
            return httpx.Response(200, json={"status": "SUCCESS", "payload": quote_payload()})
        return httpx.Response(404, json={"status": "FAILURE", "error": {"message": "no route"}})

    monkeypatch.setattr(base, "TRANSPORT", httpx.MockTransport(handler))
    return seen


def market(t: datetime = AT_0930, token: str | None = TOKEN) -> GrowwMarket:
    return GrowwMarket(session=FakeSession(token), clock=lambda: t.astimezone(UTC), holidays=set)


# ------------------------------------------------------------------ the Get Quote endpoint, read-only
def test_get_quote_is_an_allowed_get_in_the_live_bucket(mock_groww):
    m = market()
    payload = asyncio.run(m.quote("NIFTY26OCT25000CE"))
    assert payload["bid_price"] == 120.05 and payload["offer_quantity"] == 1500
    (r,) = mock_groww
    assert r.method == "GET" and r.url.path == "/v1/live-data/quote"
    assert dict(r.url.params) == {"exchange": "NSE", "segment": "FNO", "trading_symbol": "NIFTY26OCT25000CE"}
    assert groww_budget.category("/live-data/quote") == "live"
    with pytest.raises(ForbiddenRequest):  # still GET only
        asyncio.run(m._api(TOKEN).auth_post("/live-data/quote", json={}))


def test_get_quote_is_never_cached(mock_groww):
    m = market()
    asyncio.run(m.quote("NIFTY26OCT25000CE"))
    asyncio.run(m.quote("NIFTY26OCT25000CE"))
    assert len(mock_groww) == 2  # each call is a snapshot


def test_get_quote_without_a_session_raises(mock_groww):
    from finresearch.portfolio.connectors.base import ConnectorError

    with pytest.raises(ConnectorError):
        asyncio.run(market(token=None).quote("NIFTY26OCT25000CE"))
    assert mock_groww == []


# ------------------------------------------------------------------ parsing: paise, unknown stays None
def test_parse_groww_quote_to_compact_columns():
    from finresearch.fnohist import quotes as q

    ltt = int(datetime(2026, 10, 20, 9, 29, 58, tzinfo=IST).timestamp() * 1000)
    row = q.parse_groww(quote_payload(ltt_ms=ltt))
    assert (row["bid"], row["bid_qty"], row["ask"], row["ask_qty"]) == (12005, 750, 12100, 1500)
    assert row["ltp"] == 12050 and row["oi"] == 456000 and row["volume"] == 123450
    assert row["iv"] == pytest.approx(12.34)
    # depth flattened as (price paise, quantity) pairs in the order returned; the empty level (price 0) is dropped
    assert row["depth_bid"] == [12005, 750, 12000, 300] and row["depth_ask"] == [12100, 1500, 12150, 225]
    assert row["exchange_at"] == datetime(2026, 10, 20, 9, 29, 58, tzinfo=IST)


def test_parse_groww_quote_missing_sides_are_unknown_not_zero():
    from finresearch.fnohist import quotes as q

    p = quote_payload(bid=0, ask=None, depth=False)
    p.update(bid_quantity=0, offer_quantity=None, implied_volatility=0, last_price=None, open_interest=None)
    row = q.parse_groww(p)
    assert row["bid"] is None and row["bid_qty"] is None  # a zero bid is "no bid", never a bid of 0
    assert row["ask"] is None and row["ask_qty"] is None
    assert row["depth_bid"] is None and row["depth_ask"] is None
    assert row["iv"] is None and row["ltp"] is None and row["oi"] is None and row["exchange_at"] is None
    assert row["volume"] == 123450


def test_row_flags():
    from finresearch.fnohist import quotes as q

    t = datetime(2026, 10, 20, 9, 30, 10, tzinfo=IST)
    fresh = t - timedelta(seconds=60)
    assert q.flags({"bid": 100, "ask": 105, "exchange_at": fresh}, t) == 0
    assert q.flags({"bid": 105, "ask": 105, "exchange_at": fresh}, t) == q.LOCKED
    assert q.flags({"bid": 106, "ask": 105, "exchange_at": fresh}, t) == q.CROSSED
    assert q.flags({"bid": None, "ask": 105, "exchange_at": fresh}, t) == q.NO_BID
    assert q.flags({"bid": 100, "ask": None, "exchange_at": None}, t) == q.NO_ASK | q.NO_TRADE_TIME
    assert q.flags({"bid": 100, "ask": 105, "exchange_at": fresh - timedelta(seconds=1)}, t) == q.STALE


# ------------------------------------------------------------------ the cost model's interface (issue maths)
def test_mid_half_spread_and_depth_cost_by_hand():
    from finresearch.fnohist.quotes import Quote

    qt = Quote.from_row({"bid": 12005, "ask": 12100, "bid_qty": 750, "ask_qty": 1500,
                         "depth_bid": [12005, 750, 12000, 300], "depth_ask": [12100, 1500, 12150, 225]})  # fmt: skip
    assert qt.mid == Decimal("120.525") and qt.half_spread == Decimal("0.475")
    assert qt.rel_half_spread == Decimal("0.475") / Decimal("120.525")
    # buy 1600: 121.00 x 1500 + 121.50 x 100 - 1600 x 120.525 = 193650 - 192840 = 810
    assert qt.depth_cost(1600, "buy") == Decimal("810.000")
    # sell 900: 900 x 120.525 - (120.05 x 750 + 120.00 x 150) = 108472.5 - 108037.5 = 435
    assert qt.depth_cost(900, "sell") == Decimal("435.000")
    assert qt.depth_cost(1800, "buy") is None  # deeper than the book shown: unknown, not the last level's price


def test_no_mid_without_a_two_sided_uncrossed_quote():
    from finresearch.fnohist.quotes import Quote

    for bid, ask in ((None, 100), (100, None), (100, 100), (101, 100)):
        qt = Quote.from_row({"bid": bid, "ask": ask})
        assert qt.mid is None and qt.half_spread is None and qt.rel_half_spread is None
        assert qt.depth_cost(1, "buy") is None


# ------------------------------------------------------------------ storage and the day's acceptance checks
SNAPS = [datetime.combine(TUE, t, IST) for t in (time(9, 30), time(12, 0), time(15, 15), time(15, 25))]


def synthetic_row(snap: datetime, und: str, expiry: date, strike: int, kind: str, *, spot: int,
                  bid: int | None = 12005, ask: int | None = 12100, age_s: int = 5) -> dict:  # fmt: skip
    from finresearch.fnohist import quotes as q

    req = snap + timedelta(seconds=3)
    r = {"snapshot_at": snap, "underlying": und, "expiry": expiry, "strike": strike * 100, "option_type": kind,
         "requested_at": req, "exchange_at": req - timedelta(seconds=age_s), "bid": bid, "ask": ask,
         "bid_qty": 750 if bid else None, "ask_qty": 1500 if ask else None,
         "depth_bid": [bid or 1, 750, 12000, 300, 11995, 75, 11990, 1500, 11985, 225],
         "depth_ask": [ask or 1, 1500, 12150, 225, 12155, 75, 12160, 1500, 12165, 300],
         "ltp": 12050, "oi": 4560000, "volume": 12345000, "iv": 12.34, "underlying_ltp": spot * 100,
         "source": "groww"}  # fmt: skip
    r["flags"] = q.flags(r, req)
    return r


def full_day(crossed: int = 0, stale_atm: bool = False) -> list[dict]:
    rows = []
    for snap in SNAPS:
        for und, spot, step, expiries in (("NIFTY", 25012, 50, (date(2026, 10, 20), date(2026, 10, 27))),
                                          ("BANKNIFTY", 56040, 100, (date(2026, 10, 28), date(2026, 11, 25)))):  # fmt: skip
            atm = round(spot / step) * step
            for exp in expiries:
                for k in range(-10, 11):
                    for kind in ("CE", "PE"):
                        age = 120 if (stale_atm and k == 0) else 5
                        rows.append(synthetic_row(snap, und, exp, atm + k * step, kind, spot=spot, age_s=age))
    for r in rows[:crossed]:
        r.update(bid=12200, flags=r["flags"] | 4)
    return rows


@pytest.fixture
def qdb(env):
    from sqlalchemy import text

    from finresearch.db import session_scope

    with session_scope() as s:
        s.execute(text("TRUNCATE option_quote"))
        s.execute(text("DELETE FROM alert_eval_slot WHERE slot LIKE 'quotes:%'"))
    yield env


def test_store_is_idempotent_and_keyed_on_instrument_and_snapshot(qdb):
    from finresearch.fnohist import quotes as q

    rows = full_day()
    assert len(rows) == 672
    assert q.store(rows) == 672
    assert q.store(rows[:10]) == 0  # (instrument, snapshot_at) already stored
    got = q.load("NIFTY", SNAPS[0], SNAPS[0])
    assert len(got) == 84 and got[0].meta["snapshot_at"] == SNAPS[0]
    assert got[0].mid == Decimal("120.525")


def test_day_checks_pass_on_a_clean_day(qdb):
    from finresearch.fnohist import quotes as q

    q.store(full_day())
    c = q.day_checks(TUE)
    assert c["rows"] == 672 and c["expected"] == 672 and c["rows_ok"]
    assert c["crossed_or_locked"] == 0 and c["crossed_ok"]
    assert c["atm_checked"] == 32 and c["atm_fresh"] == 32 and c["atm_ok"]
    assert c["ok"] is True


def test_day_checks_fail_on_crossed_quotes_stale_atm_and_missing_rows(qdb):
    from finresearch.fnohist import quotes as q

    rows = full_day(crossed=1, stale_atm=True)
    q.store(rows[:-1])
    c = q.day_checks(TUE)
    assert c["rows"] == 671 and not c["rows_ok"]
    assert c["crossed_or_locked"] == 1 and not c["crossed_ok"]  # 1/671 = 0.15% >= 0.1%
    assert c["atm_fresh"] < c["atm_checked"] and not c["atm_ok"]
    assert c["ok"] is False


def test_day_checks_on_a_day_with_nothing_is_not_ok(qdb):
    from finresearch.fnohist import quotes as q

    c = q.day_checks(TUE)
    assert c["rows"] == 0 and c["ok"] is False  # no data is never a pass


# ------------------------------------------------------------------ which contracts a sweep asks for
CSV_HEAD = ("exchange,exchange_token,trading_symbol,groww_symbol,name,instrument_type,segment,series,isin,"
            "underlying_symbol,underlying_exchange_token,expiry_date,strike_price,lot_size,tick_size,"
            "freeze_quantity,is_reserved,buy_allowed,sell_allowed\n")  # fmt: skip


def instruments_csv(skip_pe: int | None = None) -> str:
    rows = ["NSE,26000,NIFTY,NSE-NIFTY,Nifty 50,IDX,CASH,,,,,,,1,0.05,,0,0,0",
            "NSE,26009,BANKNIFTY,NSE-BANKNIFTY,Nifty Bank,IDX,CASH,,,,,,,1,0.05,,0,0,0"]  # fmt: skip
    tok = 40000
    for und, lo, hi, step, expiries in (
        ("NIFTY", 24000, 26000, 50, ("2026-10-13", "2026-10-20", "2026-10-27", "2026-11-03")),
        ("BANKNIFTY", 54000, 58000, 100, ("2026-10-28", "2026-11-25")),
    ):
        for exp in expiries:
            for k in range(lo, hi + 1, step):
                for kind in ("CE", "PE"):
                    if kind == "PE" and k == skip_pe:
                        continue
                    tok += 1
                    sym = f"{und}{exp[2:4]}{exp[5:7]}{exp[8:]}{k}{kind}"
                    rows.append(f"NSE,{tok},{sym},NSE-{sym},{und},{kind},FNO,,,{und},26000,{exp},{k},75,0.05,,0,1,1")
    return CSV_HEAD + "\n".join(rows) + "\n"


def test_select_contracts_near_and_next_expiry_atm_plus_minus_ten():
    from finresearch.adapters.groww_market import Instruments
    from finresearch.monitor import quotes as job

    inst = Instruments.parse(instruments_csv())
    got = job.select_contracts(inst, "NIFTY", Decimal("25012.4"), TUE)
    assert {c.expiry for c in got} == {date(2026, 10, 20), date(2026, 10, 27)}  # expiry day counts; past ones not
    strikes = sorted({c.strike for c in got})
    assert len(strikes) == 21 and strikes[10] == Decimal("25000")  # ATM = nearest listed strike
    assert strikes[0] == Decimal("24500") and strikes[-1] == Decimal("25500")
    assert len(got) == 84 and all(c.trading_symbol for c in got)
    one = next(c for c in got if c.expiry == date(2026, 10, 20) and c.strike == 25000 and c.kind == "CE")
    assert one.trading_symbol == "NIFTY26102025000CE"


def test_select_contracts_keeps_a_contract_missing_from_the_file_as_unresolved():
    from finresearch.adapters.groww_market import Instruments
    from finresearch.monitor import quotes as job

    inst = Instruments.parse(instruments_csv(skip_pe=55900))
    got = job.select_contracts(inst, "BANKNIFTY", Decimal("56040"), TUE)
    assert len(got) == 84
    missing = [c for c in got if c.trading_symbol is None]
    assert [(c.strike, c.kind) for c in missing] == [(Decimal("55900"), "PE")] * 2


# ------------------------------------------------------------------ the claimed slots
def test_due_slot_window_trading_days_and_holidays():
    from finresearch.monitor import quotes as job

    at = lambda h, m, s=0, d=TUE: datetime.combine(d, time(h, m, s), IST)  # noqa: E731
    assert job.due_slot(at(9, 29, 59), set()) is None
    assert job.due_slot(at(9, 30), set()) == (at(9, 30), "quotes:2026-10-20:0930")
    assert job.due_slot(at(9, 34, 59), set()) == (at(9, 30), "quotes:2026-10-20:0930")
    assert job.due_slot(at(9, 35), set()) is None  # past the window: a late quote is not the 09:30 snapshot
    assert job.due_slot(at(15, 27), set()) == (at(15, 25), "quotes:2026-10-20:1525")
    assert job.due_slot(at(12, 1), {TUE}) is None  # NSE holiday
    assert job.due_slot(at(12, 1, d=date(2026, 10, 24)), set()) is None  # Saturday


# ------------------------------------------------------------------ a whole sweep against a mocked Groww
class GrowwSweep:
    """Answers the instruments file, the index LTP and Get Quote; `fail` symbols answer 500."""

    def __init__(self, clock_t: datetime, fail: tuple[str, ...] = (), ltp_ok: bool = True) -> None:
        self.t, self.fail, self.ltp_ok = clock_t, fail, ltp_ok
        self.requests: list[httpx.Request] = []
        self.csv = instruments_csv()

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.url.host == "growwapi-assets.groww.in":
            return httpx.Response(200, content=self.csv.encode())
        if path == "/v1/live-data/ltp" and self.ltp_ok:
            return httpx.Response(200, json={"status": "SUCCESS", "payload": {"NSE_NIFTY": 25012.4,
                                                                              "NSE_BANKNIFTY": 56040.0}})  # fmt: skip
        if path == "/v1/live-data/quote" and request.url.params["trading_symbol"] not in self.fail:
            ltt = int((self.t - timedelta(seconds=5)).timestamp() * 1000)
            return httpx.Response(200, json={"status": "SUCCESS", "payload": quote_payload(ltt_ms=ltt)})
        return httpx.Response(500, json={"status": "FAILURE", "error": {"message": "down"}})

    def calls(self, path: str) -> int:
        return sum(1 for r in self.requests if r.url.path == path)


@pytest.fixture
def sweep_groww(monkeypatch, qdb):
    class NoWait:
        def reserve(self, key, interval):
            return 0.0

        def hold_off(self, key, seconds):
            pass

    monkeypatch.setattr(groww_budget.BUDGET, "slots", NoWait())

    def install(t: datetime = AT_0930, **kw) -> GrowwSweep:
        g = GrowwSweep(t, **kw)
        monkeypatch.setattr(base, "TRANSPORT", httpx.MockTransport(g))
        return g

    return install


def _slot(slot: str) -> dict:
    from finresearch.db import session_scope
    from finresearch.db.models import AlertEvalSlot

    with session_scope() as s:
        row = s.get(AlertEvalSlot, slot)
        return dict(row.result) if row else {}


async def test_sweep_records_168_quotes_once_per_slot(sweep_groww):
    from finresearch.fnohist import quotes as q
    from finresearch.monitor import quotes as job

    g = sweep_groww()
    res = await job.quotes_step(AT_0930, market=market(), holidays=set(), spacing_s=0)
    assert res["quotes"]["stored"] == 168
    assert g.calls("/v1/live-data/quote") == 168 and g.calls("/v1/live-data/ltp") == 1
    assert {r.method for r in g.requests} == {"GET"}
    snap = datetime.combine(TUE, time(9, 30), IST)
    rows = q.load("NIFTY", snap, snap)
    assert len(rows) == 84 and rows[0].meta["source"] == "groww" and rows[0].meta["underlying_ltp"] == 2501240
    r = _slot("quotes:2026-10-20:0930")
    assert r["status"] == "done" and r["checks"]["ok"] is True and r["checks"]["rows"] == 168
    assert await job.quotes_step(AT_0930 + timedelta(seconds=60), market=market(), holidays=set(), spacing_s=0) == {}
    assert g.calls("/v1/live-data/quote") == 168  # claimed: never recorded twice


async def test_a_failed_contract_is_a_missing_row_and_fails_the_checks(sweep_groww):
    from finresearch.monitor import quotes as job

    sweep_groww(fail=("NIFTY26102025000CE",))
    res = await job.quotes_step(AT_0930, market=market(), holidays=set(), spacing_s=0)
    assert res["quotes"]["stored"] == 167
    r = _slot("quotes:2026-10-20:0930")
    assert r["status"] == "done" and r["checks"]["rows_ok"] is False and r["checks"]["ok"] is False
    assert r["failed"] == ["NIFTY26102025000CE"]


async def test_no_groww_session_fails_the_slot_without_nse_unless_switched_on(sweep_groww):
    from finresearch.monitor import quotes as job

    g = sweep_groww()
    calls: list = []

    async def nse_chain(sym, exp):
        calls.append((sym, exp))
        raise AssertionError("not called")

    res = await job.quotes_step(AT_0930, market=market(token=None), holidays=set(), spacing_s=0)
    assert res == {"quotes": "failed"} and calls == [] and g.calls("/v1/live-data/quote") == 0
    r = _slot("quotes:2026-10-20:0930")
    assert r["status"] == "failed" and "Groww" in r["error"]


def nse_chain_fake(calls: list):
    from finresearch.adapters.nse_fno import ChainRow, OptionChain, OptionQuote

    async def chain(sym: str, exp: date) -> OptionChain:
        calls.append((sym, exp))
        spot, lo, hi, step = (Decimal("25012.4"), 24000, 26000, 50) if sym == "NIFTY" else \
            (Decimal("56040"), 54000, 58000, 100)  # fmt: skip
        oq = OptionQuote(last_price=Decimal("120.5"), iv=Decimal("12.3"), oi=Decimal(1000), change_in_oi=None,
                         volume=Decimal(50), bid=Decimal("120.05"), ask=Decimal("121"))  # fmt: skip
        rows = [ChainRow(strike=Decimal(k), call=oq, put=oq) for k in range(lo, hi + 1, step)]
        return OptionChain(symbol=sym, expiry=exp, underlying=spot, as_of=AT_0930 - timedelta(seconds=20), rows=rows)

    return chain


async def test_nse_fallback_is_capped_at_four_chains_a_sweep_and_sixteen_a_day(sweep_groww):
    from finresearch.fnohist import quotes as q
    from finresearch.monitor import quotes as job

    g = sweep_groww()
    calls: list = []
    for t in q.SNAPSHOT_TIMES:
        now = datetime.combine(TUE, t, IST) + timedelta(seconds=5)
        res = await job.quotes_step(now, market=market(now, token=None), nse_chain=nse_chain_fake(calls),
                                    holidays=set(), spacing_s=0)  # fmt: skip
        assert res["quotes"]["source"] == "nse" and res["quotes"]["stored"] == 168
    assert len(calls) == 16 and g.calls("/v1/live-data/quote") == 0
    assert set(calls) == {("NIFTY", date(2026, 10, 20)), ("NIFTY", date(2026, 10, 27)),
                          ("BANKNIFTY", date(2026, 10, 28)), ("BANKNIFTY", date(2026, 11, 25))}  # fmt: skip
    snap = datetime.combine(TUE, time(9, 30), IST)
    (row,) = [r for r in q.load("NIFTY", snap, snap) if r.meta["strike"] == 2500000 and r.meta["option_type"] == "CE"
              and r.meta["expiry"] == date(2026, 10, 20)]  # fmt: skip
    assert row.meta["source"] == "nse" and row.bid_qty is None and row.depth_ask == []
    assert row.mid == Decimal("120.525")
    r = _slot("quotes:2026-10-20:0930")
    assert r["source"] == "nse" and r["nse_requests"] == 4 and "no valid Groww session" in r["error"]
    assert q.day_checks(TUE)["rows"] == 672

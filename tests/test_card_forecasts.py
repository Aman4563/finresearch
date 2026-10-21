"""P2.1 (#376): every logged F&O card becomes a forecast of "P&L per set after costs > 0 under the card's exit plan",
resolved through the backtest engine's exit simulation on the stored bhavcopy (evals/experiments/card_ledger/PREREG.md).
Synthetic cards and history only; expected P&L worked out here by hand from the stated rates, not by the code."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from finresearch.backtest.history import Day, MemoryHistory, Quote
from finresearch.signals.ledger import NotYet

D0, EXP, LOT = date(2025, 1, 10), date(2025, 2, 13), 75  # 34 days: a monthly expiry
LOGGED = datetime(2025, 1, 10, 6, 0, tzinfo=UTC)  # 11:30 IST on D0


def charges(sell_value: float, buy_value: float, orders: int, stt_sell: float = 0.001) -> float:
    """fincalc.charges' rule written out (test_backtest_engine.charges): STT 0.1 % on option sells, NSE 0.03553 % +
    SEBI Rs 10/crore both sides, stamp 0.003 % on buys, Rs 20 a order, GST 18 % on brokerage + exchange + SEBI."""
    turnover = sell_value + buy_value
    exch, sebi, brokerage = turnover * 0.0003553, turnover * 0.000001, 20.0 * orders
    return (
        sell_value * stt_sell
        + exch
        + sebi
        + buy_value * 0.00003
        + brokerage
        + 0.18 * (brokerage + exch + sebi)
    )


def card(legs, *, kind, suggested=2, source="rule of thumb", profit=50.0, stop=200.0, mids=True, **extra):
    """A trade-plan card shaped like signals.tradeplan.build's response (only the fields the ledger reads), sized at
    `suggested` sets: its legs carry lots = ratio x suggested."""
    return {
        "title": f"NIFTY {EXP:%d-%b-%Y} · {kind}", "symbol": "NIFTY", "expiry": EXP.isoformat(),
        "days_to_expiry": (EXP - D0).days, "spot": 20000.0, "lot_size": LOT, "kind": kind, "action": "WAIT",
        "qualifier": None, "label": "WAIT", "failed": ["Proven edge"],
        "legs": [{"right": r, "strike": k, "side": s, "lots": n * suggested, "qty": n * suggested * LOT, "last": px + 1}
                 for r, k, s, n, px in legs],
        "size": {"multiple": 1, "suggested": suggested, "capital": 999999, "budget": 12345.0},
        "entry": {"status": "ok" if mids else "unknown", "levels_priced_at": "bid/ask mid (groww)" if mids else
                  "last traded price (no live bid/ask)",
                  "legs": [{"mid": px if mids else None} for *_, px in legs]},
        "exits": {"source": source, "profit_pct": profit, "stop_pct": stop},
        "probability": {"real_world": 0.71, "risk_neutral": 0.66},
        "ev": {"real_world": 400.0 * suggested, "risk_neutral": 200.0 * suggested},
        "pnl_dots": {"model": "real-world 20-day scenario", "values": [-2000.0 * suggested, 800.0 * suggested],
                     "payoff_ratio": 0.4},
        "evidence": {"level": "untested", "match": "related", "spec_key": None},
        **extra,
    }  # fmt: skip


PCS = [("put", 20000.0, "sell", 1, 100.0), ("put", 19800.0, "buy", 1, 60.0)]


def day(d: date, marks: dict[float, float], *, settle_all: float | None = None, fut: float | None = None,
        contracts: int = 1500, right: str = "put") -> Day:  # fmt: skip
    x = Day(d, "NIFTY")
    if fut is not None:
        x.futures[EXP] = fut
    for k, px in marks.items():
        x.options[(EXP, right, k)] = Quote(EXP, right, k, px, settle_all or px, contracts, 1000)
    return x


def test_inputs_are_per_set_market_data_only():
    from finresearch.fnobook import card_forecasts as cf

    got = cf.card_inputs(card(PCS, kind="put_credit"), LOGGED, card_log_id=7, index=0)
    assert [(lg["strike"], lg["side"], lg["lots"], lg["entry"]) for lg in got["legs"]] == [
        (20000.0, "sell", 1, 100.0),
        (19800.0, "buy", 1, 60.0),
    ]  # per set, at the mids
    assert got["entry_day"] == "2025-01-10" and got["start_date"] == "2025-01-10" and got["lot"] == LOT
    # the rule of thumb for a monthly credit trade: +50 % / -200 % of the credit, out 7 days before expiry
    assert got["exit_plan"] == {"profit_pct": 50.0, "stop_pct": 200.0, "exit_dte": 7, "hold_to_expiry": False}
    assert got["ev"] == {"real_world": 400.0, "risk_neutral": 200.0}  # per set
    assert got["pnl_dots"]["values"] == [-2000.0, 800.0]
    assert (got["card_log_id"], got["card_index"], got["verdict"]) == (7, 0, "WAIT")
    blob = repr(got)
    assert "999999" not in blob and "12345" not in blob, (
        "the profile's capital and budget never enter the ledger"
    )


def test_waiting_cards_and_mismatched_exit_plans():
    from finresearch.fnobook import card_forecasts as cf

    assert (
        cf.card_inputs(card([], kind="put_credit"), LOGGED, card_log_id=1, index=0) is None
    )  # no legs: no trade
    odd = cf.card_inputs(card(PCS, kind="put_credit", stop=150.0), LOGGED, card_log_id=1, index=0)
    assert odd["exit_plan"] is None and "exit plan" in odd["unknown"]  # never guessed
    last = cf.card_inputs(card(PCS, kind="put_credit", mids=False), LOGGED, card_log_id=1, index=0)
    assert [lg["entry"] for lg in last["legs"]] == [101.0, 61.0]  # no live bid/ask: the last traded prices


def _inputs(c):
    from finresearch.fnobook import card_forecasts as cf

    return cf.card_inputs(c, LOGGED, card_log_id=1, index=0)


STOP_PATH = [
    day(D0, {20000.0: 100.0, 19800.0: 60.0}),  # the entry day itself is never managed
    day(date(2025, 1, 13), {20000.0: 120.0, 19800.0: 70.0}),  # gross -1,500 + 750 = -750
    day(date(2025, 1, 14), {20000.0: 200.0, 19800.0: 100.0}),  # -7,500 + 3,000 = -4,500 > -6,000
    day(date(2025, 1, 15), {20000.0: 260.0, 19800.0: 115.0}),  # -12,000 + 4,125 = -7,875 <= -2 x 3,000: stop
    day(date(2025, 1, 16), {20000.0: 90.0, 19800.0: 50.0}),
]


def test_a_stop_resolves_with_the_hand_computed_net_pnl_per_set():
    from finresearch.fnobook import card_forecasts as cf

    r = cf.simulate(_inputs(card(PCS, kind="put_credit")), MemoryHistory(STOP_PATH))
    # exits pay the engine's 1 % slippage tier (1,500 contracts): buy back 260 -> 262.60, sell 115 -> 113.85
    gross = -(262.60 - 100.0) * LOT + (113.85 - 60.0) * LOT  # -8,156.25
    costs = charges(100.0 * LOT, 60.0 * LOT, 2) + charges(113.85 * LOT, 262.60 * LOT, 2)
    assert r.outcome == 0 and r.value == pytest.approx(gross - costs, abs=0.02)
    assert "stop" in r.note and "2025-01-15" in r.note


def test_a_hold_to_expiry_recipe_settles_with_exercise_stt():
    from finresearch.fnobook import card_forecasts as cf

    dcs = [("call", 20000.0, "buy", 1, 300.0), ("call", 20400.0, "sell", 1, 150.0)]
    c = card(dcs, kind="debit_call", source="spec:nifty_dcs_m", profit=None, stop=None)
    inp = _inputs(c)
    assert inp["exit_plan"] == {
        "profit_pct": None,
        "stop_pct": None,
        "exit_dte": None,
        "hold_to_expiry": True,
    }
    path = [day(date(2025, 1, 13), {20000.0: 250.0, 20400.0: 120.0}, right="call"),
            day(EXP, {20000.0: 300.0, 20400.0: 1.0}, settle_all=20300.0, fut=20300.0, right="call")]  # fmt: skip
    r = cf.simulate(inp, MemoryHistory(path))
    gross = (300.0 - 300.0) * LOT - (
        0.0 - 150.0
    ) * LOT  # long 20000 call worth 300 at 20,300; short wing expires
    stt = (
        300.0 * LOT * 0.00125
    )  # exercise STT 0.125 % on the long's intrinsic value (Finance (No. 2) Act 2024)
    net = gross - charges(150.0 * LOT, 300.0 * LOT, 2) - stt
    assert r.outcome == 1 and r.value == pytest.approx(net, abs=0.02) and "expiry" in r.note


def test_an_open_trade_with_no_later_session_stored_is_not_yet():
    from finresearch.fnobook import card_forecasts as cf

    with pytest.raises(NotYet):
        cf.simulate(_inputs(card(PCS, kind="put_credit")), MemoryHistory(STOP_PATH[:3]))
    # the stop fired on the LAST stored session: whether that session was the last before expiry day is not yet
    # known from the data, so the result waits a session rather than trust a possibly wrong exit
    with pytest.raises(NotYet):
        cf.simulate(_inputs(card(PCS, kind="put_credit")), MemoryHistory(STOP_PATH[:4]))


def test_a_contract_missing_from_the_stored_bhavcopy_voids_never_zero():
    from finresearch.fnobook import card_forecasts as cf

    path = [day(x.day, {20000.0: x.options[(EXP, "put", 20000.0)].close}) for x in STOP_PATH]  # no 19800 rows
    r = cf.simulate(_inputs(card(PCS, kind="put_credit")), MemoryHistory(path))
    assert r.outcome is None and r.value is None and "19800" in r.note


def test_unknown_exit_plans_and_future_legs_void():
    from finresearch.fnobook import card_forecasts as cf

    r = cf.simulate(_inputs(card(PCS, kind="put_credit", stop=150.0)), MemoryHistory(STOP_PATH))
    assert r.outcome is None and "exit plan" in r.note
    fut = card([("future", 20050.0, "buy", 1, 20050.0)], kind="long_future", profit=None, stop=None)
    r = cf.simulate(_inputs(fut), MemoryHistory(STOP_PATH))
    assert r.outcome is None and "future" in r.note

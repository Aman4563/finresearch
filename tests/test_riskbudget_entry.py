"""P4.2 Stage 2 (#395): the replay's entry path is the registered engine's (backtest.engine.try_enter) split at its
sizing, so an arm can size the same candidate by the book's limits. Its oracle is the engine itself on the same close:
legs, fills, lots, premium, max loss, costs, margin and every refusal counted the same way."""

from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest

from finresearch.backtest import engine, specs
from finresearch.backtest.history import Clocked, Day, MemoryHistory, Quote
from finresearch.fincalc.fno_history import black76_price
from finresearch.riskbudget import entry

BOOK, R = specs.BOOK, 0.065
D0, EXP, F, VOL, LOT = date(2025, 1, 10), date(2025, 2, 13), 20000.0, 0.15, 75


def clk(x: Day) -> Clocked:
    c = Clocked(MemoryHistory([x]))
    c.advance(x.day)
    return c


def put_day(contracts: int = 500) -> Day:
    x = Day(D0, "NIFTY", spot=None, iv30=15.0)
    x.futures[EXP] = F
    t = (EXP - D0).days / 365
    for k in range(17600, 22500, 100):
        for right in ("put", "call"):
            p = round(black76_price(right, F, float(k), t, R, VOL), 2)
            x.options[(EXP, right, float(k))] = Quote(EXP, right, float(k), p, p, contracts, 10_000, LOT)
    return x


def both(spec, x: Day, ivs: list[float], book=BOOK) -> tuple[engine.SpecState, engine.SpecState, object]:
    a, b = engine.SpecState(spec), engine.SpecState(spec)
    want = engine.try_enter(a, x, "NIFTY", clk(x), ivs, book)
    c = entry.candidate(b, x, "NIFTY", clk(x), ivs, book)
    got = None
    if c is not None:
        fit = entry.fit_margin(c, c.n_trade, [], book)
        got = entry.place(b, c, *fit) if not isinstance(fit, str) else b.skips.update([fit])
    return a, b, (want, got)


@pytest.mark.parametrize("key", ["nifty_pcs_m", "nifty_ccs_m"])
def test_an_accepted_entry_is_the_engines(key) -> None:
    a, b, (want, got) = both(replace(specs.by_key(key), iv_pct_min=None), put_day(), [15.0])
    assert want is not None and got is not None
    for f in ("spec", "symbol", "expiry", "entry_day", "dte", "lots", "lot", "premium", "max_loss", "margin",
              "entry_costs", "iv30", "iv_pct"):  # fmt: skip
        assert getattr(got, f) == getattr(want, f), f
    assert [(g.right, g.strike, g.side, g.qty, g.entry, g.iv, g.mark) for g in got.legs] == [
        (w.right, w.strike, w.side, w.qty, w.entry, w.iv, w.mark) for w in want.legs
    ]
    assert b.open == [got] and b.last_entry == a.last_entry == D0
    assert b.entries_considered == a.entries_considered == 1 and b.skips == a.skips


@pytest.mark.parametrize(
    ("spec_kw", "ivs", "day_kw", "book_kw"),
    [
        ({}, [15.0], {}, {}),  # IV percentile unknown (one IV reading)
        ({"iv_pct_min": None, "dte_min": 40}, [15.0], {}, {}),  # no expiry in the DTE window: not considered
        ({"iv_pct_min": None}, [15.0], {"contracts": 5}, {}),  # no anchor traded with >= 10 contracts
        ({"iv_pct_min": None}, [15.0], {}, {"capital": 20_000.0}),  # one lot above the trade budget
    ],
)
def test_a_refusal_is_counted_as_the_engine_counts_it(spec_kw, ivs, day_kw, book_kw) -> None:
    spec = replace(specs.by_key("nifty_pcs_m"), **spec_kw)
    a, b, (want, got) = both(spec, put_day(**day_kw), ivs, replace(BOOK, **book_kw))
    assert want is None and got is None
    assert b.skips == a.skips and b.entries_considered == a.entries_considered and b.open == []


def test_the_whole_books_margin_counts_against_capital() -> None:
    # an open position of another spec using all but ₹1 of capital as margin leaves no room for even one lot
    st = engine.SpecState(replace(specs.by_key("nifty_pcs_m"), iv_pct_min=None))
    x = put_day()
    c = entry.candidate(st, x, "NIFTY", clk(x), [15.0], BOOK)
    other = engine.Position("banknifty_ic_m", "BANKNIFTY", EXP, D0, 34, 1, 30, [], 1.0, 1.0, BOOK.capital - 1, 0.0,
                            None, None)  # fmt: skip
    assert entry.fit_margin(c, c.n_trade, [other], BOOK) == "max loss or margin above the budget for one lot"
    unknown = replace(other, margin=None)
    assert entry.fit_margin(c, c.n_trade, [unknown], BOOK) == "margin of an open position unknown"

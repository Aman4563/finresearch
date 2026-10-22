"""Holdings-based Brinson-Fachler attribution by sector against the Nifty 50 (#348, portfolio.attribution): start
weights back-drifted from the end, constituent returns chained from the stored bhavcopy, unknown lines with reasons.
Every expected value is hand-computed in the comments; synthetic symbols only."""

from __future__ import annotations

from datetime import date
from decimal import Decimal as D

import pytest

from finresearch.portfolio import attribution as at

S0, E = date(2026, 1, 1), date(2026, 3, 31)


def row(day, close, prev):
    return (day, D(str(close)), D(str(prev)))


def test_constituent_return_chains_prev_close_through_an_adjusted_split():
    rows = [row(S0, 100, 99), row(date(2026, 1, 2), 110, 100),
            row(date(2026, 1, 5), 60, 55),  # 1:2 split ex 5-Jan: NSE's previous close adjusted 110 -> 55
            row(E, 66, 60)]  # fmt: skip
    r, why = at.constituent_return(rows, S0, E)
    assert why is None and r == pytest.approx(110 / 100 * 60 / 55 * 66 / 60 - 1)  # 1.32 - 1 = 0.32


def test_constituent_return_unknown_cases():
    split_unadjusted = [row(S0, 100, 99), row(date(2026, 1, 5), 55, 110), row(E, 60, 55)]
    r, why = at.constituent_return(split_unadjusted, S0, E)
    assert (
        r is None and why == "a one-day move of -50.0 % on 2026-01-05: probably an unadjusted split or bonus"
    )
    r, why = at.constituent_return([row(date(2026, 1, 2), 100, 99), row(E, 110, 100)], S0, E)
    assert r is None and why == "no stored close on 2026-01-01 (the window's first session)"
    r, why = at.constituent_return([row(S0, 100, 99)], S0, E)
    assert r is None and why == "no stored close on 2026-03-31 (the window's last session)"


def test_attribution_by_hand():
    # Benchmark: A (Financials) mcap at the end 660 × 100/110 = 600, +20 % -> 500 at the start; B (Financials) 300, 0 %
    # -> 300; C (IT) 300, +50 % -> 200. Start weights .5/.3/.2; Financials w .8, r 900/800 - 1 = .125; IT w .2, r .5.
    # R_b = .8 × .125 + .2 × .5 = .2
    bench = [at.Member("A", "A Ltd", "Financials", 660, 110, 100, 0.20),
             at.Member("B", "B Ltd", "Financials", 300, 50, 50, 0.0),
             at.Member("C", "C Ltd", "IT", 300, 30, 30, 0.50)]  # fmt: skip
    # Portfolio: A 300 at the end, +20 % -> 250 (Financials); X 165, +10 % -> 150 (Consumer, not in the index);
    # a fund 105, +5 % -> 100. Start weights .5/.3/.2; R_p = .1 + .03 + .01 = .14; active -.06
    port = [at.Holding("NSE:A", "A Ltd", "Financials", 300, 0.20), at.Holding("NSE:X", "X Ltd", "Consumer", 165, 0.10),
            at.Holding("MF:1", "Fund", "Funds (no look-through)", 105, 0.05)]  # fmt: skip
    out = at.attribute(port, bench)
    t = out["totals"]
    assert (t["portfolio_return"], t["benchmark_return"], t["active"]) == pytest.approx((0.14, 0.2, -0.06))
    lines = {x["segment"]: x for x in out["segments"]}
    # Financials: A = (-.3)(.125 - .2) = .0225, S = .8(.2 - .125) = .06, I = (-.3)(.075) = -.0225
    f = lines["Financials"]
    assert (f["wp"], f["wb"], f["rp"], f["rb"]) == pytest.approx((0.5, 0.8, 0.2, 0.125))
    assert (f["allocation"], f["selection"], f["interaction"]) == pytest.approx((0.0225, 0.06, -0.0225))
    assert lines["IT"]["allocation"] == pytest.approx(-0.06)  # (0 - .2)(.5 - .2), not held
    assert lines["Consumer"]["allocation"] == pytest.approx(-0.03)  # .3(.1 - .2), off the benchmark
    assert lines["Funds (no look-through)"]["allocation"] == pytest.approx(-0.03)  # .2(.05 - .2)
    assert t["allocation"] + t["selection"] + t["interaction"] == pytest.approx(t["active"])
    assert out["complete"] is True and out["coverage"] == pytest.approx(1.0)


def test_missing_benchmark_weight_or_return_is_unknown_never_zero():
    bench = [at.Member("A", "A Ltd", "Financials", None, 110, 100, 0.2, reason="NSE gave no free-float market cap"),
             at.Member("C", "C Ltd", "IT", 300, 30, 30, 0.5)]  # fmt: skip
    out = at.attribute([at.Holding("NSE:C", "C Ltd", "IT", 100, 0.5)], bench)
    assert (
        out["complete"] is False and out["totals"]["active"] is None and out["totals"]["allocation"] is None
    )
    lines = {x["segment"]: x for x in out["segments"]}
    assert lines["Financials"]["wb"] is None and lines["Financials"]["allocation"] is None
    assert "A: NSE gave no free-float market cap" in lines["Financials"]["reason"]
    assert out["benchmark_unknown"] == [
        {"symbol": "A", "name": "A Ltd", "reason": "NSE gave no free-float market cap"}
    ]


def test_holding_without_a_return_is_left_out_and_counted():
    bench = [at.Member("C", "C Ltd", "IT", 300, 30, 30, 0.5)]
    port = [at.Holding("NSE:C", "C Ltd", "IT", 150, 0.5),
            at.Holding("NSE:N", "New Ltd", "IT", 50, None, reason="listed after the window began")]  # fmt: skip
    out = at.attribute(port, bench)
    # only C counts: R_p = .5 = R_b, all effects 0; coverage 150 / 200 of the end value
    assert out["coverage"] == pytest.approx(0.75) and out["complete"] is False
    assert out["excluded"] == [{"key": "NSE:N", "name": "New Ltd", "reason": "listed after the window began"}]
    assert out["totals"]["active"] == pytest.approx(0.0)


# --------------------------------------------------------------------------- reading the stored bhavcopy (test DB)
def _store(
    days_closes: dict[str, list[tuple[date, float, float]]], loaded: list[date], *, no_file=(), nifty=None
):
    from sqlalchemy import text

    from finresearch.db import session_scope
    from finresearch.db.models import EquityEod, EquityEodDay, IndexEod

    with session_scope() as s:
        s.execute(text("TRUNCATE equity_eod, equity_eod_day, index_eod"))
        for d in loaded:
            s.add(EquityEodDay(day=d, status="loaded", rows=1))
        for d in no_file:
            s.add(EquityEodDay(day=d, status="no_file"))
        for sym, rows in days_closes.items():
            for d, c, p in rows:
                s.add(EquityEod(day=d, symbol=sym, series="EQ", close=D(str(c)), prev_close=D(str(p))))
        for d, c in (nifty or {}).items():
            s.add(IndexEod(day=d, symbol="NIFTY", index_name="Nifty 50", close=D(str(c))))


WEEK = [date(2026, 3, 2), date(2026, 3, 3), date(2026, 3, 4), date(2026, 3, 5), date(2026, 3, 6)]  # Mon-Fri


def test_window_needs_every_weekday_accounted_for(env):
    from finresearch.db import session_scope

    loaded = [d for d in WEEK if d != date(2026, 3, 4)]
    _store({}, loaded, no_file=[date(2026, 3, 4)])  # a holiday: no file, not a gap
    with session_scope() as s:
        w = at.window(s, date(2026, 3, 6), days=4)
    assert (w.start, w.end, w.reason) == (date(2026, 3, 2), date(2026, 3, 6), None)
    _store({}, loaded)  # 4-Mar never tried: a gap
    with session_scope() as s:
        w = at.window(s, date(2026, 3, 6), days=4)
    assert (
        w.reason == "the bhavcopy for 2026-03-04 is not stored, so returns over the window would skip a day"
    )
    _store({}, WEEK[2:])
    with session_scope() as s:
        w = at.window(s, date(2026, 3, 6), days=5)
    assert (
        w.reason
        == "the stored bhavcopy starts on 2026-03-04; this window needs a session on or before 2026-03-01"
    )


def test_members_from_the_store_and_the_index_check(env):
    from finresearch.adapters.nse_index_weights import IndexMember, IndexWeights
    from finresearch.db import session_scope

    _store({"AAA": [(WEEK[0], 100, 99), (WEEK[2], 110, 100), (WEEK[4], 121, 110)],
            "BBB": [(WEEK[2], 50, 49), (WEEK[4], 50, 50)]},
           WEEK, nifty={WEEK[0]: 20000, WEEK[4]: 22000})  # fmt: skip
    iw = IndexWeights("NIFTY 50", None, "https://example.invalid/w", None,
                      [IndexMember("AAA", "AAA Ltd", "INE000A01011", "Financial Services", 1210.0, 121.0),
                       IndexMember("BBB", "BBB Ltd", "INE000B01011", "Information Technology", 500.0, 50.0),
                       IndexMember("CCC", "CCC Ltd", "INE000C01011", "Information Technology", None, 10.0)])  # fmt: skip
    with session_scope() as s:
        ms = {m.symbol: m for m in at.members(s, iw, WEEK[0], WEEK[4])}
        idx = at.index_return(s, WEEK[0], WEEK[4])
    assert ms["AAA"].ret == pytest.approx(0.21) and ms["AAA"].mcap_end == pytest.approx(1210.0)
    assert (
        ms["BBB"].ret is None
        and ms["BBB"].reason == "no stored close on 2026-03-02 (the window's first session)"
    )
    assert ms["CCC"].ffmc is None and ms["CCC"].reason == "NSE gave no free-float market cap"
    assert idx == pytest.approx(0.10)  # 22,000 / 20,000 - 1


def test_holding_return_over_the_window():
    closes = {
        date(2026, 2, 27): 100.0,
        date(2026, 3, 3): 110.0,
        date(2026, 3, 4): 55.0,
        date(2026, 3, 6): 60.5,
    }
    # 1:2 split ex 4-Mar recorded (factor 2): 110 / 100 × (55 × 2) / 110 × 60.5 / 55 - 1 = 0.21; the window starts on
    # 2-Mar, whose price is the 27-Feb close (the last one on or before it)
    close_end, r, why = at.holding_return(closes, {date(2026, 3, 4): 2.0}, date(2026, 3, 2), date(2026, 3, 6))
    assert (close_end, why) == (60.5, None) and r == pytest.approx(0.21)
    _, r, why = at.holding_return(closes, {}, date(2026, 3, 2), date(2026, 3, 6))  # the split not recorded
    assert r is None and why == "a one-day move beyond ±40 % with no recorded split or bonus"
    _, r, why = at.holding_return(
        {date(2026, 3, 5): 10.0, date(2026, 3, 6): 11.0}, {}, date(2026, 3, 2), date(2026, 3, 6)
    )
    assert r is None and why == "no price on or within 7 days before 2026-03-02 (bought or listed later?)"

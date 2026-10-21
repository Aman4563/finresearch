"""#359 (P0.9): time to expiry is minutes from now to 15:30 IST on the expiry day / (365 x 1,440), at every live site.
Before, T = max(calendar days, 0.5) / 365, so a chain read at 09:20 and at 15:25 on expiry day priced identically."""

from __future__ import annotations

import json
import math
import re
from datetime import date, datetime
from pathlib import Path

import pytest
from test_fno_signal import FNO, chain

from finresearch.adapters.nse_fno import parse_index_history
from finresearch.fincalc import dates
from finresearch.fincalc import options as o
from finresearch.signals import fno as fs

SRC = Path(__file__).parent.parent / "src" / "finresearch"
FLOOR = re.compile(r"max\([^()]*(\([^()]*\)[^()]*)*,\s*0\.5\s*\)\s*/\s*365|0\.5\s*/\s*365")
IST = dates.IST


def test_no_source_floors_time_to_expiry_at_half_a_day():
    hits = []
    for p in SRC.rglob("*.py"):
        for i, line in enumerate(p.read_text().splitlines(), 1):
            if FLOOR.search(line):
                hits.append(f"{p.relative_to(SRC)}:{i}: {line.strip()}")
    assert hits == []


def test_the_pattern_catches_the_old_forms():
    for bad in ("t = max(days, 0.5) / 365", "max((expiry - today).days, 0.5) / 365", "max(dte, 0.5)/365"):
        assert FLOOR.search(bad), bad
    assert not FLOOR.search("t = (expiry - x.day).days / 365")  # the backtest's unfloored EOD dte/365 stays


def test_years_to_expiry_counts_minutes_to_1530_ist():
    e = date(2026, 10, 6)
    assert dates.years_to_expiry(e, datetime(2026, 10, 6, 9, 20, tzinfo=IST)) == pytest.approx(370 / 525_600)
    assert dates.years_to_expiry(e, datetime(2026, 10, 6, 15, 25, tzinfo=IST)) == pytest.approx(5 / 525_600)
    assert dates.years_to_expiry(e, datetime(2026, 10, 6, 15, 30, tzinfo=IST)) == 0
    assert dates.years_to_expiry(e, datetime(2026, 10, 6, 16, 0, tzinfo=IST)) < 0  # expired: never floored
    # 29-Sep 10:00 IST → 06-Oct 15:30 IST: 7 days 5.5 hours
    assert dates.years_to_expiry(e, datetime(2026, 9, 29, 10, 0, tzinfo=IST)) == pytest.approx(
        (7 + 5.5 / 24) / 365
    )
    # a UTC clock is converted, not read as IST wall time
    from datetime import UTC

    assert dates.years_to_expiry(e, datetime(2026, 10, 6, 9, 45, tzinfo=UTC)) == pytest.approx(
        15 / 525_600
    )  # 15:15 IST


def test_the_c4_straddle_reprices_with_the_clock():
    # bug report c4: a 13% ATM straddle at 24,000 on expiry day. T = 0.5/365 priced it at 92.2 all day; by the clock it is
    # about 66.1 at 09:20 and 7.7 at 15:25 (ATM straddle ≈ 0.798·S·σ·√T, here priced exactly with Black-Scholes)
    def straddle(t):
        return o.bs_price("call", 24000, 24000, t, 0.0, 0.13) + o.bs_price("put", 24000, 24000, t, 0.0, 0.13)

    e = date(2026, 10, 6)
    assert straddle(0.5 / 365) == pytest.approx(92.2, abs=0.1)
    assert straddle(dates.years_to_expiry(e, datetime(2026, 10, 6, 9, 20, tzinfo=IST))) == pytest.approx(
        66.1, abs=0.1
    )
    assert straddle(dates.years_to_expiry(e, datetime(2026, 10, 6, 15, 25, tzinfo=IST))) == pytest.approx(
        7.7, abs=0.1
    )
    assert 0.798 * 24000 * 0.13 * math.sqrt(370 / 525_600) == pytest.approx(
        66.1, abs=0.2
    )  # the closed-form check


def _closes():
    return [float(c) for _, c in parse_index_history(json.loads(
        (FNO / "indices_history_NIFTY50_202606_202609.json").read_text()))]  # fmt: skip


def test_an_expiry_day_chain_at_0920_and_1525_gives_different_t_pop_and_move():
    # an ATM bull call spread at the fixture's (7-day) prices: a far condor's PoP is ~1 and a straddle's ~0 at any T
    legs = fs.preset_legs(chain(), "bull_call")
    got = {}
    for hh, mm in ((9, 20), (15, 25)):
        now = datetime(2026, 10, 6, hh, mm, tzinfo=IST)
        got[hh] = fs.analyse(chain(), 65, legs, today=date(2026, 10, 6), now=now, closes=_closes(), iv_series=[],
                             capital=1_000_000, max_loss_pct=2, brokerage=20)  # fmt: skip
    a, b = got[9], got[15]
    assert a["t_years"] == pytest.approx(370 / 525_600, abs=1e-6) and b["t_years"] == pytest.approx(
        5 / 525_600, abs=1e-6
    )
    iv = a["vols"]["atm_iv"]
    assert a["expected_move"]["move"] == pytest.approx(a["spot"] * iv * math.sqrt(370 / 525_600), rel=1e-3)
    assert b["expected_move"]["move"] == pytest.approx(b["spot"] * iv * math.sqrt(5 / 525_600), rel=1e-3)
    assert a["risk_neutral"]["pop"] > 0.01 and b["risk_neutral"]["pop"] < a["risk_neutral"]["pop"] / 10


def test_analyse_without_a_clock_uses_the_close_of_today():
    # a date-only call (tests, end-of-day work) is the backtest's convention: from 15:30 today, i.e. days / 365
    a = fs.analyse(chain(), 65, fs.preset_legs(chain(), "bull_call"), today=date(2026, 9, 29), closes=_closes(),
                   iv_series=[], capital=1_000_000, max_loss_pct=2, brokerage=20)  # fmt: skip
    assert a["t_years"] == pytest.approx(7 / 365, abs=1e-6)


def test_an_expired_contract_is_refused_not_floored():
    with pytest.raises(ValueError, match="expired"):
        fs.analyse(chain(), 65, fs.preset_legs(chain(), "bull_call"), today=date(2026, 10, 6),
                   now=datetime(2026, 10, 6, 15, 31, tzinfo=IST), closes=_closes(), iv_series=[],
                   capital=1_000_000, max_loss_pct=2, brokerage=20)  # fmt: skip


def test_the_book_deltas_use_the_same_clock():
    # fnobook's delta exit rule: a short 22800 put (ATM-ish) on expiry day, IV from the chain. By the clock, its |delta|
    # at 15:25 is far from its 09:20 value; with T floored at half a day they were equal.
    from types import SimpleNamespace

    from finresearch.fnobook import service

    c = chain()
    leg = SimpleNamespace(side="sell", right="put", strike=22800, last_price=50.0)
    st = SimpleNamespace(expiry=date(2026, 10, 6), legs=[leg])
    d = {hh: service.paper_short_deltas(st, c, date(2026, 10, 6), 0.065, now=datetime(2026, 10, 6, hh, mm, tzinfo=IST))
         for hh, mm in ((9, 20), (15, 25))}  # fmt: skip
    iv = float(next(r for r in c.rows if float(r.strike) == 22800).put.iv) / 100
    for hh, mins in ((9, 370), (15, 5)):
        want = o.greeks("put", float(c.underlying), 22800, mins / 525_600, 0.065, iv).delta
        assert d[hh] == [pytest.approx(want, abs=1e-9)]
    # after the close the contract has expired: the delta is unknown, never a floored value
    after = service.paper_short_deltas(
        st, c, date(2026, 10, 6), 0.065, now=datetime(2026, 10, 6, 15, 45, tzinfo=IST)
    )
    assert after == [None]


def test_the_margin_estimate_uses_the_clock_and_refuses_an_expired_basket():
    # api/fno_book's /api/fno/margin passes T to fincalc.margin_approx, which floored it at half a day: a short ATM
    # straddle's scan risk was the same at 09:20 and 15:25 on expiry day
    from finresearch.fincalc import margin_approx as ma

    legs = [ma.MarginLeg("call", 24000, -75, 60.0, 0.13), ma.MarginLeg("put", 24000, -75, 60.0, 0.13)]
    early = ma.estimate(legs, 24000, 370 / 525_600, 0.065, "index")
    late = ma.estimate(legs, 24000, 5 / 525_600, 0.065, "index")
    assert early.scan_risk != pytest.approx(late.scan_risk, rel=1e-3)
    with pytest.raises(ValueError, match="expired"):
        ma.estimate(legs, 24000, 0.0, 0.065, "index")

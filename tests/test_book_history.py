"""The historical scan, ES97.5, multi-week episodes, gap and expiry-day scenarios of the F&O book (#394,
fincalc.book_history). Synthetic series only. Expected values are computed in the tests by hand arithmetic or
statistics.NormalDist, never by calling the code under test."""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta

import pytest

from finresearch.fincalc import book_history as bh
from finresearch.fincalc import book_scenarios as bs
from finresearch.fincalc.beta import BetaEstimate
from finresearch.fincalc.dates import IST

NOW = datetime(2026, 10, 22, 15, 30, tzinfo=IST)  # a Thursday, at the close
EXP = date(2026, 11, 26)  # 35 days later
F0 = 24000.0
R = 0.065


def _weekdays(start: date, n: int) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _series(rets: list[float], ivs: list[float] | None = None, start: date = date(2024, 1, 1)) -> bh.Series:
    """Session i's IV30 and the return from session i-1 to i (rets[0] is unused: session 0 has no return)."""
    cal = _weekdays(start, len(rets))
    ivs = ivs or [15.0] * len(rets)
    return bh.Series({cal[i]: (cal[i - 1], rets[i]) for i in range(1, len(rets))},
                     {cal[i]: ivs[i] for i in range(len(rets))})  # fmt: skip


def _mkt(betas: dict[str, BetaEstimate] | None = None, fwd: dict | None = None) -> bs.Market:
    return bs.Market(NOW, R, fwd or {("NIFTY", EXP): F0}, betas or {}, 23900.0)


def _fut(u: str = "NIFTY", q: int = 1) -> bs.BookLeg:
    return bs.BookLeg(u, EXP, "future", 0.0, q, None, None)


def _roll(days: int) -> float:
    return math.exp(-R * days / 365)


RETS = [0.0] + [0.001 * ((i * 37) % 61 - 30) for i in range(1, 301)]  # 300 one-session returns, -3 % .. +3 %


def test_es_and_99th_loss_of_a_futures_book_by_hand():
    # one long NIFTY future, h = 1 (Fri 23-Oct 15:30): P&L_i = F e^{r_i} e^{-r/365} - F
    sc = bh.scan([_fut()], _mkt(), {"NIFTY": _series(RETS)}, 1)
    pnl = sorted(F0 * math.exp(r) * _roll(1) - F0 for r in RETS[1:])
    assert sc.reasons == () and len(sc.days) == 300 and sc.skipped == 0
    # ES97.5 over 300 equal weights: 7.5 observations -> the worst 7 and half of the 8th
    assert sc.es == pytest.approx(-(sum(pnl[:7]) + 0.5 * pnl[7]) / 7.5, rel=1e-9)
    assert sc.loss_99 == pytest.approx(-pnl[2], rel=1e-9)  # inverted CDF: the 3rd smallest of 300
    assert [d.pnl for d in sc.worst] == pytest.approx(pnl[:10], rel=1e-9)
    assert len(sc.worst) == 10


def test_five_session_windows_compound_the_returns():
    sc = bh.scan([_fut()], _mkt(), {"NIFTY": _series(RETS)}, 5)
    cal = _weekdays(date(2024, 1, 1), 301)
    first = sc.days[0]
    assert (first.start, first.end) == (cal[0], cal[5])
    # 5 sessions after Thu 22-Oct = Thu 29-Oct: 7 calendar days of roll
    assert first.pnl == pytest.approx(F0 * math.exp(sum(RETS[1:6])) * _roll(7) - F0, rel=1e-9)
    assert len(sc.days) == 296


def test_a_missing_return_skips_every_window_over_it_and_says_so():
    ser = _series(RETS)
    cal = sorted(ser.iv30)
    gone = dict(ser.returns)
    del gone[cal[100]]
    sc = bh.scan([_fut()], _mkt(), {"NIFTY": bh.Series(gone, ser.iv30)}, 5)
    assert sc.skipped == 5 and len(sc.days) == 291
    assert all(not (d.start < cal[100] <= d.end) for d in sc.days)


def test_too_few_start_days_leave_es_unknown_with_the_reason():
    sc = bh.scan([_fut()], _mkt(), {"NIFTY": _series(RETS[:101])}, 1)
    assert len(sc.days) == 100 and sc.es is None and sc.loss_99 is None
    assert any(f"{bh.MIN_SCAN_DAYS}" in r for r in sc.es_reasons)


def test_an_unpriced_leg_makes_the_scan_unknown_never_zero():
    put = bs.BookLeg("NIFTY", EXP, "put", 23500.0, -75, None, None)
    sc = bh.scan([put], _mkt(), {"NIFTY": _series(RETS)}, 1)
    assert sc.days == () and sc.es is None and any("no price" in r for r in sc.reasons)


def test_no_stored_nifty_series_is_unknown():
    sc = bh.scan([_fut()], _mkt(), {}, 1)
    assert sc.es is None and any("NIFTY" in r for r in sc.reasons)


def _beta(u: str, b: float) -> BetaEstimate:
    return BetaEstimate(u, b, 0.0, b, b)


def test_an_underlying_moves_by_its_own_stored_move_when_it_has_one():
    fwd = {("NIFTY", EXP): F0, ("BANKNIFTY", EXP): 52000.0}
    bank = [0.0] + [-0.002] * 300
    sc = bh.scan([_fut("BANKNIFTY")], _mkt({"BANKNIFTY": _beta("BANKNIFTY", 1.2)}, fwd),
                 {"NIFTY": _series(RETS), "BANKNIFTY": _series(bank)}, 1)  # fmt: skip
    assert sc.days[0].pnl == pytest.approx(52000 * math.exp(-0.002) * _roll(1) - 52000, rel=1e-9)
    assert sc.days[0].mapped == ()


def test_without_its_own_move_an_underlying_takes_beta_times_nifty_and_is_counted():
    fwd = {("NIFTY", EXP): F0, ("BANKNIFTY", EXP): 52000.0}
    sc = bh.scan([_fut("BANKNIFTY")], _mkt({"BANKNIFTY": _beta("BANKNIFTY", 1.2)}, fwd),
                 {"NIFTY": _series(RETS)}, 1)  # fmt: skip
    mv = math.exp(RETS[1]) - 1
    assert sc.days[0].pnl == pytest.approx(52000 * (1 + 1.2 * mv) * _roll(1) - 52000, rel=1e-9)
    assert sc.days[0].mapped == ("BANKNIFTY",) and sc.mapped == {"BANKNIFTY": 300}


def test_without_its_own_move_or_a_beta_the_scan_is_unknown():
    fwd = {("NIFTY", EXP): F0, ("BANKNIFTY", EXP): 52000.0}
    sc = bh.scan([_fut("BANKNIFTY")], _mkt({}, fwd), {"NIFTY": _series(RETS)}, 1)
    assert sc.es is None and sc.days == () and any("BANKNIFTY" in r for r in sc.reasons)

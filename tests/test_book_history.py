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


def _mkt(betas: dict[str, BetaEstimate] | None = None, fwd: dict | None = None,
         carries: dict | None = None) -> bs.Market:  # fmt: skip
    """#480: every forward's carry is R (6.5 %) unless `carries` is given, so `_roll` is the roll at the carry."""
    fwd = fwd or {("NIFTY", EXP): F0}
    carries = {k: bs.Carry(R, "test carry 6.5 %") for k in fwd} if carries is None else carries
    return bs.Market(NOW, R, fwd, betas or {}, 23900.0, carries)


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


# --------------------------------------------------------------------------- multi-week episodes (daily paths)
FEB = bh.Episode("t", "test fall", date(2024, 2, 1), date(2024, 3, 29), "synthetic")


def _feb_series() -> bh.Series:
    """Weekdays from 2-Jan-2024: -1 % a session in February 2024 (21 weekdays), +1 % otherwise."""
    cal = _weekdays(date(2024, 1, 2), 80)
    rets = [0.0] + [(-0.01 if cal[i].month == 2 else 0.01) for i in range(1, 80)]
    return bh.Series({cal[i]: (cal[i - 1], rets[i]) for i in range(1, 80)}, {d: 15.0 for d in cal})


def test_an_episode_takes_its_worst_21_session_daily_path():
    ep = bh.episode([_fut()], _mkt(), {"NIFTY": _feb_series()}, FEB)
    assert ep.reasons == () and ep.start == date(2024, 1, 31)  # the last session before the window
    assert len(ep.path) == bh.EPISODE_H
    # 21 sessions after Thu 22-Oct-2026 is Fri 20-Nov: 29 calendar days of roll; all 21 sessions fell 1 %
    assert ep.worst.d == 21
    assert ep.worst.pnl == pytest.approx(F0 * math.exp(-0.21) * _roll(29) - F0, rel=1e-9)
    assert ep.path[0].pnl == pytest.approx(F0 * math.exp(-0.01) * _roll(1) - F0, rel=1e-9)


def test_a_leg_expiring_inside_the_episode_is_at_intrinsic():
    near = date(2026, 10, 29)  # 7 days: expires at session 5
    fwd = {("NIFTY", EXP): F0, ("NIFTY", near): 24010.0}
    call = bs.BookLeg("NIFTY", near, "call", 23800.0, 75, 260.0, "mid")
    ep = bh.episode([call], _mkt(fwd=fwd), {"NIFTY": _feb_series()}, FEB)
    p = ep.path[9]  # d = 10, after the expiry: F' = F (1 + move) e^{-b T}, valued at max(F' - K, 0)
    f1 = 24010.0 * math.exp(-0.10) * math.exp(-R * 7 / 365)
    assert p.d == 10 and p.pnl == pytest.approx((max(f1 - 23800, 0) - 260.0) * 75, rel=1e-9)
    assert p.intrinsic == 1


def test_an_episode_the_store_does_not_cover_is_unknown_with_the_reason():
    ep = bh.episode([_fut()], _mkt(), {"NIFTY": _series(RETS, start=date(2025, 1, 1))}, FEB)
    assert ep.worst is None and ep.path == () and any("does not cover" in r for r in ep.reasons)


def test_crisis_episodes_wait_for_the_vault_audit():
    keys = {e.key: e for e in bh.EPISODES}
    assert {"adani_2023", "febmar_2026", "oct_2008", "mar_2020"} <= set(keys)
    ep = bh.episode([_fut()], _mkt(), {"NIFTY": _series(RETS)}, keys["oct_2008"])
    assert ep.worst is None and any("vault" in r for r in ep.reasons)


# --------------------------------------------------------------------------- gap and expiry-day scenarios
GAPS = [0.0005 * ((i * 13) % 41 - 20) for i in range(300)]  # -1 % .. +1 %, 300 stored gaps


def test_gap_scenarios_reprice_now_at_the_stored_extremes():
    got = {g.label: g for g in bh.gap_scenarios([_fut()], _mkt(), GAPS)}
    srt = sorted(GAPS)
    for label, g in (("worst fall", srt[0]), ("1st percentile", srt[2]), ("99th percentile", srt[296]),
                     ("worst rise", srt[-1])):  # fmt: skip
        # at once (no time passes): F' = F e^{g}
        assert got[label].pnl == pytest.approx(F0 * math.exp(g) - F0, rel=1e-9, abs=1e-9)


def test_gap_scenarios_apply_the_spot_vol_response_to_options():
    from statistics import NormalDist

    nd = NormalDist()
    put = bs.BookLeg("NIFTY", EXP, "put", 23500.0, -75, 180.0, "mid")
    iv = bs.b76_iv("put", 180.0, F0, 23500.0, 35 / 365, R)
    g = min(GAPS)
    f1, v1 = F0 * math.exp(g), iv + 1.04 * (-(math.exp(g) - 1) * 100) / 100
    t = 35 / 365
    d1 = (math.log(f1 / 23500) + v1 * v1 * t / 2) / (v1 * math.sqrt(t))
    p1 = math.exp(-R * t) * (23500 * nd.cdf(-(d1 - v1 * math.sqrt(t))) - f1 * nd.cdf(-d1))
    got = next(x for x in bh.gap_scenarios([put], _mkt(), GAPS) if x.label == "worst fall")
    assert got.pnl == pytest.approx((p1 - 180.0) * -75, rel=1e-7)


def test_too_few_gaps_are_unknown():
    got = bh.gap_scenarios([_fut()], _mkt(), GAPS[:50])
    assert all(g.pnl is None and g.reasons for g in got)


def test_expiry_day_scenarios_value_the_book_at_the_nearest_expiry_close():
    rets = [0.001 * ((i * 7) % 61 - 30) for i in range(100)]
    got = {g.label: g for g in bh.expiry_day_scenarios([_fut()], _mkt(), rets)}
    mv = math.exp(min(rets)) - 1
    # at the expiry's close: F' = F (1 + move) e^{-b T} (b = R here), the future at intrinsic = F'
    assert got["worst fall"].pnl == pytest.approx(F0 * (1 + mv) * math.exp(-R * 35 / 365) - F0, rel=1e-9)
    assert got["worst fall"].at.date() == EXP


# --------------------------------------------------------------------------- #480: the roll at each expiry's carry
B = (
    0.0575 - 0.0124
)  # a dividend-paying NIFTY's carry r - q (r 5.75 %, dividend yield 1.24 %), not the 6.5 % rate
EXPIRY_RETS = [0.001 * ((i * 7) % 61 - 30) for i in range(100)]


def test_the_scan_rolls_each_window_at_the_expirys_carry_not_the_rate():
    sc = bh.scan(
        [_fut()], _mkt(carries={("NIFTY", EXP): bs.Carry(B, "test: r - q")}), {"NIFTY": _series(RETS)}, 1
    )
    # one long future, one session: F e^{r_1} e^{-b/365} - F; the rate's roll is 1.3 index points (24,000 x 1.99 %
    # / 365) away
    assert sc.days[0].pnl == pytest.approx(F0 * math.exp(RETS[1]) * math.exp(-B / 365) - F0, rel=1e-9)
    assert abs(sc.days[0].pnl - (F0 * math.exp(RETS[1]) * _roll(1) - F0)) > 1.0


def test_an_episode_rolls_its_daily_path_at_the_carry():
    ep = bh.episode(
        [_fut()], _mkt(carries={("NIFTY", EXP): bs.Carry(B, "test")}), {"NIFTY": _feb_series()}, FEB
    )
    assert ep.worst.d == 21  # Fri 20-Nov: 29 calendar days of roll
    assert ep.worst.pnl == pytest.approx(F0 * math.exp(-0.21) * math.exp(-B * 29 / 365) - F0, rel=1e-9)


def test_an_unknown_carry_makes_the_scan_and_episodes_unknown_but_not_a_gap():
    m = _mkt(carries={})
    sc = bh.scan([_fut()], m, {"NIFTY": _series(RETS)}, 1)
    assert sc.days == () and sc.es is None and any("carry" in r for r in sc.reasons)
    # even with every window skipped (no NIFTY return stored), the scan is unknown because of the carry
    sc = bh.scan([_fut()], m, {"NIFTY": bh.Series({}, dict(_series(RETS).iv30))}, 1)
    assert sc.es is None and any("carry" in r for r in sc.reasons)
    ep = bh.episode([_fut()], m, {"NIFTY": _feb_series()}, FEB)
    assert ep.worst is None and any("carry" in r for r in ep.reasons)
    assert all(g.pnl is not None for g in bh.gap_scenarios([_fut()], m, GAPS))  # no time passes: no roll
    exp = bh.expiry_day_scenarios([_fut()], m, EXPIRY_RETS)
    assert all(g.pnl is None and any("carry" in r for r in g.reasons) for g in exp)

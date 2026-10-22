"""The volatility race's walk-forward (P3.7, #390; volrace/race.py) on small synthetic samples: the horizon-matched
implied variance, the target, the refit schedule and purge, no look-ahead, unknown handling and the adoption read."""

import math
from dataclasses import replace
from datetime import date, timedelta

import numpy as np
import pytest

from finresearch.fincalc import realvol as rv
from finresearch.fincalc.taildist import GjrFit, GjrParams
from finresearch.volrace import race


def stub_fit(returns, window):  # a fixed GARCH so tests do not pay for Nelder-Mead
    return GjrFit(GjrParams(2e-6, 0.06, 0.04, 0.88), 0.0, len(returns))


def data_fit(
    returns, window
):  # a GARCH whose omega depends on every return it is given: a fit that saw a later
    r = np.asarray(returns[-window:])  # return would change the forecasts the look-ahead test compares
    return GjrFit(GjrParams(float(np.mean(r * r)) * 0.04, 0.06, 0.04, 0.88), 0.0, len(r))


def sessions(n: int = 300, seed: int = 390, vrp: float = 1.1) -> list[race.Session]:
    """Weekday sessions; log daily vol AR(1); OHLC from a 20-step path; IV = vrp x the true vol on two expiries."""
    rng = np.random.default_rng(seed)
    out, c, lv, d = [], 20000.0, math.log(0.01), date(2023, 1, 2)
    for _ in range(n):
        while d.weekday() >= 5:
            d += timedelta(1)
        lv = math.log(0.01) + 0.97 * (lv - math.log(0.01)) + rng.normal(0, 0.12)
        s = math.exp(lv)
        o = c * math.exp(rng.normal(0, 0.3 * s))
        path = o * np.exp(np.cumsum(rng.normal(0, 0.95 * s / math.sqrt(20), 20)))
        bar = rv.Bar(d, o, max(o, path.max()), min(o, path.min()), float(path[-1]), c)
        iv = 100 * vrp * s * math.sqrt(252)
        out.append(race.Session(d, bar, ((7, iv), (35, iv * 1.02))))
        c, d = float(path[-1]), d + timedelta(1)
    return out


def test_horizon_variance_interpolates_total_variance_flat_at_the_short_end_and_unknown_beyond() -> None:
    term = ((7, 10.0), (35, 20.0))
    assert race.horizon_variance(term, 5) == pytest.approx(0.01 * 5 / 365)  # flat short end
    w7, w35 = 0.01 * 7 / 365, 0.04 * 35 / 365
    assert race.horizon_variance(term, 21) == pytest.approx(w7 + (w35 - w7) * 14 / 28)
    assert race.horizon_variance(term, 36) is None  # beyond the longest expiry: unknown, never extrapolated
    assert race.horizon_variance((), 5) is None and race.horizon_variance(((7, None),), 5) is None


def test_target_is_h_times_yang_zhang_over_the_next_h_sessions() -> None:
    ss = sessions(40)
    assert race.target(ss, 10, 5) == pytest.approx(5 * rv.yang_zhang([s.bar for s in ss[11:16]]))
    assert race.target(ss, 36, 5) is None  # the window runs past the data


def test_refits_fall_on_each_months_first_session_and_purge_overlapping_targets() -> None:
    ss = sessions(120)
    refits = race.refit_days(ss)
    assert all(ss[i].day.month != ss[i - 1].day.month for i in refits if i > 0)
    assert [ss[i].day.month for i in refits] == sorted({s.day.month for s in ss})[
        1:
    ]  # the first month has no month start in the data
    rows = race.training_origins(ss, refit=refits[2], h=10)
    assert rows and max(rows) + 10 <= refits[2]  # purge: t + h <= refit


def test_no_look_ahead_forecasts_and_fits_ignore_every_later_session() -> None:
    ss = sessions(260)
    cut = 200
    rng = np.random.default_rng(9)
    later = [replace(s, bar=replace(s.bar, open=s.bar.open * 1.3, high=s.bar.high * 1.5, low=s.bar.low * 1.2,
                                    close=s.bar.close * 1.3, prev_close=s.bar.prev_close * 1.3),
                     term=tuple((d, v * rng.uniform(0.5, 2)) for d, v in s.term)) for s in ss[cut + 1 :]]  # fmt: skip
    a = race.forecasts(ss, (5, 10), burn_in=120, fit_garch=data_fit)
    b = race.forecasts(ss[: cut + 1] + later, (5, 10), burn_in=120, fit_garch=data_fit)
    for h in (5, 10):
        for t in range(120, cut + 1):
            assert a[h][t] == b[h][t], (h, t)  # every model's forecast at an origin up to the cut
    assert any(a[5][t] != b[5][t] for t in range(cut + 1, 250))  # the mutation did reach later origins
    assert all(a[5][t]["garch"] is not None for t in range(150, cut + 1))  # the GARCH path was exercised


def test_an_invalid_bar_makes_what_it_touches_unknown_and_counted() -> None:
    ss = sessions(260)
    ss[180] = replace(ss[180], bar=replace(ss[180].bar, close=0.0))  # a zero close
    f = race.forecasts(ss, (5,), burn_in=120, fit_garch=stub_fit)
    assert race.target(ss, 177, 5) is None  # its target window holds the bar
    assert f[5][185]["yz22"] is None and f[5][185]["har"] is None  # trailing windows and HAR rows hold it
    res = race.run(ss, (5,), burn_in=120, fit_garch=stub_fit, reps=200)
    assert res["horizons"][5]["unknown_targets"] >= 5


def test_the_race_reports_dm_holm_mcs_and_mz_and_reads_the_adoption_gate() -> None:
    res = race.run(sessions(300), (5, 10), burn_in=120, fit_garch=stub_fit, reps=300)
    for h in (5, 10):
        r = res["horizons"][h]
        assert set(r["models"]) == set(race.MODELS)
        assert (
            r["dm"]["n"] == len(r["origins"])
            and r["origins"] == list(range(r["origins"][0], 300, h))[: r["dm"]["n"]]
        )
        assert r["mcs"] is None or set(r["mcs"]["included"]) <= set(race.MODELS)
        assert {"a", "b", "r2"} <= set(r["models"]["mkt"]["mz"])
    assert set(res["holm"]) == {5, 10}
    assert res["passes"] in (True, False) and res["window_verdict"].startswith(("passes", "fails"))


def test_adoption_needs_both_windows_and_two_shared_winning_horizons() -> None:
    def w(*hs: int) -> dict:
        return {"winning_horizons": list(hs), "passes": len(hs) >= 2}

    assert race.adopt(w(5, 10), w(5, 10, 22)) is True
    assert race.adopt(w(5, 10), w(10, 22)) is False  # each window passes, but only h = 10 is shared
    assert race.adopt(w(5, 10, 22), w(5)) is False
    assert race.adopt(w(5, 10), None) is False  # the other window not read: no adoption

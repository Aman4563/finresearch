"""P3.5 (#388): probabilities that match the trade plan's exits. A Monte Carlo of the card's own exit rules: forward
paths on the stored session calendar, each leg repriced by Black-76 at its origin IV moved by a spot-vol rule, and
every path run through `fnobook.card_forecasts.simulate` (the card ledger's exit replay, i.e. `backtest.engine.manage`)
so there is one implementation of the exit rules. Synthetic inputs only; expected values worked out here."""

from __future__ import annotations

import math
from datetime import date

import numpy as np
import pytest

from finresearch.fincalc import black76 as b76

D0, EXP = date(2025, 1, 10), date(2025, 2, 13)


def test_forward_paths_are_a_martingale_with_the_registered_total_variance():
    from finresearch.fnobook import path_pop as pp

    times = np.array([3, 4, 5, 34]) / 365  # calendar years from the origin close to each session
    f = pp.forward_paths(20000.0, 0.15, times, 200_000, np.random.default_rng(1))
    assert f.shape == (200_000, 4)
    end = np.log(f[:, -1] / 20000.0)
    se = 0.15 * math.sqrt(34 / 365) / math.sqrt(200_000)
    # E[ln F_T] = -sigma^2 T / 2 and var = sigma^2 T (zero-drift forward: S drifts at r, as A1.6)
    assert end.mean() == pytest.approx(-0.5 * 0.15**2 * 34 / 365, abs=4 * se)
    assert end.var() == pytest.approx(0.15**2 * 34 / 365, rel=0.01)
    assert f[:, -1].mean() == pytest.approx(20000.0, rel=4 * se)
    again = pp.forward_paths(20000.0, 0.15, times, 10, np.random.default_rng(1))
    assert np.array_equal(again, pp.forward_paths(20000.0, 0.15, times, 10, np.random.default_rng(1)))


def test_a_leg_is_repriced_by_black76_at_its_origin_iv_moved_by_the_spot_vol_rule():
    from finresearch.fnobook import path_pop as pp

    leg = pp.PathLeg("put", 19800.0, "sell", entry=60.0, iv=0.14, contracts=1500)
    tau, df = 30 / 365, math.exp(-0.065 * 30 / 365)
    # no move: the origin IV; beta = -0.5 and F down 2 %: sigma = 0.14 - 0.5 x ln(0.98) = 0.150101
    assert pp.leg_price(leg, 20000.0, 20000.0, tau, 0.065, -0.5) == pytest.approx(
        b76.price("put", 20000.0, 19800.0, tau, df, 0.14), abs=1e-9
    )
    sig = 0.14 - 0.5 * math.log(0.98)
    assert pp.leg_price(leg, 19600.0, 20000.0, tau, 0.065, -0.5) == pytest.approx(
        b76.price("put", 19600.0, 19800.0, tau, df, sig), abs=1e-9
    )
    # a shift below the solver's floor is floored there, never negative
    assert pp.leg_price(leg, 30000.0, 20000.0, tau, 0.065, -5.0) == pytest.approx(
        b76.price("put", 30000.0, 19800.0, tau, df, b76.SIGMA_LO), abs=1e-9
    )
    assert pp.leg_price(leg, 19000.0, 20000.0, 0.0, 0.065, 0.0) == pytest.approx(800.0)  # intrinsic at expiry


def test_a_mark_is_rounded_to_the_tick_and_never_below_one():
    from finresearch.fnobook import path_pop as pp

    assert pp.tick(12.3449) == 12.35 and pp.tick(12.32) == 12.3 and pp.tick(0.004) == 0.05


# --------------------------------------------------------------------------- the exit rules are the engine's
from test_card_forecasts import LOT, PCS, STOP_PATH, card, charges  # noqa: E402

from finresearch.backtest.history import MemoryHistory  # noqa: E402


def _inp(c):
    from datetime import UTC, datetime

    from finresearch.fnobook import card_forecasts as cf

    return cf.card_inputs(c, datetime(2025, 1, 10, 6, 0, tzinfo=UTC), card_log_id=1, index=0)


def _legs(inp, iv=0.15, contracts=1500):
    from finresearch.fnobook import path_pop as pp

    return [
        pp.PathLeg(lg["right"], lg["strike"], lg["side"], lg["entry"], iv, contracts) for lg in inp["legs"]
    ]


def test_a_known_path_resolves_exactly_as_the_card_ledger_replay_of_the_same_quotes():
    """The deterministic check: STOP_PATH's quotes fed as a simulated path give the same reason, day and net as
    fnobook.card_forecasts.simulate on a MemoryHistory of those quotes (both are engine.manage)."""
    from finresearch.fnobook import card_forecasts as cf
    from finresearch.fnobook import path_pop as pp

    inp = _inp(card(PCS, kind="put_credit"))
    want = cf.simulate(inp, MemoryHistory(STOP_PATH))
    marks = [[x.options[(EXP, "put", k)].close for k in (20000.0, 19800.0)] for x in STOP_PATH[1:]]
    sessions = [x.day for x in STOP_PATH[1:]] + [EXP]
    h = pp.PathHistory(inp, _legs(inp), sessions, [20000.0] * len(sessions),
                       lambda leg_i, i: marks[i][leg_i] if i < len(marks) else 1.0)  # fmt: skip
    got = cf.simulate(inp, h)
    assert (got.outcome, got.value, got.note) == (want.outcome, want.value, want.note)
    assert got.note.startswith("stop on 2025-01-15")


def test_an_expiry_moved_earlier_by_a_holiday_settles_on_the_last_session_and_never_waits():
    """Expiry date 13-Feb is a holiday: the contracts settle on 12-Feb at the path's forward. The calendar carries a
    sentinel after the expiry date, so the exit on the last session is final (simulate would otherwise wait)."""
    from finresearch.fnobook import card_forecasts as cf
    from finresearch.fnobook import path_pop as pp

    dcs = [("call", 20000.0, "buy", 1, 300.0), ("call", 20400.0, "sell", 1, 150.0)]
    inp = _inp(card(dcs, kind="debit_call", source="spec:nifty_dcs_m", profit=None, stop=None))
    sessions = [date(2025, 1, 13), date(2025, 2, 12)]
    h = pp.PathHistory(inp, _legs(inp), sessions, [20100.0, 20300.0], lambda leg_i, i: 200.0)
    r = cf.simulate(inp, h)
    gross = (300.0 - 300.0) * LOT - (
        0.0 - 150.0
    ) * LOT  # long 20000 call worth 300 at 20,300; the wing expires
    net = gross - charges(150.0 * LOT, 300.0 * LOT, 2) - 300.0 * LOT * 0.00125  # exercise STT 0.125 %
    assert r.outcome == 1 and r.value == pytest.approx(net, abs=0.02)
    assert r.note.startswith("expiry on 2025-02-12") and "expiry moved to 2025-02-12" in r.note


# --------------------------------------------------------------------------- the Monte Carlo and its error
def _weekdays(a: date, b: date) -> list[date]:
    from datetime import timedelta

    return [a + timedelta(days=i) for i in range((b - a).days + 1) if (a + timedelta(days=i)).weekday() < 5]


SESSIONS = _weekdays(date(2025, 1, 13), EXP)  # every weekday after the origin up to the expiry (a Thursday)


def test_with_no_early_exit_the_path_pop_is_the_expiry_pop_within_its_monte_carlo_error():
    """A hold-to-expiry debit spread never exits early, so its path PoP and mean P&L must match the lognormal expiry
    model at the same volatility (fincalc.options, exact) within the reported Monte Carlo error."""
    from finresearch.fincalc import charges as ch
    from finresearch.fincalc import options as o
    from finresearch.fnobook import path_pop as pp

    dcs = [("call", 20000.0, "buy", 1, 300.0), ("call", 20400.0, "sell", 1, 150.0)]
    inp = _inp(card(dcs, kind="debit_call", source="spec:nifty_dcs_m", profit=None, stop=None))
    t, sigma, r = (EXP - D0).days / 365, 0.15, 0.065
    got = pp.monte_carlo(
        inp, _legs(inp), SESSIONS, f0=20000.0, sigma=sigma, beta=0.0, rate=r, n_paths=4000, seed=7
    )
    entry = ch.order_costs([ch.TradeLeg("option", "buy", 300.0, LOT), ch.TradeLeg("option", "sell", 150.0, LOT)],
                           D0, 20.0).total  # fmt: skip
    legs = [o.Leg("call", 20000.0, 300.0, LOT), o.Leg("call", 20400.0, 150.0, -LOT)]
    stt = lambda p: ch.exercise_stt(LOT * max(0.0, p - 20000.0), EXP)  # noqa: E731
    spot = 20000.0 * math.exp(-r * t)
    want = o.outcome(legs, spot, t, sigma, r, r, entry, stt, steps=4000)
    mean = sum(v * w for v, w in o.pnl_distribution(legs, spot, t, sigma, r, entry, stt, 4000))
    assert got.n == 4000 and got.void == 0 and got.reasons == {"expiry": 1.0}
    assert got.pop == pytest.approx(want.pop, abs=4 * got.pop_se)
    assert got.ev == pytest.approx(mean, abs=4 * got.ev_se)
    assert got.pop_se == pytest.approx(math.sqrt(got.pop * (1 - got.pop) / 4000))
    assert got.pop_ci[0] < got.pop < got.pop_ci[1] and len(got.dots) == 20 and got.dots == sorted(got.dots)


def test_reason_shares_sum_to_one_with_binomial_errors_and_a_seed_reproduces_the_run():
    from finresearch.fnobook import path_pop as pp

    inp = _inp(card(PCS, kind="put_credit"))
    a = pp.monte_carlo(
        inp, _legs(inp), SESSIONS, f0=20000.0, sigma=0.25, beta=-0.3, rate=0.065, n_paths=600, seed=3
    )
    b = pp.monte_carlo(
        inp, _legs(inp), SESSIONS, f0=20000.0, sigma=0.25, beta=-0.3, rate=0.065, n_paths=600, seed=3
    )
    assert a == b
    assert sum(a.reasons.values()) == pytest.approx(1.0) and set(a.reasons) <= {
        "target",
        "stop",
        "time",
        "expiry",
    }
    for k, p in a.reasons.items():
        assert a.reasons_se[k] == pytest.approx(math.sqrt(p * (1 - p) / 600))
    assert "stop" in a.reasons and "target" in a.reasons  # a 25 % vol path both stops out and takes profit


def test_a_path_with_no_volatility_is_one_deterministic_replay():
    """sigma -> 0: every path is the flat forward, so the Monte Carlo is the single replay of that path."""
    from finresearch.fnobook import card_forecasts as cf
    from finresearch.fnobook import path_pop as pp

    inp = _inp(card(PCS, kind="put_credit"))
    legs = _legs(inp)
    got = pp.monte_carlo(
        inp, legs, SESSIONS, f0=20000.0, sigma=1e-9, beta=0.0, rate=0.065, n_paths=50, seed=1
    )
    flat = [20000.0] * len(SESSIONS)
    taus = [(EXP - d).days / 365 for d in SESSIONS]
    one = cf.simulate(
        inp,
        pp.PathHistory(
            inp,
            legs,
            SESSIONS,
            flat,
            lambda j, i: pp.leg_price(legs[j], 20000.0, 20000.0, taus[i], 0.065, 0.0),
        ),
    )
    assert got.ev == pytest.approx(one.value, abs=0.01) and got.ev_se == pytest.approx(0.0, abs=1e-6)
    assert got.reasons == {pp.REASONS[one.note.split(" on ")[0]]: 1.0}


def test_the_spot_vol_slope_is_ols_on_the_trailing_window_and_unknown_when_short():
    """beta = OLS slope of d(ATM IV) on d ln F over the last BETA_WINDOW paired sessions; fewer than BETA_MIN pairs:
    unknown (None), never 0."""
    from finresearch.fnobook import path_pop as pp

    old = [(0.01 * ((i % 7) - 3), 9.0) for i in range(100)]  # outside the window: would pull the slope away
    rets = [0.004 * ((i * 37) % 11 - 5) for i in range(pp.BETA_WINDOW)]
    pairs = old + [(r, -0.6 * r + 0.0005 * ((i % 3) - 1)) for i, r in enumerate(rets)]
    x, y = rets, [b for _, b in pairs[100:]]
    mx, my = sum(x) / len(x), sum(y) / len(y)
    want = sum((a - mx) * (b - my) for a, b in zip(x, y, strict=True)) / sum((a - mx) ** 2 for a in x)
    assert pp.spot_vol_beta(pairs) == pytest.approx(want, rel=1e-12)
    assert pp.spot_vol_beta(pairs[-(pp.BETA_MIN - 1) :]) is None
    assert pp.BETA_WINDOW == 250 and pp.BETA_MIN == 120

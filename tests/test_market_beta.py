"""fincalc.market.beta (#504 part A): OLS beta of a stock on an index from two aligned close series. Synthetic series
only. Expected values are hand-worked, or come from an independent implementation named in the test."""

from __future__ import annotations

import dataclasses
import inspect
import itertools
import random
import statistics
from datetime import date, timedelta
from decimal import Decimal as D

import pytest

from finresearch.fincalc import market as M


def _days(n: int, start: date = date(2026, 1, 1)) -> list[date]:
    return [start + timedelta(days=i) for i in range(n)]


def _noisy(n: int = 81, seed: int = 7) -> tuple[list[D], list[D]]:
    """(stock closes, index closes) of n sessions: index returns N(0.05 %, 1 %), stock = 1.3 x index + noise."""
    rng = random.Random(seed)
    stock, index = [D(500)], [D(20000)]
    for _ in range(n - 1):
        x = rng.gauss(0.0005, 0.01)
        y = 1.3 * x + rng.gauss(0.0, 0.008)
        index.append((index[-1] * D(str(round(1 + x, 6)))).quantize(D("0.01")))
        stock.append((stock[-1] * D(str(round(1 + y, 6)))).quantize(D("0.01")))
    return stock, index


def test_ols_core_by_hand():
    """x = -1, 0, 1, 2; y = -1, 1, 2, 6. Means 0.5 and 2. Sxy = 4.5 + 0.5 + 0 + 6 = 11; Sxx = 5; beta = 2.2;
    alpha = 2 - 2.2 x 0.5 = 0.9. Residuals 0.3, 0.1, -1.1, 0.7: SSE = 1.80; s^2 = 1.8 / (4 - 2) = 0.9;
    SE = sqrt(0.9 / 5) = sqrt(0.18) = 0.4242641; Syy = 26; R^2 = 1 - 1.8 / 26 = 0.9307692."""
    beta, alpha, se, r2 = M._ols([D(-1), D(0), D(1), D(2)], [D(-1), D(1), D(2), D(6)])
    assert (beta, alpha) == (D("2.2"), D("0.9"))
    assert se.quantize(D("0.0000001")) == D("0.4242641") and r2.quantize(D("0.0000001")) == D("0.9307692")


def test_beta_of_an_exactly_linear_pair_is_the_slope_with_no_error():
    """Index closes 100, 200, 100, ...: returns +100 % and -50 %. Stock closes 100, 400, 100, ...: +300 % and -75 %.
    Both points lie on y = 0.5 + 2.5 x (3 = 0.5 + 2.5; -0.75 = 0.5 - 1.25), so beta is 2.5 exactly, alpha 0.5,
    the standard error 0 and R^2 1. 61 closes give the 60 returns the function needs."""
    n = 61
    b = M.beta([100, 400] * 30 + [100], [100, 200] * 30 + [100], _days(n), _days(n), as_of=_days(n)[-1])
    assert (b.beta, b.alpha, b.se, b.r_squared, b.n) == (D("2.5"), D("0.5"), D(0), D(1), 60)
    assert (b.window_start, b.window_end, b.as_of) == (date(2026, 1, 1), date(2026, 3, 2), date(2026, 3, 2))
    assert b.alignment == "dates match" and b.staleness.startswith("the series ends 0 days before as_of")
    assert b.largest_move == D(3) and b.largest_move_date == date(2026, 1, 2)  # +300 %, first on day 2


def test_beta_agrees_with_independent_implementations_on_a_noisy_series():
    """Independent checks: slope and intercept against the standard library's least squares
    (statistics.linear_regression), R^2 against statistics.correlation squared, and the standard error against the
    registered F&O estimator (fincalc.beta.estimate), whose weighted formula is ordinary least squares when the
    weights are equal: called here with its own spec object copied and the half-life made so long that every
    weight is 1 to twelve places. The registered spec itself is not touched."""
    from finresearch.fincalc import beta as registered
    from finresearch.fincalc import beta_prereg, beta_specs

    stock, index = _noisy()
    days = _days(len(stock))
    b = M.beta(stock, index, days, days, as_of=days[-1])
    ys = [float(stock[i] / stock[i - 1] - 1) for i in range(1, len(stock))]
    xs = [float(index[i] / index[i - 1] - 1) for i in range(1, len(index))]
    slope, intercept = statistics.linear_regression(xs, ys)
    assert float(b.beta) == pytest.approx(slope, rel=1e-9) and float(b.alpha) == pytest.approx(
        intercept, rel=1e-9
    )
    assert float(b.r_squared) == pytest.approx(statistics.correlation(xs, ys) ** 2, rel=1e-9)
    assert b.n == 80 and 1.0 < float(b.beta) < 1.6  # built with a true slope of 1.3
    flat = dataclasses.replace(beta_specs.SPEC, half_life_sessions=10**12, window_sessions=80, min_returns=60,
                               max_missing=0, recent_sessions=0)  # fmt: skip
    pairs = list(itertools.pairwise(days))
    est = registered.estimate("XYZ", [(a, c, x) for (a, c), x in zip(pairs, xs, strict=True)],
                              [(a, c, y) for (a, c), y in zip(pairs, ys, strict=True)], days[-1], spec=flat)  # fmt: skip
    assert est.known and float(b.beta) == pytest.approx(est.beta, rel=1e-8)
    assert float(b.se) == pytest.approx(est.se, rel=1e-8)
    assert beta_specs.registration_hash() == beta_prereg.HASH


def test_beta_without_dates_says_alignment_and_staleness_were_not_checked():
    stock, index = _noisy()
    b = M.beta(stock, index)
    assert b.alignment.startswith("not checked") and b.staleness.startswith("not checked")
    assert (b.window_start, b.window_end, b.as_of, b.largest_move_date) == (None, None, None, None)
    days = _days(len(stock))
    assert M.beta(stock, index, days, days).staleness.startswith("not checked")  # dates but no as_of
    assert M.beta(stock, index, days, days).beta == b.beta


def test_beta_refuses_short_stale_and_misaligned_series_with_the_reason():
    stock, index = _noisy()
    days = _days(len(stock))
    with pytest.raises(ValueError, match="short: 59 returns, at least 60"):
        M.beta(stock[:60], index[:60])
    with pytest.raises(ValueError, match="misaligned: 81 stock closes and 80 index closes"):
        M.beta(stock, index[:-1])
    shifted = [*days[:40], *[d + timedelta(days=1) for d in days[40:]]]  # the index misses one session
    with pytest.raises(
        ValueError, match=r"misaligned: .* position 40 \(stock 2026-02-10, index 2026-02-11\)"
    ):
        M.beta(stock, index, days, shifted)
    # the last close is 22-Mar-2026: 7 days later is the limit, 8 is stale; a date before it is a mistake
    assert M.beta(stock, index, days, days, as_of=date(2026, 3, 29)).staleness.startswith(
        "the series ends 7 days"
    )
    with pytest.raises(
        ValueError, match="stale: the series ends on 2026-03-22, 8 days before as_of 2026-03-30"
    ):
        M.beta(stock, index, days, days, as_of=date(2026, 3, 30))
    with pytest.raises(ValueError, match="as_of 2026-03-01 is before the last close"):
        M.beta(stock, index, days, days, as_of=date(2026, 3, 1))


def test_beta_refuses_other_invalid_input():
    stock, index = _noisy()
    days = _days(len(stock))
    for kw, why in (
        ({"stock_dates": days}, "give both stock_dates and index_dates"),
        ({"as_of": days[-1]}, "as_of needs the dates"),
        ({"stock_dates": days[:-1], "index_dates": days[:-1]}, "one date for each close"),
        ({"stock_dates": days[::-1], "index_dates": days[::-1]}, "oldest first"),
    ):
        with pytest.raises(ValueError, match=why):
            M.beta(stock, index, **kw)
    with pytest.raises(ValueError, match="close must be > 0"):
        M.beta([*stock[:-1], 0], index)
    with pytest.raises(ValueError, match="index's returns do not vary"):
        M.beta(stock, [100] * len(stock))
    with pytest.raises(ValueError, match="stock's returns do not vary"):
        M.beta([100] * len(index), index)  # a price that never moves has no beta: not 0
    # the floor is a constant of the module, not something a caller can lower
    assert M.BETA_MIN_RETURNS == 60 and list(inspect.signature(M.beta).parameters) == [
        "stock_closes",
        "index_closes",
        "stock_dates",
        "index_dates",
        "as_of",
    ]


def test_beta_takes_iso_dates_as_run_fincalc_passes_them():
    """Agents pass JSON: mcp_server.run_fincalc turns lists of ISO strings into dates; plain strings work too."""
    from finresearch.mcp_server.server import run_fincalc

    stock, index = _noisy()
    iso = [d.isoformat() for d in _days(len(stock))]
    args = {"stock_closes": [str(x) for x in stock], "index_closes": [str(x) for x in index],
            "stock_dates": iso, "index_dates": iso, "as_of": iso[-1]}  # fmt: skip
    out = run_fincalc("market.beta", args)
    direct = M.beta(stock, index, iso, iso, as_of=iso[-1])
    assert out["beta"] == str(direct.beta) and out["n"] == 80 and out["window_end"] == "2026-03-22"
    assert out["alignment"] == "dates match" and out["min_returns"] == 60

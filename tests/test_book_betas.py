"""The beta-to-NIFTY estimator for the F&O book (#394, #395): its registration and the pure estimator, against
values computed by hand in the comments (or by an independent formula)."""

from __future__ import annotations

from pathlib import Path

from finresearch.fincalc import beta_prereg, beta_specs


def test_the_registration_is_pinned_and_quoted_in_prereg_md() -> None:
    assert beta_specs.registration_hash() == beta_prereg.HASH
    text = (Path(__file__).resolve().parents[1] / "evals/experiments/book_betas/PREREG.md").read_text()
    s = beta_specs.SPEC
    assert beta_prereg.HASH in text and beta_specs.SPEC_VERSION in text
    for needle in (f"half-life of **{s.half_life_sessions} sessions**", f"**{s.window_sessions}**",
                   f"**{s.min_returns}**", f"z = {s.z}", f"floor of **{s.floor:g}**"):  # fmt: skip
        assert needle in text, needle


# --------------------------------------------------------------------------- the pure estimator
import math  # noqa: E402
from dataclasses import replace  # noqa: E402
from datetime import date, timedelta  # noqa: E402

import pytest  # noqa: E402

from finresearch.fincalc import beta as fb  # noqa: E402

D0 = date(2026, 1, 1)


def _days(n: int) -> list[date]:
    return [D0 + timedelta(days=i) for i in range(n + 1)]


def _series(rets: list[float | None], days: list[date] | None = None) -> list[tuple[date, date, float]]:
    """(d0, d1, r) on consecutive calendar days; None = no return that session."""
    ds = days or _days(len(rets))
    return [(ds[i], ds[i + 1], r) for i, r in enumerate(rets) if r is not None]


def _spec(**kw):
    base = {"min_returns": 3, "max_missing": 0, "recent_sessions": 0, "max_age_days": 7}
    return replace(beta_specs.SPEC, **{**base, **kw})


def test_equal_weights_give_the_ordinary_least_squares_beta_and_se() -> None:
    # x = NIFTY, y = the underlying. x̄ = 0.005, ȳ = 0.0125; Sxx = 1.3e-3, Sxy = 2.05e-3 -> β = 41/26.
    # residuals ×100: -0.0384615, -0.307692, -0.192308, 0.538462 -> Σe² = (5.5/13)e-4; s² = Σe²/(n-2);
    # SE = √(s²/Sxx) = 0.127563
    x, y = [0.01, -0.02, 0.03, 0.00], [0.02, -0.03, 0.05, 0.01]
    est = fb.estimate(
        "BANKNIFTY", _series(x), _series(y), _days(4)[-1], spec=_spec(half_life_sessions=10**12)
    )
    assert est.reason is None and est.n == 4 and est.missing == 0
    assert est.beta == pytest.approx(41 / 26, rel=1e-9)
    assert est.se == pytest.approx(math.sqrt(5.5 / 13 * 1e-4 / 2 / 1.3e-3), rel=1e-6)
    assert est.se == pytest.approx(0.127563, abs=1e-6)
    assert (est.window_start, est.as_of) == (_days(4)[1], _days(4)[4])


def test_ewma_weights_by_half_life_give_the_hand_computed_beta_and_se() -> None:
    # half-life 1 session: w = 1/4, 1/2, 1 (oldest first). Σw = 1.75, x̄ = 0.01, ȳ = 0.03/1.75.
    # dx = 0, -0.02, 0.01 -> Sxx = 0.5·4e-4 + 1e-4 = 3e-4; Sxy = 4e-4 -> β = 4/3.
    # Σw e² = 1/420000; n_eff = 1.75²/1.3125 = 7/3; s² = (Σwe²/Σw)·n_eff/(n_eff-2) = 9.5238e-6;
    # SE² = s² / (n_eff·Sxx/Σw) = 9.5238e-6 / 4e-4 = 1/42.
    x, y = [0.01, -0.01, 0.02], [0.02, -0.01, 0.03]
    est = fb.estimate("RELIANCE", _series(x), _series(y), _days(3)[-1], spec=_spec(half_life_sessions=1))
    assert est.beta == pytest.approx(4 / 3, rel=1e-9)
    assert est.se == pytest.approx(1 / math.sqrt(42), rel=1e-9)
    assert est.n_eff == pytest.approx(7 / 3, rel=1e-9)


def test_the_band_takes_the_upper_bound_with_its_floor_and_an_unfloored_lower_bound() -> None:
    x, y = [0.01, -0.01, 0.02], [0.02, -0.01, 0.03]
    est = fb.estimate("RELIANCE", _series(x), _series(y), _days(3)[-1], spec=_spec(half_life_sessions=1))
    se = 1 / math.sqrt(42)
    assert est.lo == pytest.approx(4 / 3 - 1.645 * se) and est.hi == pytest.approx(4 / 3 + 1.645 * se)
    # a low beta: y = x / 2 exactly (SE 0) -> lo = 0.5, hi = the floor 1
    low = fb.estimate(
        "ITC", _series(x), _series([v / 2 for v in x]), _days(3)[-1], spec=_spec(half_life_sessions=1)
    )
    assert low.beta == pytest.approx(0.5) and low.lo == pytest.approx(0.5) and low.hi == 1.0
    assert fb.NIFTY.beta == 1.0 and (fb.NIFTY.lo, fb.NIFTY.hi) == (1.0, 1.0) and fb.NIFTY.reason is None


X8 = [0.01, -0.02, 0.015, -0.005, 0.02, -0.01, 0.005, 0.012]


def test_a_missing_pair_does_not_shift_the_other_weights_and_is_counted() -> None:
    # the 3rd NIFTY session has no paired return: the weights of the rest stay 2^-k by NIFTY's calendar, so the
    # estimate equals one from the six remaining pairs with those same weights (built here independently)
    y = [1.2 * v + 0.001 * (-1) ** i for i, v in enumerate(X8)]
    yy = [None if i == 2 else v for i, v in enumerate(y)]
    sp = _spec(half_life_sessions=3, max_missing=1)
    est = fb.estimate("BANKNIFTY", _series(X8), _series(yy), _days(8)[-1], spec=sp)
    keep = [i for i in range(8) if i != 2]
    w = [2 ** (-(7 - i) / 3) for i in keep]
    xs, ys = [X8[i] for i in keep], [y[i] for i in keep]
    sw = sum(w)
    xb, yb = (
        sum(a * b for a, b in zip(w, xs, strict=True)) / sw,
        sum(a * b for a, b in zip(w, ys, strict=True)) / sw,
    )
    want = sum(a * (b - xb) * (c - yb) for a, b, c in zip(w, xs, ys, strict=True)) / sum(
        a * (b - xb) ** 2 for a, b in zip(w, xs, strict=True)
    )
    assert est.reason is None and (est.n, est.missing) == (7, 1)
    assert est.beta == pytest.approx(want, rel=1e-12)


@pytest.mark.parametrize(
    ("other", "today", "spec_kw", "needle"),
    [
        ([0.01] * 2 + [None] * 6, None, {"max_missing": 8}, "only 2 paired returns (needs 3)"),
        (
            X8[:5] + [None] + X8[6:],
            None,
            {"max_missing": 8, "recent_sessions": 3},
            "missing in the last 3 sessions",
        ),
        (
            X8[:2] + [None, None] + X8[4:],
            None,
            {"max_missing": 1},
            "2 sessions without a return in the window (at most 1)",
        ),
        (X8, date(2026, 1, 20), {}, "stale (last session 09-Jan-2026)"),
    ],
)
def test_too_little_history_gaps_and_a_stale_series_leave_beta_unknown(other, today, spec_kw, needle) -> None:
    est = fb.estimate("hdfcbank", _series(X8), _series(other), today or _days(8)[-1], spec=_spec(**spec_kw))
    assert not est.known and est.beta is None and est.se is None and est.lo is None and est.hi is None
    assert (
        est.reason is not None
        and est.reason.startswith("HDFCBANK: beta to NIFTY unknown")
        and needle in est.reason
    )


def test_a_two_session_return_is_not_paired_with_a_one_session_nifty_return() -> None:
    # the underlying has no settle on day 2, so its return for day 3 spans days 1-3: missing, not paired with day 3
    nifty = _series(X8)
    ds = _days(8)
    other = [(ds[i], ds[i + 1], v) for i, v in enumerate(X8) if i not in (1, 2)] + [(ds[1], ds[3], 0.0)]
    est = fb.estimate("SBIN", nifty, other, ds[-1], spec=_spec(max_missing=2))
    assert (est.n, est.missing) == (6, 2)


def test_returns_follow_the_front_month_change_within_one_contract() -> None:
    a, b, c = date(2026, 1, 29), date(2026, 2, 26), date(2026, 3, 26)
    d1, d2, d3 = date(2026, 1, 28), date(2026, 1, 29), date(2026, 1, 30)
    futs = [(d1, a, 100.0), (d1, b, 101.0), (d2, a, 102.0), (d2, b, 103.0), (d3, b, 104.0), (d3, c, 105.0)]
    got = fb.returns_with_prev(futs)
    # day 2: contract a (102/100); day 3: a expired on day 2, so b on both days (104/103), never 104/102
    assert got == [(d1, d2, pytest.approx(math.log(102 / 100))), (d2, d3, pytest.approx(math.log(104 / 103)))]


def test_an_unregistered_change_refuses_an_estimate(monkeypatch) -> None:
    monkeypatch.setattr(beta_prereg, "HASH", "0" * 64)
    with pytest.raises(fb.PreregistrationError):
        fb.estimate("BANKNIFTY", _series(X8), _series(X8), _days(8)[-1])

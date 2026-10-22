"""The VRP study's pure statistics (P3.6, #389; vrp/study.py): the 30-calendar-day Yang-Zhang RV in calendar-day
variance, the variance-swap payoff on windows from each monthly expiry, the Newey-West t, the expanding-tercile
split, the India VIX gate and the E[z^2] / E|z| pair. Expected values are computed here by hand."""

import math
from datetime import date, timedelta

import pytest

from finresearch.fincalc import realvol as rv
from finresearch.vrp import study as st

D0 = date(2024, 1, 25)


def bar(d, o, h, lo, c, pc):
    return rv.Bar(d, o, h, lo, c, pc)


def day(d, b, mf=None, vix=None):
    return st.Day(d, b, mf, None if mf else "no sigma", None, vix)


def _yz(bars):
    """Yang-Zhang by hand (Yang and Zhang 2000, eq. 9 with alpha 1.34)."""
    n = len(bars)
    o = [math.log(b.open / b.prev_close) for b in bars]
    c = [math.log(b.close / b.open) for b in bars]
    rs = [math.log(b.high / b.close) * math.log(b.high / b.open) + math.log(b.low / b.close) * math.log(b.low / b.open)
          for b in bars]  # fmt: skip

    def var(x):
        m = sum(x) / len(x)
        return sum((v - m) ** 2 for v in x) / (len(x) - 1)

    k = 0.34 / (1.34 + (n + 1) / (n - 1))
    return var(o) + k * var(c) + (1 - k) * sum(rs) / n


def test_window_rv_is_total_yang_zhang_over_thirty_calendar_days_annualised_by_365_over_30() -> None:
    bars = [bar(D0, 100, 101, 99, 100, 99.5)]
    px = 100.0
    for i in range(1, 40):
        o = px * (1 + 0.002 * (-1) ** i)
        c = o * (1 + 0.004 * (-1) ** (i // 2))
        bars.append(bar(D0 + timedelta(i), o, max(o, c) * 1.003, min(o, c) * 0.997, c, px))
        px = c
    days = [day(b.day, b, mf=0.02 if b.day == D0 else None) for b in bars]
    (w,) = st.windows(days, {D0}, data_end=bars[-1].day)
    inside = [b for b in bars if D0 < b.day <= D0 + timedelta(30)]
    assert w.sessions == 30
    assert w.rv == pytest.approx(len(inside) * _yz(inside) * 365 / 30, rel=1e-12)
    assert w.payoff == pytest.approx((0.02 - w.rv) / 0.02)
    assert w.log_ratio == pytest.approx(math.log(0.02 / w.rv))
    # an invalid bar inside, a window past the data, an unknown sigma: each window is unknown with its reason
    bad = list(days)
    bad[5] = day(bars[5].day, bar(bars[5].day, 100, 99, 98, 100, 100))
    assert (
        st.windows(bad, {D0}, data_end=bars[-1].day)[0].reason == "RV unknown (an invalid bar in the window)"
    )
    assert "data end" in st.windows(days, {D0}, data_end=D0 + timedelta(29))[0].reason
    assert st.windows([day(b.day, b) for b in bars], {D0}, data_end=bars[-1].day)[0].reason == "no sigma"


def test_newey_west_t_lag_one_by_hand() -> None:
    x = [0.3, -0.1, 0.25, 0.4, -0.2, 0.1, 0.05, 0.35]
    n, m = len(x), sum(x) / len(x)
    e = [v - m for v in x]
    g0 = sum(v * v for v in e) / n
    g1 = sum(e[i] * e[i - 1] for i in range(1, n)) / n
    se = math.sqrt((g0 + 2 * 0.5 * g1) / n)
    out = st.nw_mean(x, lag=1)
    assert (
        out["mean"] == pytest.approx(m)
        and out["se"] == pytest.approx(se)
        and out["t"] == pytest.approx(m / se)
    )
    assert out["ci95"] == pytest.approx((m - 1.959963984540054 * se, m + 1.959963984540054 * se))
    assert st.nw_mean([0.1, 0.2], lag=1)["t"] is None  # too short: unknown, never 0


def _w(i, mf, payoff):
    return st.Window(D0 + timedelta(30 * i), mf, 0.0, 20, payoff, 0.0, None)


def test_primary_summary_and_expanding_terciles() -> None:
    ws = [_w(i, 0.01 + 0.001 * i, (-1) ** i * 0.1 + 0.05) for i in range(15)] + [st.Window(D0, None, None, 0, None,
                                                                                       None, "no sigma")]  # fmt: skip
    s = st.primary(ws)
    assert s["n"] == 15 and s["unknown"] == 1 and s["unknown_reasons"] == {"no sigma": 1}
    assert s["share_negative"] == pytest.approx(7 / 15)
    # windows 12-14 each have sigma^2 above every earlier start: all in the top tercile; the 12 before are burn-in
    t = st.terciles(ws[:15])
    assert t["burn_in"] == 12 and t["by_tercile"]["3"]["n"] == 3 and t["by_tercile"]["1"]["n"] == 0
    low = [*ws[:12], _w(12, 0.0101, 0.2)]
    assert st.terciles(low)["by_tercile"]["1"]["n"] == 1


def test_india_vix_gate_by_era() -> None:
    days = []
    for i in range(60):
        v = 15 + 3 * math.sin(i / 4)
        days.append(day(D0 + timedelta(i), None, mf=(v / 100) ** 2, vix=v + 0.5))
    g = st.gate(days)
    assert g["passes"] and g["n"] == 59 and g["corr_changes"] == pytest.approx(1.0)
    assert g["median_gap"] == pytest.approx(-0.5)
    far = [st.Day(x.day, x.bar, x.mf30, None, None, x.vix + 1.0) for x in days]
    assert not st.gate(far)["passes"]
    few = st.gate(days[:10])
    assert not few["passes"] and "fewer than" in few["reason"]
    assert st.gate([day(x.day, None, mf=x.mf30) for x in days])["reason"].startswith("no India VIX")


def test_z_pair_reports_e_abs_z_scaled_beside_e_z_squared() -> None:
    out = st.z_stats([1.0, -1.0, 2.0, -2.0, None])
    assert out["n"] == 4 and out["unknown"] == 1
    assert out["e_z2"] == pytest.approx(2.5)
    assert out["e_abs_z_scaled"] == pytest.approx(1.5 / math.sqrt(2 / math.pi))

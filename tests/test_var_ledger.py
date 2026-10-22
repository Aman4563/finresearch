"""#399: the portfolio's historical 1-day 95 % VaR logged daily to the forecast ledger (by the monitor), resolved on the
next day of the value history and backtested with Kupiec and Christoffersen once 250 days are logged. Synthetic series
only; expected values come from numpy, by hand, or from the independent horserace.scores implementation."""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pytest

from finresearch.portfolio.series import Series

IST = timezone(timedelta(hours=5, minutes=30))
END = date(2026, 9, 30)  # a Wednesday


def _bdays(end: date, n: int) -> list[date]:
    out, d = [], end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return out[::-1]


def _rets(n: int) -> list[float]:
    return [0.012 * math.sin(i * 1.3) + 0.0015 * math.cos(i * 0.4) for i in range(n)]


def _series(n_days: int = 262, end: date = END, complete_last: bool = True) -> Series:
    """A value series whose daily TWR returns are `_rets`, with ₹50,000 put in on day 5 (a flow, not a gain)."""
    days = _bdays(end, n_days)
    rs = _rets(n_days - 1)
    value, invested = [1_000_000.0], [1_000_000.0]
    for t, r in enumerate(rs, start=1):
        flow = 50_000.0 if t == 5 else 0.0
        value.append(value[-1] * (1 + r) + flow)
        invested.append(invested[-1] + flow)
    complete = [True] * n_days
    complete[-1] = complete_last
    return Series(days, value, invested, complete, fingerprint="fp")


def at(d: date, hh: int, mm: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=IST)


# --------------------------------------------------------------------------- pure
def test_daily_returns_remove_flows():
    from finresearch.portfolio.var_ledger import daily_returns

    ser = _series(20)
    got = daily_returns(ser)
    assert [d for d, _ in got] == ser.days[1:]
    assert [r for _, r in got] == pytest.approx(_rets(19), abs=1e-12)  # the ₹50,000 on day 5 is not a return


def test_forecast_is_the_historical_var_of_the_last_250_returns():
    from finresearch.portfolio.var_ledger import var_forecast

    ser = _series(262)  # 261 returns: the last 250 are used
    v, why = var_forecast(ser, at(END, 16, 0))
    assert why is None
    expected = -float(np.quantile(np.array(_rets(261)[-250:]), 0.05))  # numpy's linear interpolation
    assert v["inputs"]["var"] == pytest.approx(expected, abs=1e-6)
    assert v["inputs"]["as_of"] == END.isoformat() and v["inputs"]["n"] == 250
    assert v["inputs"]["window_start"] == ser.days[-250].isoformat()
    assert v["probability"] == 0.05 and v["asset"] == "portfolio" and v["instrument"] == "PORTFOLIO"
    assert v["resolve_on"] == date(2026, 10, 1)
    # the ledger holds no rupee figure and no holding: fractions and dates only
    assert v["name"] is None
    flat = repr(v)
    assert "inr" not in flat.lower() and all(f"{x:.0f}" not in flat for x in ser.value[-3:])


@pytest.mark.parametrize("ser, now, reason", [
    (_series(250), at(END, 16), "needs 250 daily returns"),  # 249 returns
    (_series(262), at(END, 15, 0), "before the close"),  # during the session
    (_series(262), at(END + timedelta(days=1), 10), "not today's close"),  # a rebuild the next morning: too late
    (_series(262, complete_last=False), at(END, 16), "incomplete"),
])  # fmt: skip
def test_no_forecast_without_a_full_window_or_outside_the_after_close_window(ser, now, reason):
    from finresearch.portfolio.var_ledger import var_forecast

    v, why = var_forecast(ser, now)
    assert v is None and reason in why

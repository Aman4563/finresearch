"""The portfolio's 1-day 95 % historical VaR in the forecast ledger (#399, "Now"): logged once a trading day by the
monitor, resolved on the next day of the value history, and backtested with Kupiec and Christoffersen once 250 days
are logged.

Why a ledger as well as the walk-forward backtest on the Risk tab (analytics._var_backtest): that one re-estimates
every past day from today's reconstructed history, which a backdated import rewrites. A logged VaR is the figure
that existed on the day, before the outcome, so its backtest cannot be fitted after the fact.

The forecast (signals.ledger contract): asset "portfolio", instrument "PORTFOLIO", event "the next day's TWR return
is below -VaR", P(event) = 0.05. VaR_95 = -q_0.05 of the last WINDOW (250) daily TWR returns of the canonical value
history (portfolio.series), quantile by linear interpolation (analytics_math.historical_var), the "historical-250"
estimator #399 names. It is logged only from the evening of the history's last day (after 15:30 IST on that day), so
the next session has not opened; the close and NAV passes both run then and the later one replaces the earlier
(ledger._upsert), never a forecast made after the outcome started.

Resolution: the return on the first value-history day after `as_of`, r = (V_1 - F_1) / V_0 - 1 with F the day's net
money in (analytics_math.twr_returns). A day valued on a stale price (`complete` False), a gap over MAX_GAP_DAYS
calendar days or an `as_of` no longer in the history voids the forecast with the reason: an unknown return is never
scored as "no exception".

Privacy: the rows hold fractions and dates only (no rupee value, no holding, no name), like the F&O card ledger. The
forecast ledger is local and never sent to a model. These rows are excluded from the calibration headlines
(ledger.calibration_groups): about 250 forecasts a year at P = 0.05 would swamp the track record of the calls.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from finresearch.fincalc.dates import next_business_day, to_ist
from finresearch.portfolio import analytics_math as am

ASSET, INSTRUMENT, EVENT_KIND = "portfolio", "PORTFOLIO", "var95_1d"
LEVEL, WINDOW = 0.95, 250
MIN_TEST = 250  # resolved days before the backtest is run: 12.5 expected exceptions at 95 % (#399's power note)
MAX_GAP_DAYS = 5  # calendar days between as_of and the next history day; more = missing days, not one session
AFTER_CLOSE = (15, 30)  # IST: NSE's session has ended
METHOD = (f"historical 1-day VaR {LEVEL:.0%}: -5th percentile of the last {WINDOW} daily TWR returns of the "
          "reconstructed value history (#399)")  # fmt: skip
EVENT = ("the portfolio's time-weighted return on the next day of its value history is below -VaR (a loss larger "
         "than the 1-day 95 % VaR)")  # fmt: skip


def daily_returns(ser: Any) -> list[tuple[date, float | None]]:
    """(day, TWR return) for every history day after the first; the day's net money in is not a return."""
    flows = [0.0] + [ser.invested[t] - ser.invested[t - 1] for t in range(1, len(ser.days))]
    rs = am.twr_returns(ser.value, flows)
    return list(zip(ser.days[1:], rs[1:], strict=True))


def var_forecast(ser: Any, now: datetime) -> tuple[dict[str, Any] | None, str | None]:
    """The ledger row's values for the history's last day, or (None, why it is not logged)."""
    as_of = ser.days[-1]
    ist = to_ist(now)
    if ist.date() != as_of:
        return None, f"not today's close: the history ends on {as_of}"
    if (ist.hour, ist.minute) < AFTER_CLOSE:
        return None, "before the close: the day's return is not final"
    if not ser.complete[-1]:
        return None, f"the history's last day ({as_of}) is incomplete (a price older than 10 days)"
    rets = [r for _, r in daily_returns(ser)]
    tail = rets[am.defined_tail([None, *rets]) :]
    if len(tail) < WINDOW:
        return None, f"needs {WINDOW} daily returns; have {len(tail)}"
    var, _ = am.historical_var(tail[-WINDOW:], LEVEL)
    inputs = {"as_of": as_of.isoformat(), "window_start": ser.days[-WINDOW].isoformat(),
              "window_end": as_of.isoformat(), "n": WINDOW, "var": round(var, 6), "level": LEVEL,
              "start_date": as_of.isoformat()}  # fmt: skip
    return {"created_at": now, "asset": ASSET, "instrument": INSTRUMENT, "name": None, "source": "monitor:portfolio",
            "event_kind": EVENT_KIND, "event": EVENT, "horizon": "1 trading day",
            "resolve_on": next_business_day(as_of), "probability": round(1 - LEVEL, 6), "action": "RISK", "score": None,
            "method": METHOD, "validation_status": "rule_based", "inputs": inputs, "status": "open"}, None  # fmt: skip

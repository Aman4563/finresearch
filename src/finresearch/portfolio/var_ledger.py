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
from itertools import pairwise
from typing import Any

from finresearch.fincalc.dates import next_business_day, to_ist
from finresearch.portfolio import analytics_math as am

ASSET, INSTRUMENT, EVENT_KIND = "portfolio", "PORTFOLIO", "var95_1d"
LEVEL, WINDOW = 0.95, 250
MIN_TEST = (
    250  # resolved days before the backtest is run: 12.5 expected exceptions at 95 % (#399's power note)
)
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


def log_var(s: Any, ser: Any, now: datetime) -> dict[str, Any]:
    """Log (or replace the same day's open) VaR forecast; {"logged": bool, "reason": ...}. Monitor only: a GET never
    calls this."""
    from finresearch.signals import ledger

    values, why = var_forecast(ser, now)
    if values is None:
        return {"logged": False, "reason": why}
    values["dedupe_key"] = ledger._key_for(s, ASSET, INSTRUMENT, EVENT_KIND, ser.days[-1], METHOD, now)
    return {"logged": True, "id": ledger._upsert(s, values), "as_of": ser.days[-1].isoformat()}


def _void(why: str) -> Any:
    from finresearch.signals.ledger import Resolution

    return Resolution(None, None, f"void: {why}")


def resolve_var(f: Any, deps: Any, s: Any) -> Any:
    """The next history day's TWR return against -VaR (module docstring)."""
    from finresearch.portfolio import series
    from finresearch.signals.ledger import NotYet, Resolution

    ser, why = series.load(s)
    if ser is None:
        raise NotYet(why or "no value history")
    as_of, var = date.fromisoformat(f.inputs["as_of"]), float(f.inputs["var"])
    if as_of not in ser.days:
        return _void(f"{as_of} is no longer a day of the value history (rebuilt since)")
    i = ser.days.index(as_of)
    if i + 1 >= len(ser.days):
        raise NotYet(f"the value history has no day after {as_of} yet")
    d1 = ser.days[i + 1]
    if (d1 - as_of).days > MAX_GAP_DAYS:
        return _void(f"the next history day is {d1}, {(d1 - as_of).days} days later: not a 1-day return")
    if not (ser.complete[i] and ser.complete[i + 1]):
        return _void(f"{as_of} or {d1} is valued on a stale price: the return is unknown")
    r = dict(daily_returns(ser)).get(d1)
    if r is None:
        return _void(f"no return on {d1} (the value on {as_of} was 0)")
    hit = r < -var
    return Resolution(int(hit), r, f"{d1}: return {r:+.2%} against VaR {var:.2%}: "
                                   + ("an exception" if hit else "within the VaR"))  # fmt: skip


async def _resolver(f: Any, deps: Any, s: Any) -> Any:
    return resolve_var(f, deps, s)


def _register() -> None:
    from finresearch.signals.ledger import resolver

    resolver(ASSET, EVENT_KIND)(_resolver)


_register()


def backtest(s: Any) -> dict[str, Any]:
    """Kupiec / Christoffersen over the logged, resolved forecasts in as_of order (read only). Under MIN_TEST resolved
    days: the counts and the reason, no verdict. `trigger` is #399's start condition for the deferred work (EWMA-FHS
    VaR and ES): conditional coverage rejected, p_cc < 0.05."""
    from sqlalchemy import select

    from finresearch.db.models import Forecast
    from finresearch.portfolio.analytics import coverage_test

    rows = list(s.scalars(select(Forecast).where(Forecast.asset == ASSET, Forecast.event_kind == EVENT_KIND)))
    done = sorted((f for f in rows if f.status == "resolved" and f.outcome is not None),
                  key=lambda f: f.inputs["as_of"])  # fmt: skip
    out: dict[str, Any] = {"level": LEVEL, "window": WINDOW, "logged": len(rows), "resolved": len(done),
                           "open": sum(f.status == "open" for f in rows),
                           "void": sum(f.status == "void" for f in rows), "reason": None,
                           "since": done[0].inputs["as_of"] if done else None,
                           "how": f"the VaR logged each evening from the {WINDOW} days before it, scored on the next "
                                  "day: figures as they stood, never re-estimated"}  # fmt: skip
    if len(done) < MIN_TEST:
        out["reason"] = f"needs {MIN_TEST} logged days with a known outcome; have {len(done)}"
        return out
    out.update(coverage_test([int(f.outcome) for f in done]))
    # Christoffersen reads consecutive rows as consecutive days: count the rows that follow a missing weekday (an
    # exchange holiday counts too: an upper bound on the skipped sessions)
    ds = [date.fromisoformat(f.inputs["as_of"]) for f in done]
    out["gaps"] = sum(next_business_day(a) != b for a, b in pairwise(ds))
    if out["gaps"]:
        out["how"] += (f"; {out['gaps']} day(s) follow a day with no logged outcome, which the independence test "
                       "reads as adjacent")  # fmt: skip
    out["trigger"] = out["p_cc"] < 0.05
    return out

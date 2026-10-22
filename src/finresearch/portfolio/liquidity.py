"""Days to liquidate each holding (#348): units ÷ (participation × average daily traded shares).

Volume comes from NSE's full equity bhavcopy stored day by day (refseries.equity_eod, #373 (a)): the stock's traded
quantity in its price series (EQ, then BE/BZ, SM/ST: the close_on rule) over the last ADV_SESSIONS sessions the store
has loaded up to the valuation day. A session whose file was loaded but has no row (or a '-' quantity) for the stock
is left out of the average and counted; fewer than MIN_SESSIONS sessions with volume make the line unknown with a
reason, never 0 days. NSE volume only (BSE's is not collected), so the estimate errs towards more days.

`participation` is the share of each day's volume one would sell into without moving the price much. There is no
official figure: 10-25 % is the usual range in market-impact practice, and 20 % is the default here; it is a parameter
and is shown with every result. Mutual funds are not exchange-traded: they are redeemed at NAV, so they are "not
applicable", not "instantly liquid". Personal data (units, values) stays on this machine and never reaches an LLM.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

ADV_SESSIONS = 20  # about one month of sessions
MIN_SESSIONS = 15
DEFAULT_PARTICIPATION = 0.20
HORIZONS = (1, 5, 20)
METHOD = ("Days = units ÷ (participation × average daily traded shares on NSE over the last 20 stored sessions). "
          "Participation is a convention, not an official figure. NSE volume only, so this errs towards more days.")  # fmt: skip
FUND_REASON = "a mutual fund is redeemed at NAV (usually paid in 1-3 working days), not sold on the exchange"


@dataclass(frozen=True)
class Adv:
    shares: float | None  # average daily traded quantity
    rupees: float | None  # average daily traded value
    days: int  # sessions looked at
    days_with_rows: int  # sessions with a known quantity
    reason: str | None = None
    first: date | None = None
    last: date | None = None


def adv(sessions: Sequence[date], rows: Mapping[date, tuple[int | None, Decimal | None]]) -> Adv:
    """Average over `sessions` (loaded bhavcopy days, newest first or any order) of the stock's (qty, turnover in ₹
    lakh) `rows`; a missing row or a None quantity is left out and counted."""
    sessions = sorted(sessions)[-ADV_SESSIONS:]
    if len(sessions) < MIN_SESSIONS:
        return Adv(None, None, len(sessions), 0,
                   f"only {len(sessions)} sessions of NSE volume are stored; needs {MIN_SESSIONS}")  # fmt: skip
    got = [(d, *rows[d]) for d in sessions if d in rows and rows[d][0] is not None]
    if len(got) < MIN_SESSIONS:
        return Adv(None, None, len(sessions), len(got),
                   f"traded on {len(got)} of the last {len(sessions)} stored sessions; needs {MIN_SESSIONS}")  # fmt: skip
    shares = sum(q for _, q, _ in got) / len(got)
    vals = [float(t) * 100_000 for _, _, t in got if t is not None]
    rupees = sum(vals) / len(vals) if len(vals) == len(got) else None
    return Adv(shares, rupees, len(sessions), len(got), None, got[0][0], got[-1][0])


def read_advs(s: Session, symbols: Iterable[str], end: date) -> dict[str, Adv]:
    """Each NSE symbol's Adv over the last ADV_SESSIONS loaded bhavcopy days on or before `end`. Reads only."""
    from finresearch.db.models import EquityEod, EquityEodDay
    from finresearch.refseries.equity_eod import PRICE_SERIES

    symbols = sorted(set(symbols))
    sessions = list(s.scalars(select(EquityEodDay.day).where(EquityEodDay.status == "loaded", EquityEodDay.day <= end)
                              .order_by(EquityEodDay.day.desc()).limit(ADV_SESSIONS)))  # fmt: skip
    per: dict[str, dict[date, dict[str, tuple[int | None, Decimal | None]]]] = {}
    if sessions and symbols:
        q = select(
            EquityEod.symbol, EquityEod.day, EquityEod.series, EquityEod.qty, EquityEod.turnover_lacs
        ).where(
            EquityEod.symbol.in_(symbols), EquityEod.day.in_(sessions), EquityEod.series.in_(PRICE_SERIES)
        )
        for sym, d, series, qty, tv in s.execute(q):
            per.setdefault(sym, {}).setdefault(d, {})[series] = (qty, tv)
    out = {}
    for sym in symbols:
        rows = {d: next(by[x] for x in PRICE_SERIES if x in by) for d, by in per.get(sym, {}).items()}
        out[sym] = adv(sessions, rows)
    return out


def nse_symbol(p: Any) -> str | None:
    if getattr(p, "nse_symbol", None):
        return p.nse_symbol
    kind, _, ident = p.key.partition(":")
    return ident if kind == "NSE" else None


def _r(x: float | None, nd: int = 2) -> float | None:
    return None if x is None else round(x, nd)


def liquidity(positions: Sequence[Any], advs: Mapping[str, Adv], *,
              participation: float = DEFAULT_PARTICIPATION) -> dict[str, Any]:  # fmt: skip
    """The payload: one row per position with value > 0 and a value-weighted summary (`within`: share of the
    portfolio sellable within N days; unknown and not-applicable shares stated separately)."""
    if not 0 < participation <= 1:
        raise ValueError("participation must be a fraction in (0, 1]")
    pos = [p for p in positions if p.value > 0]
    total = sum(p.value for p in pos)
    rows, unknown, na, days_known = [], 0.0, 0.0, []
    for p in pos:
        row: dict[str, Any] = {"key": p.key, "name": p.name, "asset_type": p.asset_type, "units": p.units,
                               "value": _r(p.value), "weight": _r(p.value / total, 4) if total else None,
                               "symbol": None, "adv_shares": None, "adv_rupees": None, "sessions": None,
                               "pct_of_adv": None, "days": None, "status": "unknown", "reason": None}  # fmt: skip
        sym = nse_symbol(p)
        if p.asset_type == "mf":
            row.update(status="not_applicable", reason=FUND_REASON)
            na += p.value
        elif sym is None:
            row["reason"] = "no NSE volume: the holding has no NSE symbol and BSE volume is not collected"
            unknown += p.value
        else:
            a = advs.get(sym)
            row["symbol"] = sym
            if a is None or a.shares is None or a.shares <= 0:
                row["reason"] = (a.reason if a else None) or "no NSE volume stored"
                unknown += p.value
            else:
                d = p.units / (participation * a.shares)
                row.update(status="known", adv_shares=_r(a.shares, 0), adv_rupees=_r(a.rupees, 0), days=_r(d, 2),
                           pct_of_adv=_r(p.units / a.shares * 100, 1),
                           sessions={"looked_at": a.days, "with_volume": a.days_with_rows,
                                     "first": a.first.isoformat() if a.first else None,
                                     "last": a.last.isoformat() if a.last else None})  # fmt: skip
                days_known.append((d, p.value))
        rows.append(row)
    # nothing known while some value is unknown: the share sellable is unknown, never "0 %"
    none_known = not days_known and unknown > 0
    within = {str(h): None if none_known or not total else _r(sum(v for d, v in days_known if d <= h) / total, 4)
              for h in HORIZONS}  # fmt: skip
    summary = {"within": within, "unknown": _r(unknown / total, 4) if total else None,
               "not_applicable": _r(na / total, 4) if total else None,
               "max_days": _r(max((d for d, _ in days_known), default=None), 2), "complete": unknown == 0}  # fmt: skip
    rows.sort(key=lambda r: (r["days"] is None, -(r["days"] or 0)))
    return {"participation": participation, "adv_sessions": ADV_SESSIONS, "min_sessions": MIN_SESSIONS,
            "horizons": list(HORIZONS), "method": METHOD, "positions": rows, "summary": summary}  # fmt: skip

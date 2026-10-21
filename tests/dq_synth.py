"""Synthetic F&O days for the data-quality tests (#368): a made-up universe of two index futures (NIFTY, BANKNIFTY),
two stock futures (ABCSTK, XYZSTK, fake symbols), NIFTY options on two expiries, the bhavcopy manifest with 30 days of
row-count history, participant OI that reconciles with the futures, and index closes with India VIX. Prices drift
deterministically day to day so a copied day is visible. No network, no real data."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

E1, E2 = date(2026, 10, 27), date(2026, 11, 24)
T0, T1 = date(2026, 9, 29), date(2026, 9, 30)  # Tuesday, Wednesday
LOTS = {"NIFTY": 75, "BANKNIFTY": 35, "ABCSTK": 500, "XYZSTK": 250}
BASE = {"NIFTY": 25000.0, "BANKNIFTY": 55000.0, "ABCSTK": 1200.0, "XYZSTK": 800.0}
STRIKES = (24800, 25000, 25200)


def price(sym: str, day: date) -> float:
    return BASE[sym] * (1 + 0.003 * (day - T0).days)


def _row(day, sym, inst, expiry, strike, right, px, lot, oi_lots, contracts):
    p = Decimal(str(round(px, 2)))
    return {"day": day, "symbol": sym, "instrument": inst, "expiry": expiry, "strike": Decimal(strike),
            "option_type": right, "open": p, "high": p * Decimal("1.01"), "low": p * Decimal("0.99"), "close": p,
            "settle": p + Decimal("0.05"), "oi": oi_lots * lot, "chg_oi": 0, "contracts": contracts,
            "turnover": Decimal(contracts * lot) * p, "underlying": None, "lot_size": lot}  # fmt: skip


def bhav_rows(day: date) -> list[dict]:
    out = []
    for sym, lot in LOTS.items():
        inst = "IDF" if sym in ("NIFTY", "BANKNIFTY") else "STF"
        for i, e in enumerate((E1, E2)):
            r = _row(day, sym, inst, e, 0, "XX", price(sym, day) * (1 + 0.004 * i), lot, 1000 - 400 * i, 50)
            r["underlying"] = Decimal(str(round(price(sym, day), 2))) if inst == "IDF" else None
            out.append(r)
    for e in (E1, E2):
        for k in STRIKES:
            for right in ("CE", "PE"):
                prem = 100 + abs(k - price("NIFTY", day)) * 0.1 + (e - day).days
                out.append(_row(day, "NIFTY", "IDO", e, k, right, prem, 75, 200, 10))
    return out


def insert_bhav(s, rows: list[dict]) -> None:
    from sqlalchemy import insert

    from finresearch.db.models import FnoBhav

    if rows:
        s.execute(insert(FnoBhav), rows)


def manifest(s, upto: date, n: int = 30, kept: int = 32, total: int = 40) -> list[date]:
    """Loaded manifest rows for the `n` weekdays ending `upto`, rows_kept/rows_total with a little noise."""
    from finresearch.db.models import FnoBhavDay

    days, d = [], upto
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    for i, d in enumerate(sorted(days)):
        s.add(FnoBhavDay(day=d, status="loaded", format="udiff", url="synthetic", rows_total=total + i % 7,
                         rows_kept=kept + i % 5, rows_dead=0, stock_symbols=[]))  # fmt: skip
    return sorted(days)

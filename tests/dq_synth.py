"""Synthetic F&O days for the data-quality tests (#368): a made-up universe of two index futures (NIFTY, BANKNIFTY),
two stock futures (ABCSTK, XYZSTK, fake symbols), NIFTY options on two expiries (Black-76 prices off each expiry's
future, so the option-price screens of dq.options see an arbitrage-free day), the bhavcopy manifest with 30 days of
row-count history, participant OI that reconciles with the futures, and index closes with India VIX. Prices drift
deterministically day to day so a copied day is visible. No network, no real data."""

from __future__ import annotations

import math
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


SIGMA, RATE = 0.12, 0.06  # the options: Black-76 at a flat 12 % volatility and a 6 % rate (arbitrage-free)


def option_price(day: date, expiry: date, strike: float, right: str, fwd: float) -> float:
    """Black-76 off the same-expiry future's settle, T = calendar days / 365 (fincalc.fno_history's clock)."""
    from finresearch.fincalc import black76

    t = (expiry - day).days / 365
    return black76.price(right, fwd, strike, t, math.exp(-RATE * t), SIGMA)  # type: ignore[arg-type]


def bhav_rows(day: date) -> list[dict]:
    out, fut = [], {}
    for sym, lot in LOTS.items():
        inst = "IDF" if sym in ("NIFTY", "BANKNIFTY") else "STF"
        for i, e in enumerate((E1, E2)):
            r = _row(day, sym, inst, e, 0, "XX", price(sym, day) * (1 + 0.004 * i), lot, 1000 - 400 * i, 50)
            r["underlying"] = Decimal(str(round(price(sym, day), 2))) if inst == "IDF" else None
            fut[(sym, e)] = float(r["settle"])
            out.append(r)
    for e in (E1, E2):
        for k in STRIKES:
            for right in ("CE", "PE"):
                prem = option_price(day, e, k, "call" if right == "CE" else "put", fut[("NIFTY", e)])
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


BUCKETS = ("fut_idx", "fut_stk", "opt_idx_call", "opt_idx_put", "opt_stk_call", "opt_stk_put")


def participants(day: date) -> list[dict]:
    """Four participants whose futures longs (and shorts) add up to the synthetic futures' open interest in contracts:
    index futures (1000 + 600 lots) x 2 symbols = 3,200, stock futures the same. Options balance among themselves."""
    share = {"Client": 0.4, "DII": 0.1, "FII": 0.3, "Pro": 0.2}
    out = []
    for who, w in share.items():
        r: dict = {"day": day, "client_type": who, "source": "synthetic"}
        for b in BUCKETS:
            n = int(3200 * w) if b.startswith("fut") else int(10000 * w)
            if b.startswith("fut"):
                r[f"{b}_long"], r[f"{b}_short"] = n, n
            else:
                kind, right = b.rsplit("_", 1)
                r[f"{kind}_{right}_long"], r[f"{kind}_{right}_short"] = n, n
        r["total_long"] = sum(v for k, v in r.items() if k.endswith("_long"))
        r["total_short"] = sum(v for k, v in r.items() if k.endswith("_short"))
        out.append(r)
    return out


def index_rows(day: date, vix: float = 13.0) -> list[dict]:
    out = []
    for sym in ("NIFTY", "BANKNIFTY", "INDIA VIX"):
        c = Decimal(str(round(price(sym, day), 2))) if sym in BASE else Decimal(str(vix))
        out.append({"day": day, "symbol": sym, "index_name": sym, "open": c, "high": c * Decimal("1.01"),
                    "low": c * Decimal("0.99"), "close": c})  # fmt: skip
    return out

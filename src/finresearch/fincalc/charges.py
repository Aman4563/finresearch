"""Statutory and exchange charges on NSE equity-derivative trades, as a dated table (analysis only; no orders).

Every rate is a row with the date it applies from, its basis, its source and how sure we are of it:
- ``verified``: read from a primary source (statute, exchange or regulator);
- ``secondary``: read from a broker or tax-aggregator page on the stated date, not from the primary circular;
- ``unconfirmed``: announced but enactment or the start date is not confirmed (docs/dev/RESEARCH_ROADMAP.md §D.5
  marks the Finance Bill 2026 STT rates [U]; Zerodha's charges page applied the same 0.15 % / 0.05 % on 30-Sep-2026),
  or a rate we could not re-read from a source.

Rates change with budgets and exchange circulars, so callers can override any key (``overrides``) and the API shows
the rows it used. Brokerage is not statutory: it is a flat amount per executed order that the user sets in the
profile (default ₹20, a common discount-broker price).

Sources (fetched 30-Sep-2026):
- STT from 1-Apr-2026, Finance Bill 2026 memorandum: https://www.indiabudget.gov.in/doc/memo.pdf (roadmap [56]) and
  ClearTax: https://cleartax.in/s/securities-transaction-tax-stt (roadmap [60]; "the new STT rate for futures as
  proposed in Budget 2026 is 0.05% which is a 150% increase from the existing 0.02%"; options 0.15 %).
- Exchange transaction charges, SEBI fee, stamp duty, GST: Zerodha's charges page https://zerodha.com/charges
  (NSE options 0.03553 % of premium, NSE futures 0.00183 %, SEBI ₹10/crore, stamp 0.003 % / 0.002 % on the buy side,
  GST 18 % on brokerage + SEBI + transaction charges). A broker page, not the NSE circular, hence ``secondary``;
  the date each rate started is not verified, so ``effective_from`` is the earliest date we can vouch for.
- IPFT (₹0.01 per crore) is left out: under a paisa per crore of turnover.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal
from typing import Literal

Status = Literal["verified", "secondary", "unconfirmed"]
Side = Literal["buy", "sell"]

BUDGET_2026 = "https://www.indiabudget.gov.in/doc/memo.pdf"
CLEARTAX_STT = "https://cleartax.in/s/securities-transaction-tax-stt"
ZERODHA_CHARGES = "https://zerodha.com/charges"
DEFAULT_BROKERAGE_PER_ORDER = Decimal(20)
CRORE = 10_000_000


@dataclass(frozen=True)
class ChargeRate:
    key: str
    rate: Decimal  # a fraction of the basis (0.0015 = 0.15 %)
    basis: str
    effective_from: date
    source: str
    status: Status
    note: str = ""

    def to_json(self) -> dict:
        return {"key": self.key, "rate_pct": float(self.rate * 100), "basis": self.basis,
                "effective_from": self.effective_from.isoformat(), "source": self.source, "status": self.status,
                "note": self.note}  # fmt: skip


def _r(
    key: str, pct: str, basis: str, since: date, source: str, status: Status, note: str = ""
) -> ChargeRate:
    return ChargeRate(key, Decimal(pct) / 100, basis, since, source, status, note)


_SEEN = "rate as published on 30-Sep-2026; the date it started is not verified"
# the table: several rows per key, the latest one on or before the trade date applies
RATES: tuple[ChargeRate, ...] = (
    _r("stt_option_sell", "0.1", "option premium, sell side", date(2024, 10, 1), CLEARTAX_STT, "unconfirmed",
       "Finance (No. 2) Act 2024 rate; not re-read from the Act (ClearTax no longer lists it)"),
    _r("stt_option_exercise", "0.125", "intrinsic value of long options exercised at expiry", date(2024, 10, 1),
       CLEARTAX_STT, "unconfirmed", "Finance (No. 2) Act 2024 rate; not re-read from the Act"),
    _r("stt_futures_sell", "0.02", "futures turnover, sell side", date(2024, 10, 1), CLEARTAX_STT, "secondary",
       "Finance (No. 2) Act 2024 rate (\"the existing 0.02%\")"),
    _r("stt_option_sell", "0.15", "option premium, sell side", date(2026, 4, 1), BUDGET_2026, "unconfirmed",
       "Finance Bill 2026; enactment of the Finance Act 2026 not confirmed (roadmap §D.5)"),
    _r("stt_option_exercise", "0.15", "intrinsic value of long options exercised at expiry", date(2026, 4, 1),
       BUDGET_2026, "unconfirmed", "Finance Bill 2026; enactment not confirmed"),
    _r("stt_futures_sell", "0.05", "futures turnover, sell side", date(2026, 4, 1), BUDGET_2026, "unconfirmed",
       "Finance Bill 2026; enactment not confirmed"),
    _r("exchange_option", "0.03553", "option premium, both sides (NSE)", date(2026, 9, 30), ZERODHA_CHARGES,
       "secondary", _SEEN),
    _r("exchange_futures", "0.00183", "futures turnover, both sides (NSE)", date(2026, 9, 30), ZERODHA_CHARGES,
       "secondary", _SEEN),
    _r("sebi_fee", "0.0001", "turnover, both sides (₹10 per crore)", date(2026, 9, 30), ZERODHA_CHARGES,
       "secondary", _SEEN),
    _r("stamp_option_buy", "0.003", "option premium, buy side", date(2026, 9, 30), ZERODHA_CHARGES, "secondary",
       _SEEN),
    _r("stamp_futures_buy", "0.002", "futures turnover, buy side", date(2026, 9, 30), ZERODHA_CHARGES,
       "secondary", _SEEN),
    _r("gst", "18", "brokerage + exchange charges + SEBI fee", date(2026, 9, 30), ZERODHA_CHARGES, "secondary",
       _SEEN),
)  # fmt: skip
KEYS = tuple(dict.fromkeys(r.key for r in RATES))


def rates_as_of(
    on: date, overrides: Mapping[str, Decimal | float | str] | None = None
) -> dict[str, ChargeRate]:
    """The rate of every key in force on `on`. Before a key's first row, its earliest row is used (noted). An
    override (a fraction, e.g. 0.001 for 0.1 %) replaces the rate and is marked as the user's."""
    out: dict[str, ChargeRate] = {}
    for key in KEYS:
        rows = sorted((r for r in RATES if r.key == key), key=lambda r: r.effective_from)
        live = [r for r in rows if r.effective_from <= on]
        row = (
            live[-1]
            if live
            else replace(rows[0], note=f"{rows[0].note}; used for an earlier date".lstrip("; "))
        )
        if overrides and key in overrides:
            row = replace(row, rate=Decimal(str(overrides[key])), source="user override", status="verified",
                          note="set by the user")  # fmt: skip
        out[key] = row
    return out


@dataclass(frozen=True)
class TradeLeg:
    """One order: `price` is the option premium or the futures price per unit; `qty` is units (lots × lot size)."""

    kind: Literal["option", "future"]
    side: Side
    price: float
    qty: int


@dataclass
class Costs:
    lines: dict[str, float]  # stt, exchange, sebi, stamp, brokerage, gst
    total: float
    turnover: float
    orders: int
    rates: list[ChargeRate] = field(default_factory=list)

    def to_json(self) -> dict:
        return {"lines": {k: round(v, 2) for k, v in self.lines.items()}, "total": round(self.total, 2),
                "turnover": round(self.turnover, 2), "orders": self.orders,
                "rates": [r.to_json() for r in self.rates]}  # fmt: skip


def order_costs(legs: Sequence[TradeLeg], on: date, brokerage_per_order: Decimal | float = DEFAULT_BROKERAGE_PER_ORDER,
                overrides: Mapping[str, Decimal | float | str] | None = None) -> Costs:  # fmt: skip
    """Charges for executing `legs` (one order each) on `on`:
    STT (sell side), exchange transaction charges and SEBI fee (both sides), stamp duty (buy side), flat brokerage per
    order, and GST on brokerage + exchange charges + SEBI fee."""
    r = {k: float(v.rate) for k, v in rates_as_of(on, overrides).items()}
    stt = exch = sebi = stamp = turnover = 0.0
    for leg in legs:
        value = abs(leg.price * leg.qty)
        turnover += value
        opt = leg.kind == "option"
        exch += value * r["exchange_option" if opt else "exchange_futures"]
        sebi += value * r["sebi_fee"]
        if leg.side == "sell":
            stt += value * r["stt_option_sell" if opt else "stt_futures_sell"]
        else:
            stamp += value * r["stamp_option_buy" if opt else "stamp_futures_buy"]
    brokerage = float(brokerage_per_order) * len(legs)
    gst = (brokerage + exch + sebi) * r["gst"]
    lines = {"stt": stt, "exchange": exch, "sebi": sebi, "stamp": stamp, "brokerage": brokerage, "gst": gst}
    used = rates_as_of(on, overrides)
    return Costs(lines, sum(lines.values()), turnover, len(legs), list(used.values()))


def exercise_stt(
    intrinsic_value: float, on: date, overrides: Mapping[str, Decimal | float | str] | None = None
) -> float:
    """STT on long options that expire in the money and are exercised: a rate on the total intrinsic value (₹)."""
    return max(0.0, intrinsic_value) * float(rates_as_of(on, overrides)["stt_option_exercise"].rate)


def flip(legs: Sequence[TradeLeg]) -> list[TradeLeg]:
    """The closing orders for `legs` (same prices): used to show what squaring off before expiry would cost."""
    return [replace(x, side="sell" if x.side == "buy" else "buy") for x in legs]

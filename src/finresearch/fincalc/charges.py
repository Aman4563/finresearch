"""Statutory and exchange charges on NSE equity-derivative trades, as a dated table (analysis only; no orders).

Every rate is a row with the date it applies from, its basis, its source and how sure we are of it:
- ``verified``: read from a primary source (statute, exchange or regulator);
- ``secondary``: read from a broker or tax-aggregator page on the stated date, or the start date is not from a primary
  source;
- ``unconfirmed``: announced but enactment or the start date is not confirmed (docs/dev/RESEARCH_ROADMAP.md §D.5
  marks the Finance Bill 2026 STT rates [U]; Zerodha's charges page applied the same 0.15 % / 0.05 % on 30-Sep-2026),
  or a rate we could not re-read from a source.

Rates change with budgets and exchange circulars, so callers can override any key (``overrides``) and the API shows
the rows it used. Brokerage is not statutory: it is a flat amount per executed order that the user sets in the
profile (default ₹20, a common discount-broker price).

Sources:
- STT from 1-Apr-2026, Finance Bill 2026 memorandum: https://www.indiabudget.gov.in/doc/memo.pdf (roadmap [56]) and
  ClearTax: https://cleartax.in/s/securities-transaction-tax-stt (roadmap [60]; fetched 30-Sep-2026).
- 2013 to Sep-2026 (#382 (f), fetched 22-Oct-2026): STT from NSE's FATAX circulars quoting each Finance Act; NSE
  transaction charges from NSE's FA circulars (each row names its circular); SEBI fee, stamp duty and GST from NSE's
  levies page and SEBI's press release 24/2020. The Finance Act texts, the stamp-duty gazette notification and the
  CBIC GST notification themselves were not read (each row's note says so where it matters).
- Exchange charges before 1-Oct-2024 were slabbed by a member's monthly turnover; the table uses the first slab, the
  rate a retail broker passes on. From 1-Apr-2023 the exchange rows include NSE's IPFT contribution (Rs 50 per crore
  of options premium, Rs 10 per crore of futures); before that IPFT was Rs 0.01 per crore and is left out.
- Before a key's earliest row (pre-2013 STT, pre-2021 exchange charges: the vault years) the earliest row is still
  used and noted "used for an earlier date"; #365 adds the older rows.
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
_NSE = "https://nsearchives.nseindia.com/content/circulars/"
NSE_STT_2013, NSE_STT_2016, NSE_STT_2019 = (
    _NSE + "FATAX23500.pdf",
    _NSE + "FATAX32385.pdf",
    _NSE + "FATAX41919.pdf",
)
NSE_STT_2023 = "https://archives.nseindia.com/content/circulars/FATAX56235.pdf"
NSE_STT_2024 = _NSE + "FATAX63809.pdf"
NSE_TX_2021, NSE_TX_2023, NSE_TX_2024 = _NSE + "FA46730.pdf", _NSE + "FA56129.pdf", _NSE + "FA61137.pdf"
NSE_TX_2024_10 = _NSE + "FA64232.pdf"
NSE_LEVIES = "https://www.nseindia.com/static/invest/first-time-investor-sebi-turnover-fees-stt-other-levies"
SEBI_2020 = ("https://www.sebi.gov.in/media/press-releases/apr-2020/"
             "sebi-reduces-broker-turnover-fees-and-filing-fees-for-issuers_46571.html")  # fmt: skip
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
_SLAB = ("NSE's first-slab rate (the turnover slab a retail broker's clients pay; lower slabs apply only to a "
         "member's turnover above it)")  # fmt: skip
_IPFT = "plus NSE's IPFT contribution (Rs 50/crore of options premium, Rs 10/crore of futures, FA/56129)"
# the table: several rows per key, the latest one on or before the trade date applies
RATES: tuple[ChargeRate, ...] = (
    # STT (Securities Transaction Tax), from NSE's circulars quoting each Finance Act (fetched 22-Oct-2026)
    _r("stt_option_sell", "0.05", "option premium, sell side", date(2016, 6, 1), NSE_STT_2016, "verified",
       "Finance Act 2016: 0.017 % -> 0.05 % from 1-Jun-2016 (NSE/FATAX/32385)"),
    _r("stt_option_sell", "0.0625", "option premium, sell side", date(2023, 4, 1), NSE_STT_2023, "verified",
       "Finance Act 2023 (No. 8 of 2023): 0.05 % -> 0.0625 % from 1-Apr-2023 (NSE/FATAX/56235)"),
    _r("stt_option_sell", "0.1", "option premium, sell side", date(2024, 10, 1), NSE_STT_2024, "verified",
       "Finance (No. 2) Act 2024: 0.0625 % -> 0.10 % from 1-Oct-2024 (NSE/FATAX/63809)"),
    _r("stt_option_exercise", "0.125", "intrinsic value of long options exercised at expiry", date(2019, 9, 1),
       NSE_STT_2019, "verified", "on the intrinsic value instead of the settlement price from 1-Sep-2019 "
       "(Finance (No. 2) Act 2019, NSE/FATAX/41919); unchanged by the 2023 and 2024 Acts (NSE/FATAX/56235, 63809)"),
    _r("stt_futures_sell", "0.01", "futures turnover, sell side", date(2013, 6, 1), NSE_STT_2013, "verified",
       "0.017 % -> 0.01 % from 1-Jun-2013 (NSE/FATAX/23500)"),
    _r("stt_futures_sell", "0.0125", "futures turnover, sell side", date(2023, 4, 1), NSE_STT_2023, "verified",
       "Finance Act 2023: 0.01 % -> 0.0125 % from 1-Apr-2023 (NSE/FATAX/56235)"),
    _r("stt_futures_sell", "0.02", "futures turnover, sell side", date(2024, 10, 1), NSE_STT_2024, "verified",
       "Finance (No. 2) Act 2024: 0.0125 % -> 0.02 % from 1-Oct-2024 (NSE/FATAX/63809)"),
    _r("stt_option_sell", "0.15", "option premium, sell side", date(2026, 4, 1), BUDGET_2026, "unconfirmed",
       "Finance Bill 2026 memo p.62-63 (re-read 1-Oct-2026: 0.1 % -> 0.15 % of premium, for options entered into on "
       "or after 1-Apr-2026); enactment of the Finance Act 2026 not confirmed (roadmap §D.5)"),
    _r("stt_option_exercise", "0.15", "intrinsic value of long options exercised at expiry", date(2026, 4, 1),
       BUDGET_2026, "unconfirmed", "Finance Bill 2026 memo p.62-63: 0.125 % -> 0.15 % of the intrinsic price; "
       "enactment not confirmed"),
    _r("stt_futures_sell", "0.05", "futures turnover, sell side", date(2026, 4, 1), BUDGET_2026, "unconfirmed",
       "Finance Bill 2026 memo p.62-63: 0.02 % -> 0.05 % of the traded price; enactment not confirmed"),
    # NSE transaction charges, each side; IPFT included from 1-Apr-2023 (Rs 0.01/crore before: negligible)
    _r("exchange_option", "0.053", "option premium, both sides (NSE)", date(2021, 1, 1), NSE_TX_2021, "verified",
       f"Rs 53.00 per lakh of premium from 1-Jan-2021 (NSE/FA/46730); {_SLAB}"),
    _r("exchange_option", "0.0505", "option premium, both sides (NSE)", date(2023, 4, 1), NSE_TX_2023, "verified",
       f"Rs 50.00 per lakh from 1-Apr-2023 (NSE/FA/56129) {_IPFT}; {_SLAB}"),
    _r("exchange_option", "0.05", "option premium, both sides (NSE)", date(2024, 4, 1), NSE_TX_2024, "verified",
       f"Rs 49.50 per lakh from 1-Apr-2024 (NSE/FA/61137) {_IPFT}; {_SLAB}"),
    _r("exchange_option", "0.03553", "option premium, both sides (NSE)", date(2024, 10, 1), NSE_TX_2024_10,
       "verified", f"Rs 35.03 per lakh, flat, from 1-Oct-2024 (NSE/FA/64232, SEBI 'true to label' circular "
       f"SEBI/HO/MRD/TPD-1/P/CIR/2024/92) {_IPFT}; the same total since 1-Mar-2026 (NSE/FA/73061)"),
    _r("exchange_futures", "0.002", "futures turnover, both sides (NSE)", date(2021, 1, 1), NSE_TX_2021,
       "verified", f"Rs 2.00 per lakh from 1-Jan-2021 (NSE/FA/46730); {_SLAB}"),
    _r("exchange_futures", "0.0020", "futures turnover, both sides (NSE)", date(2023, 4, 1), NSE_TX_2023,
       "verified", f"Rs 1.90 per lakh from 1-Apr-2023 (NSE/FA/56129) {_IPFT}; {_SLAB}"),
    _r("exchange_futures", "0.00198", "futures turnover, both sides (NSE)", date(2024, 4, 1), NSE_TX_2024,
       "verified", f"Rs 1.88 per lakh from 1-Apr-2024 (NSE/FA/61137) {_IPFT}; {_SLAB}"),
    _r("exchange_futures", "0.00183", "futures turnover, both sides (NSE)", date(2024, 10, 1), NSE_TX_2024_10,
       "verified", f"Rs 1.73 per lakh, flat, from 1-Oct-2024 (NSE/FA/64232) {_IPFT}"),
    # SEBI turnover fee: Rs 10/crore; halved June 2020 to March 2021 (SEBI PR 24/2020), so dated from April 2021
    _r("sebi_fee", "0.0001", "turnover, both sides (₹10 per crore)", date(2021, 4, 1), NSE_LEVIES, "verified",
       f"Rs 10 per crore (NSE's levies page; SEBI board memo Jul-2020); 50 % off June 2020 to March 2021 ({SEBI_2020}); "
       "the date it first applied is not verified"),
    _r("stamp_option_buy", "0.003", "option premium, buy side", date(2020, 7, 1), NSE_LEVIES, "verified",
       "Indian Stamp Act as amended by the Finance Act 2019, uniform from 1-Jul-2020 (NSE's levies page; the "
       "gazette notification not read)"),
    _r("stamp_futures_buy", "0.002", "futures turnover, buy side", date(2020, 7, 1), NSE_LEVIES, "verified",
       "Indian Stamp Act as amended by the Finance Act 2019, uniform from 1-Jul-2020 (NSE's levies page)"),
    _r("gst", "18", "brokerage + exchange charges + SEBI fee", date(2017, 7, 1), NSE_LEVIES, "secondary",
       "18 % on stock brokers' services (NSE's levies page); 1-Jul-2017 is GST's start, the CBIC rate notification "
       "was not read"),
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

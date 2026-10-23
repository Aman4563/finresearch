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
- Before 1-Jul-2020 stamp duty is Maharashtra's 0.002 % and NSE's options charge before 1-Apr-2017 is PTI's Rs 50 per
  lakh, both secondary and both the owner's choice of 23-Oct-2026 (#471); each row's note gives the caveats.
- Before a key's earliest row, and in a period recorded with no rate, the rate is unknown (#365): order_costs and
  exercise_stt raise ChargesUnknown and the backtest skips that entry. A later rate is never used for an earlier date.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal
from typing import Literal

Status = Literal["verified", "secondary", "unconfirmed", "unknown"]
Side = Literal["buy", "sell"]

BUDGET_2026 = "https://www.indiabudget.gov.in/doc/memo.pdf"
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
# 2008-2020 (#365, fetched 22-Oct-2026)
NSE_STT_2008, NSE_ST_2008 = _NSE + "faac10706.htm", _NSE + "faac10704.htm"
NSE_TX_2009, NSE_TX_2017 = _NSE + "faac13029.pdf", _NSE + "FA34601.pdf"
FINANCE_BILL_2006 = "https://www.indiabudget.gov.in/budget_archive/ub2006-07/fb/bill11.pdf"
FINANCE_BILL_2008 = "https://www.indiabudget.gov.in/budget_archive/ub2008-09/fb/bill61.pdf"
SEBI_FEES_REGS = "https://www.sebi.gov.in/acts/stockreg1.pdf"
SEBI_FEE_2009 = ("https://www.cse-india.com/upload/CSE%20Notices%20&%20Circulars/2009/"
                 "Annexure%201%20(payment%20of%20fees).pdf")  # fmt: skip
SEBI_FEE_2014 = _NSE + "COMP26781.zip"
SEBI_FEE_2017 = "https://www.sebi.gov.in/sebi_data/attachdocs/1488970004632.pdf"
SEBI_FEE_2019 = "https://www.cse-india.com/upload/cse_notice/REVISED_SEBI_TURNOVER_FEES.htm"
TRU_2007 = "https://www.indiabudget.gov.in/budget_archive/ub2007-08/cen/dojstru1.pdf"
ST_2009_2012 = "https://indiabudget.gov.in/budget2012-2013/ub2012-13/cen/st0212.pdf"
TRU_2015 = "https://www.indiabudget.gov.in/budget2015-2016/ub2015-16/cen/dojstru2.pdf"
TRU_2016 = "https://www.indiabudget.gov.in/budget2016-2017/ub2016-17/cen/dojstru2.pdf"
NSE_STAMP = "https://www.nseindia.com/static/regulations/member-compliance-stamp-duty"
# owner's decisions of 23-Oct-2026 (#471): pre-1-Jul-2020 stamp duty and pre-1-Apr-2017 NSE options charge (secondary)
MH_STAMP_2005 = "https://taxindiaonline.com/RC2/pdfdocs/ordinance/maharashtra_stampduty_ordinance.pdf"
PTI_2017 = ("https://www.business-standard.com/amp/article/markets/"
            "nse-cuts-fee-on-options-currency-derivatives-to-deepen-market-117040900174_1.html")  # fmt: skip
EXERCISE_ON_INTRINSIC_FROM = date(2019, 9, 1)  # before: 0.125 % of the settlement price (Finance Act 2008)
DEFAULT_BROKERAGE_PER_ORDER = Decimal(20)
CRORE = 10_000_000


@dataclass(frozen=True)
class ChargeRate:
    key: str
    rate: Decimal | None  # a fraction of the basis (0.0015 = 0.15 %); None = unknown (no sourced rate)
    basis: str
    effective_from: date
    source: str
    status: Status
    note: str = ""

    def to_json(self) -> dict:
        return {"key": self.key, "rate_pct": None if self.rate is None else float(self.rate * 100), "basis": self.basis,
                "effective_from": self.effective_from.isoformat(), "source": self.source, "status": self.status,
                "note": self.note}  # fmt: skip


def _r(
    key: str, pct: str | None, basis: str, since: date, source: str, status: Status, note: str = ""
) -> ChargeRate:
    return ChargeRate(key, None if pct is None else Decimal(pct) / 100, basis, since, source, status, note)


class ChargesUnknown(ValueError):
    """A charge needed for a trade date has no sourced rate (#365): the trade cannot be priced, never at 0 or at a
    later date's rate."""


_SLAB = ("NSE's first-slab rate (the turnover slab a retail broker's clients pay; lower slabs apply only to a "
         "member's turnover above it)")  # fmt: skip
_IPFT = "plus NSE's IPFT contribution (Rs 50/crore of options premium, Rs 10/crore of futures, FA/56129)"
_MH_STAMP = ("Maharashtra's rate, the owner's choice of 23-Oct-2026 (#471: Maharashtra, NSE's home state; before "
             "1-Jul-2020 stamp duty was the client's state's, NSE's member-compliance page): 20 paise per Rs 10,000 "
             "(0.002 %) on F&O trades, Bombay Stamp Act Art. 51A(c) as inserted by Maharashtra Ordinance No. II of "
             "2005 (7-May-2005, in force at once), read from a third-party copy of the ordinance; the 'or part "
             "thereof' rounding is not modelled; later Maharashtra amendments up to 2020 were not checked.")  # fmt: skip
# the table: several rows per key, the latest one on or before the trade date applies
RATES: tuple[ChargeRate, ...] = (
    # 2008-2020 (#365). A row with no rate (None) records a period with no single sourced rate: unknown, never 0.
    _r("stt_option_sell", None, "option premium, sell side", date(2008, 1, 1), NSE_STT_2008, "unknown",
       "until 31-May-2008 option-sale STT was 0.017 % of strike + premium (Finance Act 2006; NSE/F&A/10706), a basis "
       "the order legs do not carry"),
    _r("stt_option_sell", "0.017", "option premium, sell side", date(2008, 6, 1), NSE_STT_2008, "verified",
       "Finance Act 2008 (clause 119 of the Bill): 0.017 % of the premium from 1-Jun-2008 (NSE/F&A/10706); "
       f"unchanged by the Finance Act 2013 (NSE/FATAX/23500); {FINANCE_BILL_2008}"),
    _r("stt_option_exercise", "0.125", "settlement price x quantity of long options exercised", date(2008, 6, 1),
       NSE_STT_2008, "verified", "Finance Act 2008 s.99(a)(ii): 0.125 % of the settlement price, paid by the "
       "purchaser (NSE/F&A/10706); the statute says 'settlement price', x quantity is our reading; on the "
       "intrinsic value only from 1-Sep-2019. Before 1-Jun-2008 not sourced"),
    _r("stt_futures_sell", "0.017", "futures turnover, sell side", date(2006, 6, 1), FINANCE_BILL_2006, "verified",
       "Finance Act 2006: 0.0133 % -> 0.017 % for derivatives from 1-Jun-2006 (in force on 1-Jan-2008, NSE/F&A/10706)"),
    _r("exchange_option", "0.05", "option premium, both sides (NSE)", date(2008, 1, 1), PTI_2017, "secondary",
       "secondary (PTI): Rs 50 per lakh of premium, the owner's choice of 23-Oct-2026 (#471). No NSE circular found; "
       "PTI (Business Standard, 9-Apr-2017) says NSE 'so far' levied Rs 5,000 on the first Rs 1 crore of monthly "
       "premium (= Rs 50 per lakh) with incentives above it (not modelled); Zerodha's Oct-2016 page said 0.053 %, "
       "possibly with clearing. The start date is not sourced (taken as in force on 1-Jan-2008)"),
    _r("exchange_option", "0.05", "option premium, both sides (NSE)", date(2017, 4, 1), NSE_TX_2017, "secondary",
       f"NSE/FA/34601: a flat Rs 2,500 a month up to Rs 3 crore of premium, then Rs 50 per lakh; the per-lakh band "
       f"is used, the monthly flat fee is not modelled; {_SLAB}"),
    _r("exchange_futures", "0.002", "futures turnover, both sides (NSE)", date(2008, 1, 1), NSE_TX_2009, "secondary",
       "Rs 2 per lakh, flat: NSE/F&A/13029 (Oct-2009) calls it the present rate; no revision in NSE's 2007-2009 "
       "circular list; the start date is not sourced (taken as in force on 1-Jan-2008)"),
    _r("exchange_futures", "0.0019", "futures turnover, both sides (NSE)", date(2009, 10, 1), NSE_TX_2009, "verified",
       f"Rs 1.90 per lakh up to Rs 2,500 crore a month from 1-Oct-2009 (NSE/F&A/13029; 'existing' in NSE/FA/46730); "
       f"{_SLAB}"),
    _r("sebi_fee", "0.0002", "turnover, both sides (₹20 per crore)", date(2006, 10, 1), SEBI_FEES_REGS, "verified",
       "SEBI (Stock Brokers) Regulations, Schedule III, footnote 94; options on premium (exercise notional not "
       "modelled)"),
    _r("sebi_fee", "0.0001", "turnover, both sides (₹10 per crore)", date(2009, 7, 1), SEBI_FEE_2009, "verified",
       "SEBI gazette notification in force from 1-Jul-2009 (text hosted by the Calcutta Stock Exchange)"),
    _r("sebi_fee", "0.0002", "turnover, both sides (₹20 per crore)", date(2014, 5, 23), SEBI_FEE_2014, "verified",
       "SEBI gazette LAD-NRO/GN/2014-15/03/1089, sent with NSE/COMP/26781"),
    _r("sebi_fee", "0.00015", "turnover, both sides (₹15 per crore)", date(2017, 4, 1), SEBI_FEE_2017, "verified",
       "SEBI notification of Mar-2017 (NSE/FA/34501)"),
    _r("sebi_fee", "0.0001", "turnover, both sides (₹10 per crore)", date(2019, 4, 1), SEBI_FEE_2019, "verified",
       "SEBI/LAD-NRO/GN/2019/03 from 1-Apr-2019 (text hosted by the Calcutta Stock Exchange)"),
    _r("sebi_fee", "0.00005", "turnover, both sides (₹5 per crore)", date(2020, 6, 1), SEBI_2020, "verified",
       "50 % off the ₹10 per crore from June 2020 to March 2021 (SEBI PR 24/2020)"),
    _r("stamp_option_buy", "0.002", "option premium, buy side", date(2005, 5, 7), MH_STAMP_2005, "secondary",
       f"{_MH_STAMP} The ordinance says 'futures and options trading' without a premium or notional basis: the "
       "premium is this table's reading, as for the uniform rate from 1-Jul-2020"),
    _r("stamp_futures_buy", "0.002", "futures turnover, buy side", date(2005, 5, 7), MH_STAMP_2005, "secondary",
       _MH_STAMP),
    _r("gst", None, "brokerage + exchange charges + SEBI fee", date(2008, 1, 1), NSE_ST_2008, "unknown",
       "until 15-May-2008 service tax applied to brokerage but not to NSE's charges (NSE/F&A/10704)"),
    _r("gst", "12.36", "brokerage + exchange charges + SEBI fee", date(2008, 5, 16), NSE_ST_2008, "secondary",
       f"service tax 12 % + 2 % + 1 % cesses ({TRU_2007}), on NSE's charges from 16-May-2008 (NSE/F&A/10704, "
       "subject line read only); whether the SEBI fee bore it is not checked"),
    _r("gst", "10.30", "brokerage + exchange charges + SEBI fee", date(2009, 2, 24), ST_2009_2012, "secondary",
       "service tax 10 % + cesses from 24-Feb-2009 (Notification 8/2009-ST; the 10 % from a secondary copy)"),
    _r("gst", "12.36", "brokerage + exchange charges + SEBI fee", date(2012, 4, 1), ST_2009_2012, "verified",
       "service tax 12 % + cesses from 1-Apr-2012 (Notification 2/2012-ST)"),
    _r("gst", "14", "brokerage + exchange charges + SEBI fee", date(2015, 6, 1), TRU_2015, "secondary",
       "service tax 14 % (cesses folded in): rate from the 2015 TRU letter, 1-Jun-2015 from Notification 14/2015-ST "
       "read in a secondary copy"),
    _r("gst", "14.5", "brokerage + exchange charges + SEBI fee", date(2015, 11, 15), TRU_2015, "secondary",
       "with Swachh Bharat cess 0.5 % from 15-Nov-2015 (Notifications 21-22/2015-ST, secondary copies)"),
    _r("gst", "15", "brokerage + exchange charges + SEBI fee", date(2016, 6, 1), TRU_2016, "verified",
       "with Krishi Kalyan cess 0.5 % from 1-Jun-2016 (2016 TRU letter)"),
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
    """The rate of every key in force on `on`. Before a key's first sourced row, or in a period recorded as unknown,
    the row is unknown (rate None, status "unknown"): an earlier date never borrows a later rate (#365). An override
    (a fraction, e.g. 0.001 for 0.1 %) replaces the rate and is marked as the user's."""
    out: dict[str, ChargeRate] = {}
    for key in KEYS:
        rows = sorted((r for r in RATES if r.key == key), key=lambda r: r.effective_from)
        live = [r for r in rows if r.effective_from <= on]
        if live and live[-1].rate is not None:
            row = live[-1]
        elif live:
            row = replace(live[-1], status="unknown")
        else:
            first = f" before {rows[0].effective_from:%d-%b-%Y}" if rows else ""
            row = ChargeRate(
                key, None, rows[0].basis if rows else "", on, "", "unknown", f"no sourced rate{first}"
            )
        if overrides and key in overrides:
            row = replace(row, rate=Decimal(str(overrides[key])), source="user override", status="verified",
                          note="set by the user")  # fmt: skip
        out[key] = row
    return out


def unknown_keys(on: date, overrides: Mapping[str, Decimal | float | str] | None = None) -> list[str]:
    """The keys with no sourced rate on `on` (empty when the date can be priced)."""
    return [k for k, v in rates_as_of(on, overrides).items() if v.rate is None]


def _known(rows: dict[str, ChargeRate], keys: Sequence[str], on: date) -> dict[str, float]:
    missing = [k for k in keys if rows[k].rate is None]
    if missing:
        raise ChargesUnknown(f"no sourced rate on {on:%d-%b-%Y} for {', '.join(missing)}")
    return {k: float(rows[k].rate) for k in keys}  # type: ignore[arg-type]


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
    used = rates_as_of(on, overrides)
    r = _known(used, [k for k in KEYS if k != "stt_option_exercise"], on)
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
    return Costs(lines, sum(lines.values()), turnover, len(legs), list(used.values()))


def exercise_stt(intrinsic_value: float, on: date, overrides: Mapping[str, Decimal | float | str] | None = None,
                 settlement_value: float | None = None) -> float:  # fmt: skip
    """STT on long options that expire in the money and are exercised (₹): from 1-Sep-2019 a rate on the total
    intrinsic value; before, on the settlement price x quantity (`settlement_value`; without it, unknown)."""
    rate = _known(rates_as_of(on, overrides), ["stt_option_exercise"], on)["stt_option_exercise"]
    if on >= EXERCISE_ON_INTRINSIC_FROM:
        return max(0.0, intrinsic_value) * rate
    if intrinsic_value <= 0:
        return 0.0
    if settlement_value is None:
        raise ChargesUnknown(f"exercise STT on {on:%d-%b-%Y} is on the settlement value, which was not given")
    return max(0.0, settlement_value) * rate


def flip(legs: Sequence[TradeLeg]) -> list[TradeLeg]:
    """The closing orders for `legs` (same prices): used to show what squaring off before expiry would cost."""
    return [replace(x, side="sell" if x.side == "buy" else "buy") for x in legs]

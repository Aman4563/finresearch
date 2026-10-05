"""The one rule for "the price" of a listed share: which exchange field to show, what to call it, and what the day's
change is measured from.

What the exchanges' fields hold (recorded payloads in tests/fixtures/prices/, notes in the research write-up
`price-semantics.md` of issue #133):

- NSE quote API (`GetQuoteApi?functionName=getSymbolData`): `tradeInfo.lastPrice` is the last traded price. It stays
  the last normal-market trade after 15:30 unless the stock trades in the 15:40-16:00 closing session (whose trades
  appear to execute at the closing price: observed, not found documented) (INFY 30-Sep-2026: lastPrice = closePrice = 994.10; TMCV the same day: lastPrice 420.00,
  closePrice 421.65). `metaData.closePrice` is NSE's official closing price, 0 until NSE publishes it. NSE's own
  `change`/`pChange` in that payload are lastPrice minus basePrice, so they are NOT the official day change after the
  close. `metaData.basePrice` (= `adjPrice` on an ex-date) is the previous close adjusted for a corporate action whose
  ex-date is today: SAIL 30-Sep-2026, ex-dividend Rs 2.35: previousClose 181.82, basePrice 179.47. NSE's pre-open page:
  "In case of corporate action, previous day's closing price is adjustable closing price or the base price."
- BSE header (`getScripHeaderData`): `LTP` and an unadjusted `PrevClose` (SAIL 30-Sep-2026: 183.10, BSE's 29-Sep close,
  no dividend adjustment) and no close field. BSE's official close is the daily bar / bhavcopy close; after BSE's
  closing session the LTP usually equals it (TMCV 30-Sep-2026: LTP 421.60 = bhavcopy ClsPric 421.60) but not for a
  stock with no trade then, so the BSE adapter reads the day's bar after the close (adapters.bse_equity).

The rule (`price_view`):
- in session (the quote's own timestamp is before 15:30 IST on the current day): the last traded price, "Last traded";
- once the session is over and the official close is published: the official close, "Close (official)", with the
  last trade kept alongside when it differs;
- over but the close not published yet (15:30 until NSE's closing session ends): the last trade, "Last traded (close
  not yet published)", with NSE's indicative close when it gives one;
- no trade at all: the previous close, "Previous close".
The day change is always measured from the adjusted base price when the exchange gives one (NSE), else from the
previous close less a cash dividend going ex today when the caller knows of one (BSE), else from the previous close.
A split or bonus going ex today on BSE makes the change not comparable; the caller passes `action_today` to say so.

The session test reads the quote's own timestamp, not the wall clock alone: a stale close from yesterday can never be
shown as today's price while the market is trading, because a quote stamped before 15:30 today is always "last traded".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

from finresearch.fincalc.dates import to_ist

NORMAL_CLOSE = time(15, 30)  # NSE/BSE normal market close (IST); the closing session runs 15:40-16:00

LAST_TRADED, OFFICIAL_CLOSE, PREVIOUS_CLOSE = "last_traded", "official_close", "previous_close"
LABELS = {LAST_TRADED: "Last traded", OFFICIAL_CLOSE: "Close (official)", PREVIOUS_CLOSE: "Previous close"}
REF_LABELS = {
    "base_price": "previous close adjusted for today's corporate action (exchange base price)",
    "listing_price": "the listing day's base price (the price discovered in the pre-open call auction)",
    "previous_close": "previous close",
    "previous_close_less_dividend": "previous close less today's ex-dividend",
}


def _pos(v: Any) -> Decimal | None:
    """A price the exchange actually published: None for missing, zero or negative placeholders."""
    if v is None:
        return None
    d = v if isinstance(v, Decimal) else Decimal(str(v))
    return d if d > 0 else None


def session_over(as_of: datetime | None, now: datetime | None = None) -> bool:
    """Whether the session the quote describes has ended: its timestamp is at or after 15:30 IST, or it is from an
    earlier day than `now`. A quote with no timestamp counts as over (nothing says it is live)."""
    if as_of is None:
        return True
    t = to_ist(as_of)
    if t.time() >= NORMAL_CLOSE:
        return True
    return bool(now is not None and to_ist(now).date() > t.date())


@dataclass
class PriceView:
    exchange: str
    price: Decimal | None
    kind: str
    as_of: datetime | None
    last_traded: Decimal | None
    official_close: Decimal | None
    previous_close: Decimal | None
    reference: Decimal | None
    reference_kind: str | None
    session: str  # "open" | "closing" (over, close not published) | "closed"
    notes: list[str] = field(default_factory=list)
    change_comparable: bool = True

    @property
    def label(self) -> str:
        if self.kind == LAST_TRADED and self.session == "closing":
            return "Last traded (close not yet published)"
        return LABELS[self.kind]

    @property
    def change(self) -> Decimal | None:
        if self.price is None or self.reference is None or not self.change_comparable:
            return None
        return self.price - self.reference

    @property
    def change_pct(self) -> Decimal | None:
        c = self.change
        return c / self.reference * 100 if c is not None and self.reference else None

    @property
    def differs(self) -> bool:
        """After the close: the last trade is not the official close (both are shown)."""
        return bool(self.kind == OFFICIAL_CLOSE and self.last_traded is not None
                    and self.official_close is not None and self.last_traded != self.official_close)  # fmt: skip

    @property
    def day(self) -> date | None:
        return to_ist(self.as_of).date() if self.as_of else None


def price_view(q: Any, *, exchange: str | None = None, now: datetime | None = None,
               dividend_today: Decimal | None = None, action_today: str | None = None) -> PriceView:  # fmt: skip
    """The display price of a quote (an adapters.nse.Quote or bse_equity.BseQuote; any object with the same fields).

    `dividend_today`: a cash dividend per share going ex on the quote's day, used for the change reference only when
    the exchange gives no adjusted base price (BSE). `action_today`: a non-dividend corporate action going ex that day
    (split, bonus, rights...) on an exchange without a base price: the change is then not computed."""
    ex = (exchange or getattr(q, "exchange", None) or "NSE").upper()
    last, close = _pos(getattr(q, "last_price", None)), _pos(getattr(q, "close_price", None))
    prev, base = _pos(getattr(q, "previous_close", None)), _pos(getattr(q, "base_price", None))
    as_of = getattr(q, "as_of", None)
    notes: list[str] = []
    over = session_over(as_of, now)
    if over and close is not None:
        kind, price, session = OFFICIAL_CLOSE, close, "closed"
        if last is not None and last != close:
            notes.append(f"Last trade ₹{last}; the official close ₹{close} is the exchange's published closing price, "
                         "which is computed from the session's closing trades and need not equal the last trade.")  # fmt: skip
    elif last is not None:
        kind, price = LAST_TRADED, last
        session = "closing" if over else "open"
        if over:
            notes.append(f"{ex} has not published the official close yet; this is the last trade.")
            ic = _pos(getattr(q, "indicative_close", None))
            if ic is not None:
                notes.append(f"{ex} indicative close: ₹{ic}.")
    else:
        kind, price, session = PREVIOUS_CLOSE, prev, "closed" if over else "open"
        notes.append("No trade yet in this session: showing the previous close.")
    comparable = True
    listing_day = getattr(q, "listing_date", None)
    if (
        base is not None
        and as_of is not None
        and listing_day is not None
        and to_ist(as_of).date() == listing_day
    ):
        # listing day: NSE's "previous close" is the issue price and the base price is the price discovered in the
        # special pre-open session (SEBI circular CIR/MRD/DP/01/2012, call auction for IPO listings), not a
        # corporate-action adjustment (#200: Orient Cables read "-10 % today, previous close ₹272 adjusted ...")
        ref, ref_kind = base, "listing_price"
        notes.append(f"Listing day: change measured from ₹{base}, the price discovered in the pre-open call auction"
                     + (f"; the issue price was ₹{prev}." if prev is not None else "."))  # fmt: skip
    elif base is not None:
        # the base price equals the previous close except on an ex-date; the label says "adjusted" only then
        ref, ref_kind = base, ("previous_close" if base == prev else "base_price")
        if prev is not None and base != prev:
            notes.append(f"Day change measured from ₹{base}, the previous close ₹{prev} adjusted for today's "
                         "corporate action (the exchange's base price).")  # fmt: skip
    elif prev is not None and dividend_today:
        ref, ref_kind = prev - dividend_today, "previous_close_less_dividend"
        notes.append(f"Day change measured from ₹{ref}: the previous close ₹{prev} less today's ex-dividend "
                     f"₹{dividend_today}.")  # fmt: skip
    else:
        ref, ref_kind = prev, ("previous_close" if prev is not None else None)
        if action_today and prev is not None:
            comparable = False
            notes.append(f"A corporate action goes ex today ({action_today}); {ex} gives no adjusted previous close, so "
                         "the day change is not shown.")  # fmt: skip
    if kind == PREVIOUS_CLOSE:
        ref, ref_kind = None, None
    return PriceView(exchange=ex, price=price, kind=kind, as_of=as_of, last_traded=last, official_close=close,
                     previous_close=prev, reference=ref, reference_kind=ref_kind, session=session, notes=notes,
                     change_comparable=comparable)  # fmt: skip


def display_price(q: Any, **kw: Any) -> Decimal | None:
    """Just the price `price_view` would show (for valuations, signals and alerts)."""
    return price_view(q, **kw).price if q is not None else None


def disagreement(a: Decimal | None, b: Decimal | None) -> Decimal | None:
    """|a - b| as a fraction of b (None when either is missing)."""
    a, b = _pos(a), _pos(b)
    if a is None or b is None:
        return None
    return abs(a - b) / b

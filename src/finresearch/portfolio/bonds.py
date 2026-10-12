"""Held bonds and Sovereign Gold Bonds (#294): their terms, next coupon, maturity, an SGB's early-redemption windows,
the value of a bond past its maturity, and one alert per coupon, maturity and SGB window.

A bond held like a share (an NCD, a G-sec, an SDL, a T-bill or an SGB in a demat account) is priced like one while it
trades. Past its maturity there is nothing left to trade: the issuer owes the redemption amount, and the holding stays
open until the user records the `redemption` (lots.py). Until then the holding says "matured — record redemption" and
is valued at its redemption value only when that is known (an entered redemption price, else the face value of a
non-SGB bond); otherwise its value is unknown. The last traded or statement price is never carried past maturity.

Terms come from, in order: what the user entered on the holding (`meta.bond_terms`), the bond library (Company
"bond-<isin>" meta and the lifecycle state, #293: the maturity only), and the instrument's own rules:
- SGB (RBI SGB FAQ, https://www.rbi.org.in/commonman/English/Scripts/FAQs.aspx?Id=1658, read 12-Oct-2026): tenor 8
  years from the issue date; interest "2.50 per cent (fixed rate) per annum on the amount of initial investment",
  credited semi-annually, the last with the principal (the 2015-16 tranches paid 2.75 %, so the rate is never
  assumed); "early encashment/redemption of the bond is allowed after fifth year from the date of issue on coupon
  payment dates"; "TDS is not applicable on the bond". The redemption price is set by RBI from the gold price, so it
  is unknown until announced and entered.
- Government securities: dated G-secs and SDLs pay coupons half-yearly and are quoted per ₹100 face value; T-bills
  pay no coupon and are redeemed at ₹100 face [S: RBI's G-sec market FAQ, not re-read for this change].
- Other bonds and NCDs: maturity from the library; coupon, frequency and face value only as entered (NSE's bond list
  does not give the frequency, and a partly redeemed bond's face differs: adapters.nse_bonds).

Price basis: NSE's capital-market bond prices include accrued interest (dirty), the app's convention for listed NCDs
(api.markets.bond_analytics). A held NCD priced from an NSE quote is labelled "dirty"; any other basis is not claimed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from finresearch.fincalc.tax import add_months
from finresearch.portfolio.tax import is_debt_security, sovereign_kind

SGB_TENOR_MONTHS = 96  # 8 years (RBI SGB FAQ)
SGB_EARLY_AFTER_MONTHS = 60  # early redemption after the fifth year, on coupon dates (RBI SGB FAQ)
SGB_FREQ = 2  # interest credited semi-annually (RBI SGB FAQ)
GSEC_FREQ = 2  # dated G-secs and SDLs: half-yearly coupons [S]
GSEC_FACE = Decimal(100)  # G-secs, SDLs and T-bills: ₹100 face value per unit [S]
COUPON_ALERT_DAYS = 7  # a coupon this close raises one alert
MATURITY_ALERT_DAYS = 30  # a maturity (or an SGB early-redemption window) this close raises one alert
TERM_KEYS = ("issue_date", "maturity", "coupon_pct", "freq", "face_value", "redemption_price")
MATURED = "matured — record redemption"


@dataclass(frozen=True)
class BondTerms:
    kind: str  # sgb | gsec | sdl | tbill | bond
    issue_date: date | None = None
    maturity: date | None = None
    coupon_pct: Decimal | None = None  # % a year
    freq: int | None = None  # coupons a year: 1, 2, 4 or 12; None = unknown (a T-bill has none)
    face_value: Decimal | None = None  # per unit
    redemption_price: Decimal | None = None  # per unit, when the redemption amount is known (entered)
    sources: dict[str, str] = field(default_factory=dict)


def bond_kind(asset_type: str, isin: str | None, name: str | None, symbol: str | None,
              tax_class: str | None = None) -> str | None:  # fmt: skip
    """The bond kind of a holding, or None for anything else (a fund is never one here)."""
    if asset_type != "stock":
        return None
    sov = sovereign_kind(isin, name, symbol)
    if sov is not None:
        return sov
    if tax_class == "sgb":
        return "sgb"
    return "bond" if is_debt_security(isin, name, symbol) else None


def _day(v: Any) -> date | None:
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10]) if v else None
    except ValueError:
        return None


def _dec(v: Any) -> Decimal | None:
    if v is None or v == "":
        return None
    try:
        d = Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def clean_terms(raw: dict[str, Any]) -> dict[str, Any]:
    """Validated `meta.bond_terms` from what the user entered (ISO dates, positive numbers, freq 1/2/4/12). Raises
    ValueError naming the bad field; an empty value removes that term."""
    out: dict[str, Any] = {}
    for k, v in (raw or {}).items():
        if k not in TERM_KEYS:
            raise ValueError(f"bond_terms: unknown term {k!r}")
        if v is None or v == "":
            continue
        if k in ("issue_date", "maturity"):
            d = _day(v)
            if d is None:
                raise ValueError(f"bond_terms.{k}: not a date")
            out[k] = d.isoformat()
        elif k == "freq":
            if int(v) not in (1, 2, 4, 12):
                raise ValueError("bond_terms.freq must be 1, 2, 4 or 12")
            out[k] = int(v)
        else:
            d = _dec(v)
            if d is None or d < 0 or (d == 0 and k != "coupon_pct"):
                raise ValueError(f"bond_terms.{k}: a positive number is needed")
            out[k] = str(d)
    if out.get("issue_date") and out.get("maturity") and out["maturity"] <= out["issue_date"]:
        raise ValueError("bond_terms: the maturity must be after the issue date")
    return out


def terms_of(kind: str, entered: dict[str, Any] | None, library_maturity: Any = None,
             library_source: str | None = None) -> BondTerms:  # fmt: skip
    """A holding's terms: entered values first, then the library's maturity, then the instrument's own rules."""
    e = entered or {}
    src: dict[str, str] = {k: "entered" for k in TERM_KEYS if e.get(k) not in (None, "")}
    issue, maturity = _day(e.get("issue_date")), _day(e.get("maturity"))
    coupon, freq = _dec(e.get("coupon_pct")), (int(e["freq"]) if e.get("freq") else None)
    face, red = _dec(e.get("face_value")), _dec(e.get("redemption_price"))
    if maturity is None and library_maturity:
        maturity = _day(library_maturity)
        if maturity is not None:
            src["maturity"] = library_source or "bond library"
    if kind == "sgb":
        if maturity is None and issue is not None:
            maturity, src["maturity"] = add_months(issue, SGB_TENOR_MONTHS), "issue date + 8 years (RBI SGB FAQ)"
        if freq is None:
            freq, src["freq"] = SGB_FREQ, "half-yearly (RBI SGB FAQ)"
    elif kind in ("gsec", "sdl"):
        if freq is None:
            freq, src["freq"] = GSEC_FREQ, "half-yearly (G-sec convention)"
        if face is None:
            face, src["face_value"] = GSEC_FACE, "₹100 per unit (G-sec convention)"
    elif kind == "tbill":
        freq = None  # a discount instrument: no coupon
        if face is None:
            face, src["face_value"] = GSEC_FACE, "₹100 per unit (T-bill convention)"
    return BondTerms(kind, issue, maturity, coupon, freq, face, red, src)


def matured(t: BondTerms, today: date) -> bool:
    """True only when the maturity is known and has come (lifecycle.matured's rule: unknown is never matured)."""
    return t.maturity is not None and t.maturity <= today


def coupon_schedule(t: BondTerms, after: date) -> list[date]:
    """Coupon dates on or after `after` up to the maturity; [] when the maturity or frequency is unknown, a T-bill,
    or matured. Dates step back from the maturity's day and month (fincalc.bonds), so an SGB's fall on its issue day."""
    from finresearch.fincalc.bonds import coupon_dates

    if t.maturity is None or t.freq is None or t.maturity < after:
        return []
    return coupon_dates(after - timedelta(days=1), t.maturity, t.freq)


def next_coupon(t: BondTerms, today: date) -> tuple[date | None, str | None]:
    """(the next coupon date, None) or (None, why it is unknown)."""
    if t.kind == "tbill":
        return None, "a T-bill pays no coupon (it is redeemed at face value)"
    if t.maturity is None:
        return None, "maturity unknown: enter the bond's terms"
    if matured(t, today):
        return None, f"matured on {t.maturity.isoformat()}"
    if t.freq is None:
        return None, "coupon frequency unknown: enter it from the offer document"
    days = coupon_schedule(t, today)
    return (days[0], None) if days else (None, "no coupon left before maturity")


def sgb_windows(t: BondTerms, today: date, n: int = 4) -> list[date]:
    """The next `n` early-redemption dates of an SGB: coupon dates from the fifth anniversary of issue, before the
    maturity (RBI SGB FAQ: "after fifth year from the date of issue on coupon payment dates") [S: whether the fifth
    anniversary itself is the first window is read from the FAQ's wording, not from a tranche's notification]."""
    if t.kind != "sgb" or t.issue_date is None or t.maturity is None:
        return []
    first = add_months(t.issue_date, SGB_EARLY_AFTER_MONTHS)
    return [d for d in coupon_schedule(t, max(today, first)) if d < t.maturity][:n]


def redemption_value(t: BondTerms) -> tuple[Decimal | None, str | None]:
    """(per-unit amount due at maturity, its source), or (None, None) when unknown: an entered redemption price, else
    the face value of a bond other than an SGB (an SGB's is RBI's gold-price average, never its face)."""
    if t.redemption_price is not None:
        return t.redemption_price, "entered redemption price"
    if t.kind != "sgb" and t.face_value is not None:
        return t.face_value, f"face value ({t.sources.get('face_value', 'entered')})"
    return None, None


def view(t: BondTerms, today: date, units: Decimal) -> dict[str, Any]:
    """What the holdings table shows for a bond."""
    nc, why = next_coupon(t, today)
    done = matured(t, today) and units > 0
    return {"kind": t.kind, "issue_date": _iso(t.issue_date), "maturity": _iso(t.maturity),
            "coupon_pct": _num(t.coupon_pct), "freq": t.freq, "face_value": _num(t.face_value),
            "redemption_price": _num(t.redemption_price), "next_coupon": _iso(nc), "next_coupon_reason": why,
            "matured": done, "status": MATURED if done else None,
            "redemption_windows": [d.isoformat() for d in sgb_windows(t, today)], "sources": dict(t.sources),
            "entered": {k: v for k, v in (("issue_date", t.issue_date), ("maturity", t.maturity)) if v}}  # fmt: skip


def _iso(d: date | None) -> str | None:
    return d.isoformat() if d else None


def _num(x: Decimal | None) -> float | None:
    return None if x is None else float(x)

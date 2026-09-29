"""Lot size, minimum bid and per-category application amounts for the IPO radar rows.

Primary source: NSE's per-issue issue information (`/api/ipo-detail`: "Bid Lot" + "Minimum Order Quantity" on
mainboard, "Lot Size" on SME). Cross-check and fallback: BSE's issue details (`GetMkt_ISSUE_BBS_IPO`: Market_Lot,
Minimum_Bid_Quantity), which cover mainboard issues too and name the same NSE symbol. Money is at the upper band.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from finresearch.adapters.bse import BseIssueDetail, detail_url
from finresearch.adapters.nse import IssueTerms, parse_price_band
from finresearch.fincalc.ipo import RETAIL_CAP, application_limits


def _num(x: Decimal | None) -> int | float | None:
    if x is None:
        return None
    return int(x) if x == x.to_integral_value() else float(x)


def _band(low: Decimal | None, high: Decimal | None) -> str | None:
    if low is None or high is None:
        return None
    lo, hi = (format(v.normalize(), "f") for v in (low, high))
    return f"Rs.{lo} to Rs.{hi}" if lo != hi else f"Rs.{hi}"


def bse_terms(d: BseIssueDetail, series: str) -> IssueTerms:
    """BSE issue details in the IssueTerms shape (the same company has the same lot on both exchanges)."""
    min_lots = d.min_lots or (1 if d.market_lot else None)
    return IssueTerms(symbol=d.symbol or str(d.ipo_no), series=series, lot_size=d.market_lot,
                      min_bid_shares=d.minimum_bid or d.market_lot, min_lots=min_lots,
                      min_lots_basis="BSE Minimum Bid Quantity" if d.min_lots else None,
                      price_range=d.price_band, price_low=d.price_low, price_high=d.price_high,
                      source="BSE issue details",
                      source_url=d.fetch.url if d.fetch else detail_url(d.ipo_no),
                      as_of=d.fetch.fetched_at if d.fetch else d.as_of)  # fmt: skip


def _check(primary: IssueTerms, other: IssueTerms | None) -> str | None:
    """Cross-check the chosen terms against the other exchange: None when there is nothing to compare."""
    if other is None or other.lot_size is None or primary is other:
        return None
    diffs = []
    if other.lot_size != primary.lot_size:
        diffs.append(f"lot {other.lot_size}")
    if (
        other.price_high is not None
        and primary.price_high is not None
        and other.price_high != primary.price_high
    ):
        diffs.append(f"upper band ₹{_num(other.price_high)}")
    who = other.source.split()[0]
    return f"{who} agrees" if not diffs else f"{who} differs: " + ", ".join(diffs)


def terms_fields(
    *,
    series: str | None,
    list_band: str | None,
    nse: IssueTerms | None = None,
    bse: BseIssueDetail | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """Additive radar fields: lot, minimum bid, filled price band and the per-category application amounts."""
    sme = (series or "").upper() == "SME"
    other = bse_terms(bse, "SME" if sme else "EQ") if bse is not None and bse.market_lot else None
    primary = nse if nse is not None and nse.lot_size else other
    out: dict[str, Any] = {"lot_size": None, "min_lots": None, "min_bid_shares": None, "application": None,
                           "lot_source": None, "lot_note": note, "price_band_note": None}  # fmt: skip
    list_low, list_high = parse_price_band(list_band)
    if primary is None:
        if note is None:
            out["lot_note"] = "The exchanges have not published the lot yet"
        return out
    low, high = primary.price_low, primary.price_high
    if high is None:  # issue page without a band: fall back to the list's band
        low, high = list_low, list_high
    elif list_high is not None and (list_low, list_high) != (low, high):
        page = "NSE's issue page says" if primary is nse else "BSE's issue details say"
        out["price_band"], out["price_band_list"] = _band(low, high), list_band
        out["price_band_note"] = f"The issue list shows {list_band}; {page} {_band(low, high)}, used here"
    if not list_band and high is not None:
        out["price_band"] = _band(low, high)
    out.update(lot_size=primary.lot_size, min_lots=primary.min_lots, min_bid_shares=primary.min_bid_shares)
    out["lot_source"] = {"label": primary.source, "url": primary.source_url,
                         "as_of": primary.as_of.isoformat() if primary.as_of else None,
                         "min_lots_basis": primary.min_lots_basis,
                         "check": _check(primary, other if primary is nse else nse)}  # fmt: skip
    if high is None or not primary.lot_size:
        return out
    lim = application_limits(primary.lot_size, high, min_lots=primary.min_lots or 1, sme=sme,
                             retail_cap=primary.retail_cap or RETAIL_CAP)  # fmt: skip
    out["application"] = {
        "price": _num(lim.price), "lot_cost": _num(lim.lot_cost), "min_investment": _num(lim.min_investment),
        "retail_max_lots": lim.retail_max_lots, "retail_max_amount": _num(lim.retail_max_amount),
        "shni_min_lots": lim.shni_min_lots, "shni_min_amount": _num(lim.shni_min_amount),
        "bhni_min_lots": lim.bhni_min_lots, "bhni_min_amount": _num(lim.bhni_min_amount),
        "retail_cap": _num(primary.retail_cap or RETAIL_CAP), "basis": "upper price band",
    }  # fmt: skip
    return out

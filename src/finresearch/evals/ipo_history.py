"""IPO history harvester: the training set behind the IPO base rates and listing model (docs/dev/RESEARCH_ROADMAP.md
§D.1 and §E item 2).

For every NSE `public-past-issues` row of a board (mainboard EQ by default; SME is harvested separately with
`--series SME` and always flagged by its `series`), it reads:

- `/api/ipo-detail` → the FINAL category book (`activeCat`: the COMBINED NSE+BSE book, ex-anchor, shares offered on
  the LOWER price band; times are recomputed here as bid / offered), the issue information (price range, lot,
  issue-size text → fresh issue and OFS where NSE states them) and `metaInfo.listingDate`;
- `NseEquity.history` → the listing-day bar (open, high, low, close, VWAP; NSE sets the listing day's previous close
  to the issue price, which cross-checks the issue price);
- NSE's Nifty 50 index history → the 20-session Nifty return up to the issue close (knowable at the retail decision)
  and up to the day before listing; the count of same-board listings in the 90 days before the close.

Politeness: one NSE request every `delay` seconds (default 2 s) through the app's PoliteClient, retried and
re-warmed by the adapters. Closed issues never change, so their detail and price payloads are disk-cached for weeks:
a re-run (or a resumed run) costs no network for rows already fetched. Rows already `complete` are skipped unless
`refresh=True`. The raw inputs are kept in `raw`, so `reparse()` re-derives every column offline.

Everything the model needs is stored in `ipo_history`; `export_jsonl`/`import_jsonl` move the dataset between
databases (the test database is wiped by pytest).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from finresearch.adapters.http import IST, PoliteClient
from finresearch.adapters.nse import NseClient, PastIssue, parse_num, parse_price_band

REFORM_DATE = date(
    2022, 4, 4
)  # SEBI circular 13-Jan-2022: NII allotment reform for issues opening on/after this
DETAIL_TTL_S = 45 * 86400  # a closed issue's book and issue page do not change
HISTORY_TTL_S = 45 * 86400
NIFTY_CHUNK_DAYS = 90  # NSE's index-history API returns at most ~70 sessions per call
NIFTY_SESSIONS = 20
IPO_WINDOW_DAYS = 90
MAX_LISTING_GAP_DAYS = 30  # T+6 before Dec-2023, T+3 since; a later NSE listing is not the IPO listing
HARVEST_RATE = 0.5  # NSE requests per second for the harvester (one every 2 s)
SCOPE = "nse_combined"  # activeCat: NSE+BSE, ex-anchor, lower-band shares offered
CR = Decimal(10_000_000)

# activeCat top-level rows. Old pages say "Non Institutional Investors", new ones add 2.1 (bNII) and 2.2 (sNII).
_CATS = (
    ("qib", ("qualified institutional",)),
    ("nii", ("non institutional", "non-institutional")),
    ("retail", ("retail individual", "individual investors")),
    ("employee", ("employee",)),
)


# --------------------------------------------------------------------------- parsing (pure, offline)


def _times(bid: Decimal | None, offered: Decimal | None) -> Decimal | None:
    if bid is None or not offered:
        return None
    return (bid / offered).quantize(Decimal("0.0001"))


def book_from_rows(
    rows: Iterable[tuple[str, str, Decimal | None, Decimal | None, Decimal | None]],
) -> dict[str, Any]:
    """Category book from normalised rows (code, name, shares offered, shares bid, NSE's printed times).
    Times = shares bid / shares offered; NSE's rounded printed figure is only a fallback. Returns {qib_times,
    nii_times, bnii_times, snii_times, retail_times, employee_times, total_times, public_shares}."""
    out: dict[str, Any] = {
        f"{k}_times": None for k in ("qib", "nii", "bnii", "snii", "retail", "employee", "total")
    }
    out["public_shares"] = None
    for code, name, offered, bid, printed in rows:
        code, name = (code or "").strip(), re.sub(r"\s+", " ", name or "").strip().lower()
        if code == "Sr.No." or name == "category":
            continue
        t = _times(bid, offered)
        if t is None and offered:
            t = printed
        if not code and name == "total":
            out["total_times"], out["public_shares"] = t, offered
            continue
        if code == "2.1":
            out["bnii_times"] = t
        elif code == "2.2":
            out["snii_times"] = t
        if not code.isdigit():
            continue
        for key, needles in _CATS:
            if any(n in name for n in needles) and out[f"{key}_times"] is None:
                out[f"{key}_times"] = t
                break
    return out


def parse_book(active: dict[str, Any] | None) -> dict[str, Any]:
    """Final category book from NSE's `activeCat` (combined NSE+BSE), plus NSE's "Updated as on" text."""
    rows = (active or {}).get("dataList") or []
    out = book_from_rows((str(r.get("srNo") or ""), str(r.get("category") or ""), parse_num(r.get("noOfShareOffered")),
                          parse_num(r.get("noOfSharesBid")), parse_num(r.get("noOfTotalMeant"))) for r in rows)  # fmt: skip
    out["updated"] = (str(active.get("updateTime") or "").strip() or None) if rows and active else None
    # some 2020-21 pages store numbers cut at the first comma ('"2' for "2,45,67,890"): unusable, not zero
    out["malformed"] = any(str(r.get(k) or "").startswith('"') for r in rows
                           for k in ("noOfShareOffered", "noOfSharesBid"))  # fmt: skip
    return out


def book_from_snapshot(categories: list[dict[str, Any]]) -> dict[str, Any]:
    """The same book from a stored `subscription_snapshot.categories` list (CategorySubscription dumps)."""
    return book_from_rows((str(c.get("code") or ""), str(c.get("name") or ""), _dec(c.get("shares_offered")),
                           _dec(c.get("shares_bid")), _dec(c.get("times"))) for c in categories)  # fmt: skip


_AMOUNT = re.compile(
    r"(?:rs\.?|inr|₹)\s*([\d,]+(?:\.\d+)?)\s*(million|mn|crores?|cr\b|lakhs?|billion|bn)?", re.IGNORECASE
)
_SHARES = re.compile(r"([\d,]{5,})\s*(?:equity\s+)?shares", re.IGNORECASE)
_UNIT = {"million": Decimal("0.1"), "mn": Decimal("0.1"), "crore": Decimal(1), "crores": Decimal(1),
         "cr": Decimal(1), "lakh": Decimal("0.01"), "lakhs": Decimal("0.01"), "billion": Decimal(100),
         "bn": Decimal(100)}  # fmt: skip
_SALE = re.compile(r"offer\s+for\s+sale|\bofs\b|offer\s+of\s+(?:up\s*to\s+)?[\d,]*\s*(?:equity\s+)?shares?|"
                   r"offer\s+of\s+share|selling\s+shareholder", re.IGNORECASE)  # fmt: skip
_FRESH = re.compile(r"fresh\s+issue|fresh\s+issuance", re.IGNORECASE)


def _to_cr(num: str, unit: str | None) -> Decimal | None:
    try:
        value = Decimal(num.replace(",", ""))
    except InvalidOperation:
        return None
    if unit is None:
        return None  # a bare rupee figure is ambiguous (per share?); ignore it
    return value * _UNIT[unit.lower()]


def parse_issue_size(text: str | None, issue_price: Decimal | None) -> dict[str, Decimal | None]:
    """NSE's free-text "Issue Size" → {issue_size_cr, fresh_cr, ofs_cr, ofs_share} (₹ crore). Best effort:

    - "Fresh issue aggregating up to Rs. 3,200 million and Offer for Sale aggregating up to Rs. 2,320 million"
      → fresh 320, OFS 232, total 552, OFS share 0.42;
    - "Rs 3,000 million and an offer of share upto 24,107,440 Equity Shares" → the OFS in shares × issue price;
    - "[.] Equity Shares aggregating upto Rs 5,000 Million (including anchor portion of …)" → total only; the split
      is unknown, so the OFS share is left empty (never assumed zero).
    Anchor-portion share counts in brackets are ignored."""
    out: dict[str, Decimal | None] = {
        "issue_size_cr": None,
        "fresh_cr": None,
        "ofs_cr": None,
        "ofs_share": None,
    }
    if not text:
        return out
    clean = re.sub(r"\([^)]*anchor[^)]*\)", " ", text, flags=re.IGNORECASE)
    clean = re.sub(r"\s+", " ", clean.replace('"', " "))
    has_sale, has_fresh = bool(_SALE.search(clean)), bool(_FRESH.search(clean))
    # split into clauses at " and " / ";" so each amount can be matched to the nearest label before it
    parts = re.split(r"\band\b|&|;|\bcomprising\b", clean, flags=re.IGNORECASE)
    fresh = ofs = total = None
    for part in parts:
        amount = next((_to_cr(m.group(1), m.group(2)) for m in _AMOUNT.finditer(part) if m.group(2)), None)
        shares = None
        if amount is None:
            m = _SHARES.search(part)
            if m and issue_price:
                shares = Decimal(m.group(1).replace(",", ""))
                amount = shares * issue_price / CR
        if amount is None:
            continue
        if _SALE.search(part):
            ofs = (ofs or 0) + amount
        elif _FRESH.search(part) or (has_sale and fresh is None and shares is None):
            fresh = (
                fresh or 0
            ) + amount  # "Rs 3,000 million and an offer of … shares": the unlabeled rupee part
        elif total is None:
            total = amount
    if fresh is not None and ofs is not None:
        out.update(fresh_cr=fresh, ofs_cr=ofs, issue_size_cr=fresh + ofs)
    elif fresh is not None and not has_sale:
        out.update(fresh_cr=fresh, ofs_cr=Decimal(0), issue_size_cr=fresh)
    elif ofs is not None and not has_fresh and total is None:
        out.update(fresh_cr=Decimal(0), ofs_cr=ofs, issue_size_cr=ofs)
    else:
        out["issue_size_cr"] = total or fresh or ofs
    if out["issue_size_cr"] and out["ofs_cr"] is not None:
        out["ofs_share"] = (out["ofs_cr"] / out["issue_size_cr"]).quantize(Decimal("0.0001"))
    for k in ("issue_size_cr", "fresh_cr", "ofs_cr"):
        if out[k] is not None:
            out[k] = Decimal(out[k]).quantize(Decimal("0.01"))
    return out


def _info(info: dict[str, str], *keys: str) -> str | None:
    norm = {k.strip().lower(): v for k, v in info.items()}
    return next((norm[k.lower()] for k in keys if k.lower() in norm), None)


def issue_info_subset(data: dict[str, Any]) -> dict[str, str]:
    """The issue-information rows the harvester uses (titles are stripped: old pages say "Issue Size ")."""
    keep = ("issue size", "price range", "issue price", "bid lot", "market lot", "lot size", "minimum order quantity",
            "issue period", "issue type")  # fmt: skip
    out: dict[str, str] = {}
    for item in (data.get("issueInfo") or {}).get("dataList") or []:
        title = re.sub(r"\s+", " ", str(item.get("title") or "")).strip()
        if title.lower() in keep:
            out[title] = re.sub(r"\s+", " ", str(item.get("value") or "")).strip()
    return out


def nifty_return(closes: list[tuple[date, Decimal]], anchor: date, *, inclusive: bool,
                 sessions: int = NIFTY_SESSIONS) -> Decimal | None:  # fmt: skip
    """Nifty 50 return over the `sessions` trading days ending at the last close on/before `anchor` (inclusive) or
    strictly before it: close[t] / close[t - sessions] - 1. `closes` sorted ascending."""
    idx = [i for i, (d, _) in enumerate(closes) if (d <= anchor if inclusive else d < anchor)]
    if not idx or idx[-1] - sessions < 0:
        return None
    end = idx[-1]
    if (anchor - closes[end][0]).days > 7:  # a gap in the index data: do not reach back to a stale close
        return None
    return (closes[end][1] / closes[end - sessions][1] - 1).quantize(Decimal("0.000001"))


def ipo_count(listing_dates: Iterable[date], anchor: date, days: int = IPO_WINDOW_DAYS) -> int:
    """Listings in the `days` days before `anchor` (anchor excluded): a proxy for how hot the IPO market is."""
    lo = anchor - timedelta(days=days)
    return sum(1 for d in listing_dates if lo <= d < anchor)


def listing_bar(bars: list[dict[str, Any]], listing: date | None) -> dict[str, Any] | None:
    """The first price bar on or after the listing date (the listing may slip a day)."""
    rows = sorted((b for b in bars if b.get("day")), key=lambda b: b["day"])
    if listing is not None:
        rows = [b for b in rows if date.fromisoformat(b["day"]) >= listing]
    return rows[0] if rows else None


def _dec(v: Any) -> Decimal | None:
    return None if v is None else Decimal(str(v))


@dataclass
class Inputs:
    """What one issue's row is derived from (all of it kept in `raw`)."""

    symbol: str
    series: str
    company: str | None
    ipo_start: date | None
    ipo_end: date | None
    listing_date: date | None
    list_issue_price: Decimal | None  # from public-past-issues
    price_range: str | None
    active: dict[str, Any] | None
    info: dict[str, str]
    meta_listing: str | None
    bars: list[dict[str, Any]]


def derive(x: Inputs, nifty: list[tuple[date, Decimal]], listings: list[date]) -> dict[str, Any]:
    """Every `ipo_history` column from the raw inputs (pure; `reparse` re-runs it offline)."""
    missing: list[str] = []
    book = parse_book(x.active)
    if book.get("malformed"):
        for k in ("qib", "nii", "bnii", "snii", "retail", "employee", "total"):
            book[f"{k}_times"] = None
        book["public_shares"] = None
        missing.append("subscription: NSE's stored book is malformed (numbers truncated at a comma)")
    elif book["qib_times"] is None:
        missing.append("subscription: NSE's issue page has no final category book (activeCat empty)")
    listing = x.listing_date
    if x.meta_listing:
        with contextlib.suppress(ValueError):
            listing = date.fromisoformat(x.meta_listing[:10])
    bar = listing_bar(x.bars, listing)
    late = listing - x.ipo_end if listing and x.ipo_end else None
    if bar is not None and late is not None and late.days > MAX_LISTING_GAP_DAYS:
        # e.g. CAMS and Protean listed on BSE first and moved to NSE months later: NSE's first bar is not the IPO
        missing.append(f"listing bar: NSE listing came {late.days} days after the issue closed (listed elsewhere "
                       "first), so NSE's first bar is not the IPO listing")  # fmt: skip
        bar = None
    low, high = parse_price_band(_info(x.info, "Price Range") or x.price_range)
    issue = x.list_issue_price
    prev = _dec(bar.get("prev_close")) if bar else None
    if issue is None and prev is not None:
        issue = prev  # NSE sets the listing day's previous close to the issue price
    if issue is None and high is not None and low == high:
        issue = high
    if issue is None:
        missing.append("issue price: not in public-past-issues nor the listing bar")
    if bar is None and not any(m.startswith("listing bar") for m in missing):
        missing.append("listing bar: NSE returned no price history for the listing week")
    op, cl = (_dec(bar.get("open")), _dec(bar.get("close"))) if bar else (None, None)
    if bar is not None and op is None:
        missing.append("listing open: empty in NSE's bar")
    ret_open = (op / issue - 1).quantize(Decimal("0.000001")) if op and issue else None
    ret_close = (cl / issue - 1).quantize(Decimal("0.000001")) if cl and issue else None
    size = parse_issue_size(_info(x.info, "Issue Size"), issue)
    lot = None
    lot_text = _info(x.info, "Bid Lot", "Market Lot", "Lot Size")
    if lot_text and (m := re.search(r"\d[\d,]*", lot_text)):
        lot = int(m.group().replace(",", ""))
    public_cr = (book["public_shares"] * issue / CR).quantize(Decimal("0.01")) if book["public_shares"] and issue \
        else None  # fmt: skip
    anchor = x.ipo_end or (listing - timedelta(days=3) if listing else None)
    row = {
        "company": x.company,
        "ipo_start": x.ipo_start,
        "ipo_end": x.ipo_end,
        "listing_date": listing,
        "issue_price": issue,
        "price_low": low,
        "price_high": high,
        "lot_size": lot,
        "issue_size_cr": size["issue_size_cr"],
        "public_book_cr": public_cr,
        "fresh_cr": size["fresh_cr"],
        "ofs_cr": size["ofs_cr"],
        "ofs_share": size["ofs_share"],
        **{k: v for k, v in book.items() if k.endswith("_times")},
        "subscription_scope": SCOPE
        if book["qib_times"] is not None or book["total_times"] is not None
        else None,
        "subscription_updated": (book["updated"] or "")[:80] or None,
        "list_open": op,
        "list_high": _dec(bar.get("high")) if bar else None,
        "list_low": _dec(bar.get("low")) if bar else None,
        "list_close": cl,
        "list_vwap": _dec(bar.get("vwap")) if bar else None,
        "list_prev_close": prev,
        "return_open": ret_open,
        "return_close": ret_close,
        "nifty_ret20_close": nifty_return(nifty, anchor, inclusive=True) if anchor and nifty else None,
        "nifty_ret20_listing": nifty_return(nifty, listing, inclusive=False) if listing and nifty else None,
        "ipo_count_90d": ipo_count(listings, anchor) if anchor else None,
        "post_2022": (x.ipo_start or anchor) >= REFORM_DATE if (x.ipo_start or anchor) else None,
    }
    if prev is not None and x.list_issue_price and abs(prev / x.list_issue_price - 1) > Decimal("0.01"):
        missing.append(f"check: listing-day previous close {prev} differs from the issue price {x.list_issue_price} "
                       "(price history may be adjusted for a later split or bonus)")  # fmt: skip
    if size["issue_size_cr"] is None:
        missing.append("issue size: NSE's issue-size text had no parseable amount")
    if size["ofs_share"] is None:
        missing.append("OFS share: the issue-size text does not split fresh issue and OFS")
    if not nifty or row["nifty_ret20_close"] is None:
        missing.append("nifty: no 20-session Nifty 50 return for the issue close")
    core = ret_open is not None and book["qib_times"] is not None
    row["status"] = "complete" if core else "partial"
    row["missing"] = missing
    return row


# --------------------------------------------------------------------------- network


def _bar_json(b) -> dict[str, Any]:
    return {"day": b.day.isoformat(), **{k: (str(getattr(b, k)) if getattr(b, k) is not None else None)
                                         for k in ("open", "high", "low", "close", "prev_close", "vwap", "volume")}}  # fmt: skip


async def fetch_nifty(nse: NseClient, start: date, end: date, *, today: date | None = None,
                      log: Callable[[str], None] = print) -> list[tuple[date, Decimal]]:  # fmt: skip
    """Nifty 50 closes from NSE's index-history API in 90-day chunks (cached; the current chunk for 12 hours)."""
    today = today or datetime.now(IST).date()
    out: dict[date, Decimal] = {}
    lo = start
    while lo <= end:
        hi = min(end, lo + timedelta(days=NIFTY_CHUNK_DAYS - 1))
        ttl = 12 * 3600 if hi >= today - timedelta(days=3) else HISTORY_TTL_S
        try:
            data, _ = await nse.get_json("/api/historicalOR/indicesHistory",
                                         {"indexType": "NIFTY 50", "from": lo.strftime("%d-%m-%Y"),
                                          "to": hi.strftime("%d-%m-%Y")}, cache_ttl=ttl)  # fmt: skip
            for r in (data or {}).get("data") or []:
                d = datetime.strptime(str(r.get("EOD_TIMESTAMP")), "%d-%b-%Y").date()
                c = parse_num(r.get("EOD_CLOSE_INDEX_VAL"))
                if c is not None:
                    out[d] = c
        except Exception as e:  # a missing chunk leaves gaps; nifty_return refuses to bridge them
            log(f"nifty {lo}..{hi}: {type(e).__name__}: {e}")
        lo = hi + timedelta(days=1)
    return sorted(out.items())


async def fetch_inputs(
    nse: NseClient, p: PastIssue, series: str, *, eq=None
) -> tuple[Inputs, dict[str, Any]]:
    """ipo-detail (cached) + the listing week's bars (cached). Errors in the price call are kept as a missing field."""
    from finresearch.adapters.nse_equity import NseEquity

    source: dict[str, Any] = {}
    data, resp = await nse.get_json("/api/ipo-detail", {"symbol": p.symbol, "series": series},
                                    cache_ttl=DETAIL_TTL_S)  # fmt: skip
    data = data if isinstance(data, dict) else {}
    source["ipo_detail"] = {"url": resp.record.url, "fetched_at": resp.record.fetched_at.isoformat(),
                            "sha256": resp.record.sha256}  # fmt: skip
    meta = data.get("metaInfo") or {}
    meta_listing = str(meta.get("listingDate") or "") or None
    listing = p.listing_date
    if meta_listing:
        with contextlib.suppress(ValueError):
            listing = date.fromisoformat(meta_listing[:10])
    bars: list[dict[str, Any]] = []
    if listing:
        eq = eq or NseEquity(nse)
        wanted = ["EQ", "BE"] if series == "EQ" else ["SM", "ST", "EQ"]
        active = [str(s).strip() for s in meta.get("activeSeries") or [] if str(s).strip()]
        tried: list[str] = []
        for ser in dict.fromkeys([*[s for s in active if s in wanted], *wanted]):
            tried.append(ser)
            try:
                got = await eq.history(
                    p.symbol, listing, listing + timedelta(days=7), ser, cache_ttl=HISTORY_TTL_S
                )
            except Exception as e:
                source.setdefault("history_errors", []).append(f"{ser}: {type(e).__name__}: {e}"[:200])
                continue
            if got:
                bars = [_bar_json(b) for b in got]
                source["history"] = {"series": ser, "from": listing.isoformat()}
                break
        source["history_series_tried"] = tried
    x = Inputs(symbol=p.symbol, series=series, company=p.company, ipo_start=p.ipo_start, ipo_end=p.ipo_end,
               listing_date=p.listing_date, list_issue_price=p.issue_price, price_range=p.price_range,
               active=data.get("activeCat"), info=issue_info_subset(data), meta_listing=meta_listing, bars=bars)  # fmt: skip
    return x, source


def _raw(x: Inputs) -> dict[str, Any]:
    return {"active": x.active, "info": x.info, "meta_listing": x.meta_listing, "bars": x.bars,
            "past": {"company": x.company, "ipo_start": _iso(x.ipo_start), "ipo_end": _iso(x.ipo_end),
                     "listing_date": _iso(x.listing_date), "issue_price": _s(x.list_issue_price),
                     "price_range": x.price_range}}  # fmt: skip


def _inputs_from_raw(symbol: str, series: str, raw: dict[str, Any]) -> Inputs:
    past = raw.get("past") or {}

    def d(v):
        return date.fromisoformat(v) if v else None

    return Inputs(symbol=symbol, series=series, company=past.get("company"), ipo_start=d(past.get("ipo_start")),
                  ipo_end=d(past.get("ipo_end")), listing_date=d(past.get("listing_date")),
                  list_issue_price=_dec(past.get("issue_price")), price_range=past.get("price_range"),
                  active=raw.get("active"), info=raw.get("info") or {}, meta_listing=raw.get("meta_listing"),
                  bars=raw.get("bars") or [])  # fmt: skip


def _iso(d: date | None) -> str | None:
    return d.isoformat() if d else None


def _s(v: Decimal | None) -> str | None:
    return None if v is None else str(v)


def eligible(rows: list[PastIssue], series: str, today: date) -> list[PastIssue]:
    """Past IPOs of one board that have listed. NSE's list also carries debt, rights, InvIT and pending rows, and
    further public offers (FPOs) whose "listing date" is the company's original listing, years before the offer."""
    return [r for r in rows if (r.security_type or "").upper() == series and r.symbol and r.listing_date
            and r.listing_date <= today and (r.ipo_start is None or r.listing_date >= r.ipo_start)]  # fmt: skip


def _upsert(session: Session, symbol: str, series: str, row: dict[str, Any], source: dict[str, Any],
            raw: dict[str, Any]) -> None:  # fmt: skip
    from finresearch.db.models import IpoHistory

    values = {
        "symbol": symbol,
        "series": series,
        **row,
        "source": source,
        "raw": raw,
        "updated_at": datetime.now(IST),
    }
    stmt = insert(IpoHistory).values(**values)
    session.execute(stmt.on_conflict_do_update(index_elements=["symbol", "series", "ipo_start"],
                                               set_={k: stmt.excluded[k] for k in values if k not in
                                                     ("symbol", "series", "ipo_start")}))  # fmt: skip


async def harvest(*, series: str = "EQ", limit: int | None = None, refresh: bool = False, delay: float = 2.0,
                  client: NseClient | None = None, session_factory=None, today: date | None = None,
                  log: Callable[[str], None] = print) -> dict[str, Any]:  # fmt: skip
    """Fill `ipo_history` for one board. Resumable and idempotent (upsert by symbol, series and issue start)."""
    from finresearch.adapters.nse_equity import NseEquity
    from finresearch.db import session_scope
    from finresearch.db.models import IpoHistory

    series = series.upper()
    factory = session_factory or session_scope
    today = today or datetime.now(IST).date()
    own = client is None
    nse = client or NseClient(PoliteClient(host_rates={"nseindia.com": 1 / max(delay, 0.01)}))
    stats: Counter[str] = Counter()
    try:
        past = eligible(await nse.past_issues(), series, today)
        past.sort(key=lambda r: r.listing_date or date.min)
        listings = [r.listing_date for r in past if r.listing_date]
        with factory() as s:
            done = {(r.symbol, r.ipo_start) for r in s.scalars(select(IpoHistory).where(
                IpoHistory.series == series, IpoHistory.status == "complete"))}  # fmt: skip
        todo = [r for r in past if refresh or (r.symbol, r.ipo_start) not in done]
        if limit is not None:
            todo = todo[:limit]
        log(f"{series}: {len(past)} listed issues on NSE, {len(done)} already complete, {len(todo)} to fetch")
        first = min((r.ipo_start or r.listing_date for r in past), default=today) - timedelta(days=60)
        nifty = await fetch_nifty(nse, first, today, today=today, log=log)
        log(f"nifty: {len(nifty)} closes {nifty[0][0] if nifty else '-'}..{nifty[-1][0] if nifty else '-'}")
        eq = NseEquity(nse)
        for i, p in enumerate(todo, 1):
            try:
                x, source = await fetch_inputs(nse, p, series, eq=eq)
            except Exception as e:
                stats["error"] += 1
                log(f"[{i}/{len(todo)}] {p.symbol}: error {type(e).__name__}: {e}"[:300])
                with factory() as s:
                    x = Inputs(p.symbol, series, p.company, p.ipo_start, p.ipo_end, p.listing_date, p.issue_price,
                               p.price_range, None, {}, None, [])  # fmt: skip
                    row = derive(x, nifty, listings)
                    row.update(status="error", missing=[f"fetch failed: {type(e).__name__}: {e}"[:300]])
                    _upsert(s, p.symbol, series, row, {}, _raw(x))
                continue
            row = derive(x, nifty, listings)
            stats[row["status"]] += 1
            with factory() as s:
                _upsert(s, p.symbol, series, row, source, _raw(x))
            if i % 25 == 0 or i == len(todo):
                log(f"[{i}/{len(todo)}] {p.symbol} {row['status']} ({dict(stats)})")
            await asyncio.sleep(0)  # the PoliteClient spaces requests; nothing else to wait for
    finally:
        if own:
            await nse.aclose()
    return {"series": series, "fetched": sum(stats.values()), **stats}


def reparse(session: Session, nifty: list[tuple[date, Decimal]] | None = None) -> int:
    """Re-derive every row from its stored raw inputs (after a parser fix). Nifty features are kept unless a
    fresh `nifty` series is passed."""
    from finresearch.db.models import IpoHistory

    rows = session.scalars(select(IpoHistory)).all()
    by_series: dict[str, list[date]] = {}
    for r in rows:
        if r.listing_date:
            by_series.setdefault(r.series, []).append(r.listing_date)
    n = 0
    for r in rows:
        x = _inputs_from_raw(r.symbol, r.series, r.raw or {})
        new = derive(x, nifty or [], by_series.get(r.series, []))
        if nifty is None:
            for k in ("nifty_ret20_close", "nifty_ret20_listing"):
                new[k] = getattr(r, k)
            new["missing"] = [m for m in new["missing"] if not m.startswith("nifty")] + \
                [m for m in (r.missing or []) if str(m).startswith("nifty")]  # fmt: skip
        if r.status == "error" and not x.active and not x.bars:
            continue
        for k, v in new.items():
            setattr(r, k, v)
        n += 1
    return n


# --------------------------------------------------------------------------- dataset I/O and coverage

EXPORT_FIELDS = ("symbol", "series", "company", "ipo_start", "ipo_end", "listing_date", "issue_price", "price_low",
                 "price_high", "lot_size", "issue_size_cr", "public_book_cr", "fresh_cr", "ofs_cr", "ofs_share",
                 "qib_times", "nii_times", "bnii_times", "snii_times", "retail_times", "employee_times",
                 "total_times", "subscription_scope", "subscription_updated", "list_open", "list_high", "list_low",
                 "list_close", "list_vwap", "list_prev_close", "return_open", "return_close", "nifty_ret20_close",
                 "nifty_ret20_listing", "ipo_count_90d", "post_2022", "status", "missing", "source", "raw")  # fmt: skip
_DATES = {"ipo_start", "ipo_end", "listing_date"}


def _jsonable(v: Any) -> Any:
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, date | datetime):
        return v.isoformat()
    return v


def export_jsonl(session: Session, path: Path, *, with_raw: bool = True) -> int:
    from finresearch.db.models import IpoHistory

    path.parent.mkdir(parents=True, exist_ok=True)
    rows = session.scalars(select(IpoHistory).order_by(IpoHistory.series, IpoHistory.listing_date)).all()
    with path.open("w") as f:
        for r in rows:
            rec = {k: _jsonable(getattr(r, k)) for k in EXPORT_FIELDS if with_raw or k != "raw"}
            f.write(json.dumps(rec, separators=(",", ":")) + "\n")
    return len(rows)


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    out = []
    for line in path.read_text().splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def import_jsonl(session: Session, path: Path) -> int:
    from finresearch.db.models import IpoHistory

    n = 0
    for rec in load_jsonl(path):
        values: dict[str, Any] = {}
        for k in EXPORT_FIELDS:
            v = rec.get(k)
            col = IpoHistory.__table__.c[k]
            if v is not None and k in _DATES:
                v = date.fromisoformat(v)
            elif v is not None and col.type.python_type is Decimal:
                v = Decimal(v)
            values[k] = (
                v
                if v is not None or k not in ("missing", "source", "raw")
                else ([] if k == "missing" else {})
            )
        stmt = insert(IpoHistory).values(**values, updated_at=datetime.now(IST))
        session.execute(stmt.on_conflict_do_update(index_elements=["symbol", "series", "ipo_start"],
                                                   set_={k: stmt.excluded[k] for k in values
                                                         if k not in ("symbol", "series", "ipo_start")}))  # fmt: skip
        n += 1
    return n


def coverage(rows: list[Any]) -> dict[str, Any]:
    """Rows harvested / complete / missing by reason, per series and listing year (rows: ORM objects or dicts)."""

    def g(r, k):
        return r.get(k) if isinstance(r, dict) else getattr(r, k)

    out: dict[str, Any] = {}
    for series in sorted({g(r, "series") for r in rows}):
        rs = [r for r in rows if g(r, "series") == series]
        years: dict[str, Counter] = {}
        reasons: Counter[str] = Counter()
        for r in rs:
            ld = g(r, "listing_date")
            y = str(ld)[:4] if ld else "unknown"
            c = years.setdefault(y, Counter())
            c["rows"] += 1
            c[g(r, "status")] += 1
            c["with_book"] += g(r, "qib_times") is not None
            c["with_listing_open"] += g(r, "return_open") is not None
            for m in g(r, "missing") or []:
                reasons[str(m).split(":")[0]] += 1
        out[series] = {"rows": len(rs), "complete": sum(1 for r in rs if g(r, "status") == "complete"),
                       "with_book": sum(1 for r in rs if g(r, "qib_times") is not None),
                       "with_listing_open": sum(1 for r in rs if g(r, "return_open") is not None),
                       "with_ofs_share": sum(1 for r in rs if g(r, "ofs_share") is not None),
                       "with_nifty": sum(1 for r in rs if g(r, "nifty_ret20_close") is not None),
                       "missing_by_reason": dict(reasons.most_common()),
                       "by_year": {y: dict(c) for y, c in sorted(years.items())}}  # fmt: skip
    return out

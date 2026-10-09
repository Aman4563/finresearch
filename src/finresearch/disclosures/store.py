"""Storage for disclosure feeds: feed status (last good read, last failure) and de-duplicated history rows, plus the
set of instruments the user tracks (held stocks and bonds, stock watches, tracked bonds)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from finresearch.db.models import DisclosureFeed, DisclosureRecord

MARKET = "*"


def jsonable(v: Any) -> Any:
    if isinstance(v, BaseModel):
        return jsonable(v.model_dump())
    if isinstance(v, dict):
        return {str(k): jsonable(x) for k, x in v.items()}
    if isinstance(v, list | tuple | set):
        return [jsonable(x) for x in v]
    if isinstance(v, Decimal):
        return format(v.normalize(), "f") if v == v.to_integral_value() else str(v)
    if isinstance(v, datetime | date):
        return v.isoformat()
    return v


def dedupe_key(*parts: Any) -> str:
    return hashlib.sha256(json.dumps([jsonable(p) for p in parts], default=str).encode()).hexdigest()


# --------------------------------------------------------------------------- feed status
def feed(s: Session, dataset: str, key: str = MARKET) -> DisclosureFeed | None:
    return s.scalars(
        select(DisclosureFeed).where(DisclosureFeed.dataset == dataset, DisclosureFeed.key == key)
    ).first()


def _row(s: Session, dataset: str, key: str) -> DisclosureFeed:
    row = feed(s, dataset, key)
    if row is None:
        row = DisclosureFeed(dataset=dataset, key=key, payload={})
        s.add(row)
    return row


def record_ok(s: Session, dataset: str, key: str, *, payload: dict[str, Any], url: str | None, as_of: str | None,
              now: datetime) -> None:  # fmt: skip
    row = _row(s, dataset, key)
    row.payload, row.source_url, row.as_of, row.ok_at, row.checked_at = (
        jsonable(payload),
        url,
        as_of,
        now,
        now,
    )
    s.flush()


# --------------------------------------------------------------------------- ISIN -> exchange symbols
# NSE's equity list merged with BSE's scrip master by ISIN (adapters.bse_equity.merge_listings), kept as a market feed
# so synchronous code (tracked(), the pre-trade red flags) can find a holding's NSE symbol from its ISIN alone: broker
# holdings statements carry only the ISIN (#200). Written by the API whenever it loads the listings and by the daily
# portfolio pass; at most once per ISIN_MAP_FRESH.
ISIN_MAP = "isin_map"
ISIN_MAP_FRESH = timedelta(hours=20)


def record_isin_map(s: Session, listings: Any, now: datetime, *, force: bool = False) -> bool:
    """Store {ISIN: [NSE symbol | None, BSE code | None]} from a Listings; skipped while the stored map is fresh."""
    row = feed(s, ISIN_MAP)
    if not force and row is not None and row.ok_at is not None and now - row.ok_at < ISIN_MAP_FRESH:
        return False
    m = {r.isin.upper(): [r.nse_symbol, r.bse_code] for r in getattr(listings, "rows", []) or [] if r.isin}
    if not m:
        return False
    record_ok(s, ISIN_MAP, MARKET, payload={"map": m}, url=None, as_of=now.isoformat(), now=now)
    return True


def isin_map(s: Session) -> dict[str, list[str | None]]:
    """{ISIN: [NSE symbol | None, BSE code | None]} as last stored ({} before the first load)."""
    row = feed(s, ISIN_MAP)
    return dict((row.payload or {}).get("map") or {}) if row is not None else {}


def stock_key(
    isin: str | None, nse_symbol: str | None, bse_code: str | None, m: dict[str, list[str | None]]
) -> str | None:
    """A stock holding's exchange key ("<NSE symbol>" or "BSE:<code>"): the ISIN map decides when it knows the ISIN
    (a holding with only an ISIN gets its symbol; a BSE-only company stays BSE-only whatever symbol a broker wrote),
    else the holding's own symbol or code (#200)."""
    ent = m.get((isin or "").upper()) if isin else None
    if (
        ent and not ent[0] and nse_symbol and not bse_only_isin(isin)
    ):  # an ETF: NSE's equity list does not cover it
        ent = None
    sym, bse = (ent[0], bse_code or ent[1]) if ent else (nse_symbol, bse_code)
    return sym.upper() if sym else (f"BSE:{bse}" if bse else None)


def bse_only_isin(isin: str | None) -> bool:
    """Whether an ISIN missing from NSE's list means "not on NSE": true for company securities (INE...). NSE's equity
    list (EQUITY_L.csv) has no ETFs or other fund units (INF...), so an ETF's absence from it says nothing (#200:
    NIFTYBEES was quoted on BSE)."""
    return (isin or "").upper().startswith("INE")


def nse_symbol_for_isin(
    s: Session, isin: str | None, m: dict[str, list[str | None]] | None = None
) -> str | None:
    """The NSE symbol of an ISIN from the stored map; None when unknown or listed on BSE only."""
    hit = (isin_map(s) if m is None else m).get((isin or "").upper()) if isin else None
    return hit[0] if hit else None


def record_error(s: Session, dataset: str, key: str, error: str, now: datetime) -> None:
    """A failed read: the last good payload stays; the error and its time are kept for the "unavailable" note."""
    row = _row(s, dataset, key)
    row.error, row.error_at, row.checked_at = error[:500], now, now
    s.flush()


# --------------------------------------------------------------------------- history rows
def upsert_records(s: Session, dataset: str, rows: list[dict[str, Any]], *, update: bool = False) -> int:
    """Insert history rows ({symbol, isin, issuer, day, key: tuple, data, source_url}); returns how many were new.
    With `update`, an existing row's data is refreshed (a pledge quarter NSE revised)."""
    new = 0
    for r in rows:
        values = {"dataset": dataset, "symbol": r.get("symbol"), "isin": r.get("isin"), "issuer": r.get("issuer"),
                  "day": r.get("day"), "dedupe_key": dedupe_key(dataset, *r["key"]), "data": jsonable(r["data"]),
                  "source_url": r.get("source_url")}  # fmt: skip
        stmt = insert(DisclosureRecord).values(**values)
        if update:
            stmt = stmt.on_conflict_do_update(index_elements=["dedupe_key"], set_={"data": stmt.excluded.data,
                                              "source_url": stmt.excluded.source_url, "day": stmt.excluded.day})  # fmt: skip
            existed = s.scalar(
                select(DisclosureRecord.id).where(DisclosureRecord.dedupe_key == values["dedupe_key"])
            )
            s.execute(stmt)
            new += existed is None
        else:
            got = s.execute(
                stmt.on_conflict_do_nothing(index_elements=["dedupe_key"]).returning(DisclosureRecord.id)
            )
            new += got.first() is not None
    s.flush()
    return new


def records(s: Session, dataset: str, *, symbol: str | None = None, isin: str | None = None,
            issuer: str | None = None, since: date | None = None) -> list[DisclosureRecord]:  # fmt: skip
    q = select(DisclosureRecord).where(DisclosureRecord.dataset == dataset)
    if symbol is not None:
        q = q.where(DisclosureRecord.symbol == symbol)
    if isin is not None:
        q = q.where(DisclosureRecord.isin == isin)
    if issuer is not None:
        q = q.where(DisclosureRecord.issuer == issuer)
    if since is not None:
        q = q.where(DisclosureRecord.day >= since)
    return list(s.scalars(q.order_by(DisclosureRecord.day.desc().nulls_last(), DisclosureRecord.id.desc())))


# --------------------------------------------------------------------------- what the user tracks
@dataclass
class Tracked:
    """What the user owns or follows (#293: one answer for every fetch-targeting job): open holdings (stocks, funds by
    AMFI scheme code, bonds by ISIN) and watched instruments (stock watches, funds and bonds added to the library).
    Stocks by NSE symbol (BSE-only stocks are listed apart: NSE's feeds do not cover them); bonds by ISIN.

    Each entry carries `weight`: the holding's share of the portfolio in % from the monitor's last valuation (summed
    over accounts), None when not held or not valued yet (unknown, never 0). Instruments that have ended (a matured or
    delisted bond, a fund AMFI dropped, a delisted stock: monitor.lifecycle) are left out and listed in `ended`."""

    stocks: dict[str, dict[str, Any]] = field(default_factory=dict)  # symbol -> {isin, name, held, watched, weight}
    bse_only: dict[str, dict[str, Any]] = field(
        default_factory=dict
    )  # "BSE:<code>" -> {isin, name, held, watched, weight}
    bonds: dict[str, dict[str, Any]] = field(default_factory=dict)  # isin -> {name, held, tracked, weight}
    funds: dict[str, dict[str, Any]] = field(default_factory=dict)  # AMFI code -> {name, held, tracked, weight}
    ended: dict[str, dict[str, Any]] = field(default_factory=dict)  # lifecycle key -> why it ended

    def issuers(self) -> dict[str, list[str]]:
        """{ISIN issuer code: [instrument keys]} over every tracked instrument with an ISIN."""
        from finresearch.adapters.nse_disclosures import issuer_code

        out: dict[str, list[str]] = {}
        for key, v in [*self.stocks.items(), *self.bse_only.items()]:
            if (code := issuer_code(v.get("isin"))) is not None:
                out.setdefault(code, []).append(key)
        for isin in self.bonds:
            if (code := issuer_code(isin)) is not None:
                out.setdefault(code, []).append(isin)
        return out

    def stock_keys(self, *, nse_only: bool = False) -> list[str]:
        """Stock keys by priority: held by weight (largest first; an unvalued holding after the valued ones), then
        watched-only, each group alphabetical on ties. `nse_only` leaves out BSE-only stocks."""
        items = [*self.stocks.items(), *([] if nse_only else self.bse_only.items())]
        return [k for k, _ in sorted(items, key=lambda kv: (not kv[1]["held"], kv[1].get("weight") is None,
                                                             -(kv[1].get("weight") or 0), kv[0]))]  # fmt: skip


_DEBT_TYPES = (
    "07",
    "08",
)  # ISIN security-type code of debentures / bonds (see adapters.nse_disclosures.issuer_code)


def holding_weights(s: Session) -> dict[int, float]:
    """{holding id: weight %} from the monitor's last valuation (portfolio.cache); {} before the first one."""
    from finresearch.portfolio import cache

    v = cache.read(s, cache.VALUATION)
    return {int(h): float(x["weight_pct"]) for h, x in (v.get("holdings") or {}).items()
            if x.get("weight_pct") is not None}  # fmt: skip


def tracked(s: Session, today: date | None = None) -> Tracked:
    from finresearch.db.models import Company, PortfolioHolding, PortfolioLot, Watch
    from finresearch.fincalc.dates import today_ist
    from finresearch.monitor import lifecycle

    t = Tracked()
    gone = lifecycle.ended_keys(s, today or today_ist())
    weights = holding_weights(s)

    def weigh(v: dict[str, Any], w: float | None) -> None:
        if w is not None:
            v["weight"] = round((v.get("weight") or 0) + w, 4)

    def add(key: str, isin: str | None, name: str | None, how: str, w: float | None = None) -> None:
        if (why := gone.get(lifecycle.key("stock", key))) is not None:
            t.ended[lifecycle.key("stock", key)] = why
            return
        bucket = t.bse_only if key.startswith("BSE:") else t.stocks
        v = bucket.setdefault(key, {"isin": None, "name": None, "held": False, "watched": False, "weight": None})
        v["isin"] = v["isin"] or (isin.upper() if isin else None)
        v["name"] = v["name"] or name
        v[how] = True
        weigh(v, w)

    def other(bucket: dict, kind: str, ident: str, name: str | None, how: str, w: float | None = None) -> None:
        if (why := gone.get(lifecycle.key(kind, ident))) is not None:
            t.ended[lifecycle.key(kind, ident)] = why
            return
        v = bucket.setdefault(ident, {"name": name, "held": False, "tracked": False, "weight": None})
        v[how] = True
        weigh(v, w)

    open_ids = set(s.scalars(select(PortfolioLot.holding_id).where(PortfolioLot.open_quantity > 0)))
    imap = isin_map(s) if open_ids else {}
    for h in s.scalars(select(PortfolioHolding).where(PortfolioHolding.id.in_(open_ids))) if open_ids else []:
        isin = (h.isin or "").upper() or None
        w = weights.get(h.id)
        # the ISIN map (today's listings) decides when it knows the ISIN: a holding with only an ISIN (a broker
        # holdings statement) gets its NSE symbol, and a BSE-only company stays BSE-only whatever symbol the broker
        # wrote (e.g. Groww's "NSE$")
        key = stock_key(isin, h.nse_symbol, h.bse_code, imap) if h.asset_type == "stock" else None
        if key:
            add(key, isin, h.name, "held", w)
        elif isin and isin.startswith("INE") and isin[7:9] in _DEBT_TYPES:
            other(t.bonds, "bond", isin, h.name, "held", w)
        elif h.asset_type == "mf" and h.scheme_code:
            other(t.funds, "fund", h.scheme_code.strip(), h.name, "held", w)
    for w in s.scalars(select(Watch).where(Watch.kind == "stock", Watch.active.is_(True))):
        comp = s.get(Company, w.company_id)
        key = (
            w.nse_symbol.upper()
            if w.nse_symbol and w.exchange != "BSE"
            else (f"BSE:{w.bse_code}" if w.bse_code else None)
        )
        if key:
            add(key, comp.isin if comp else None, comp.name if comp else None, "watched")
    for c in s.scalars(select(Company).where(Company.slug.like("bond-%") | Company.slug.like("mf-%"))):
        if c.slug.startswith("bond-"):
            other(t.bonds, "bond", c.slug[5:].upper(), c.name, "tracked")
        else:
            other(t.funds, "fund", c.slug[3:], c.name, "tracked")
    return t

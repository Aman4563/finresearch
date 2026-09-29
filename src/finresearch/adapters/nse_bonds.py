"""Listed bonds and NCDs traded in NSE's capital-market segment (live list with coupon, face value, last price,
maturity and rating)."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel

from finresearch.adapters.nse import NseClient, parse_nse_date, parse_nse_timestamp, parse_num

STANDARD_FACE = (Decimal(100), Decimal(1000), Decimal(10000), Decimal(100000), Decimal(1000000))


class ListedBond(BaseModel):
    symbol: str
    series: str | None
    isin: str
    coupon_pct: Decimal | None
    face_value: Decimal | None
    last_price: Decimal | None
    close: Decimal | None
    maturity: date | None
    next_interest_date: date | None
    rating: str | None
    rating_agency: str | None
    traded_value: Decimal | None
    as_of: datetime | None = None

    @property
    def warnings(self) -> list[str]:
        out = []
        if self.face_value is not None and self.face_value not in STANDARD_FACE:
            out.append(f"face value {self.face_value} is not a standard denomination: the bond may be partly "
                       "redeemed (amortising); YTM from coupon and maturity alone would be wrong")  # fmt: skip
        if self.next_interest_date and self.as_of and self.next_interest_date < self.as_of.date():
            out.append(
                f"NSE's next-interest date {self.next_interest_date} is in the past; use the offer document"
            )
        if not self.rating:
            out.append("no rating in NSE's list; find the current rating and agency")
        return out

    @classmethod
    def parse(cls, r: dict[str, Any], as_of: datetime | None = None) -> ListedBond:
        rating = (r.get("credit_rating") or "").strip() or None
        return cls(symbol=str(r.get("symbol", "")).strip(), series=r.get("series"), isin=r.get("isin", ""),
                   coupon_pct=parse_num(r.get("coupr")), face_value=parse_num(r.get("face_value")),
                   last_price=parse_num(r.get("ltP")), close=parse_num(r.get("close")),
                   maturity=parse_nse_date(r.get("maturity_date")),
                   next_interest_date=parse_nse_date(r.get("nxtip_date")) if r.get("nxtip_date") not in (None, "-") else None,
                   rating=rating, rating_agency=(r.get("rating_agency") or None), traded_value=parse_num(r.get("trdVal")),
                   as_of=as_of)  # fmt: skip


def parse_live_bonds(data: dict[str, Any]) -> list[ListedBond]:
    as_of = parse_nse_timestamp(data.get("timeStamp"))
    return [ListedBond.parse(r, as_of) for r in data.get("data") or []]


def search_bonds(bonds: list[ListedBond], query: str, limit: int = 20) -> list[ListedBond]:
    q = query.strip().upper()
    if not q:
        return []
    hits = [b for b in bonds if q in b.symbol.upper() or q == b.isin.upper()]
    return sorted(
        hits, key=lambda b: (b.isin.upper() != q, not b.symbol.upper().startswith(q), -(b.traded_value or 0))
    )[:limit]


async def live_bonds(client: NseClient | None = None) -> list[ListedBond]:
    async with client or NseClient() as nse:
        data, _ = await nse.get_json("/api/liveBonds-traded-on-cm", {"type": "bonds"})
    return parse_live_bonds(data)

"""Baseline offer facts parsed deterministically from NSE's (or, for a BSE SME issue, BSE's) issue information.

Live run 4 used the price band, lot size and fresh-issue amount only as inputs to calculations and never recorded
them as claims. The exchange publishes them verbatim, so the pipeline records them itself: each one is an atomic
claim citing the NSE API URL, the access time and the exact text, marked verified because it is parsed from the
primary exchange source.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from finresearch.db.models import Citation, Claim
from finresearch.fincalc.numbers import parse_number

NSE_DETAIL_URL = "https://www.nseindia.com/api/ipo-detail?symbol={symbol}&series=EQ"


@dataclass
class BaselineFact:
    metric: str
    value: Decimal
    unit: str
    statement: str
    quote: str
    importance: str = "high"


def _num(text: str) -> Decimal | None:
    try:
        return parse_number(text)
    except (ValueError, ArithmeticError):
        return None


def parse_baseline(issue_info: dict[str, str]) -> list[BaselineFact]:
    out: list[BaselineFact] = []
    price = issue_info.get("Price Range") or ""
    nums = [n for n in (_num(x) for x in re.findall(r"\d[\d,]*(?:\.\d+)?", price)) if n is not None]
    if nums:
        lo, hi = nums[0], nums[-1]
        out.append(
            BaselineFact(
                "price_band_upper", hi, "INR per share", f"Upper end of the price band is ₹{hi}", price
            )
        )
        out.append(BaselineFact("price_band_lower", lo, "INR per share", f"Lower end of the price band is ₹{lo}", price,
                                "normal"))  # fmt: skip
    lot = issue_info.get("Bid Lot") or issue_info.get("Lot Size") or ""  # SME issues use "Lot Size"
    m = re.search(r"(\d[\d,]*)\s*equity shares", lot, re.I)
    if m and (v := _num(m.group(1))) is not None:
        out.append(BaselineFact("lot_size", v, "shares", f"Bid lot is {v} equity shares", lot))
    fv = issue_info.get("Face Value") or ""
    m = re.search(r"(?:rs|re)\.?\s*(\d+(?:\.\d+)?)", fv, re.I)
    if m and (v := _num(m.group(1))) is not None:
        out.append(
            BaselineFact("face_value", v, "INR per share", f"Face value is ₹{v} per share", fv, "normal")
        )
    size = (issue_info.get("Issue Size") or "").strip('"')
    low = size.lower()
    ofs_at = low.find("offer for sale")
    fresh_part, ofs_part = (size[:ofs_at], size[ofs_at:]) if ofs_at >= 0 else (size, "")
    m = re.search(r"aggregating up to\s*(?:rs\.?\s*)?(\d[\d,]*(?:\.\d+)?)\s*million", fresh_part, re.I)
    if m and (v := _num(m.group(1))) is not None:
        out.append(
            BaselineFact(
                "fresh_issue_amount", v, "INR million", f"Fresh issue aggregates up to ₹{v} million", size
            )
        )
    m = re.search(r"aggregating up to\s*(?:rs\.?\s*)?(\d[\d,]*(?:\.\d+)?)\s*million", ofs_part, re.I)
    if m and (v := _num(m.group(1))) is not None:
        out.append(
            BaselineFact(
                "ofs_amount", v, "INR million", f"Offer for sale aggregates up to ₹{v} million", size
            )
        )
    m = re.search(r"up to\s*(\d[\d,]*)\s*equity shares", ofs_part, re.I)
    if m and (v := _num(m.group(1))) is not None:
        out.append(
            BaselineFact("ofs_shares", v, "shares", f"Offer for sale of up to {v} equity shares", size)
        )
    m = re.search(r"anchor[^0-9]*portion of\s*(\d[\d,]*)\s*equity shares", size, re.I)
    if m and (v := _num(m.group(1))) is not None:
        out.append(BaselineFact("anchor_portion_shares", v, "shares", f"Anchor portion of {v} equity shares", size,
                                "normal"))  # fmt: skip
    m = re.search(r"market maker[^0-9]*portion of\s*(\d[\d,]*)\s*equity shares", size, re.I)
    if m and (v := _num(m.group(1))) is not None:
        out.append(BaselineFact("market_maker_portion_shares", v, "shares", f"Market maker portion of {v} equity "
                                "shares (SME issue)", size, "normal"))  # fmt: skip
    return out


def record_baseline(session: Session, run_id: int, symbol: str | None, issue_info: dict[str, str],
                    fetched_at: datetime | None, *, exchange: str = "NSE", url: str | None = None) -> list[int]:  # fmt: skip
    """`exchange="BSE"` for a BSE-only SME issue, whose issue_info comes from BSE's issue details at `url`."""
    ids = []
    url = url or NSE_DETAIL_URL.format(symbol=symbol)
    for f in parse_baseline(issue_info):
        c = Claim(run_id=run_id, stream="facts", statement=f"{f.statement} ({exchange} issue information)",
                  claim_type="numeric", metric=f.metric, value=f.value, unit=f.unit, period="offer",
                  importance=f.importance, status="verified",
                  verifier_note=f"deterministic: parsed from {exchange} issue information (primary source)",
                  checks={"source": f"{exchange.lower()}_issue_info"})  # fmt: skip
        session.add(c)
        session.flush()
        session.add(Citation(claim_id=c.id, url=url, accessed_at=fetched_at, quote=f.quote[:1000]))
        ids.append(c.id)
    session.flush()
    return ids

"""Baseline facts for a listed stock, read deterministically from NSE (no LLM).

Each fact is recorded as a verified atomic claim citing the NSE page it came from and the access time: last
price, 52-week high and low, issued shares and market cap (fincalc), latest promoter holding and trailing
twelve-month dividends per share (summed from corporate actions by ex-date).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from finresearch.db.models import Citation, Claim


@dataclass
class StockFact:
    metric: str
    value: Decimal
    unit: str
    period: str
    statement: str
    url: str
    quote: str
    importance: str = "high"


def quote_page(symbol: str) -> str:
    return f"https://www.nseindia.com/get-quotes/equity?symbol={symbol}"


def stock_facts(symbol: str, quote, shareholding: list, actions: list, today: date) -> list[StockFact]:
    from finresearch.fincalc.valuation import market_cap

    page, out = quote_page(symbol), []
    asof = quote.as_of.strftime("%Y-%m-%d %H:%M IST") if quote and quote.as_of else today.isoformat()
    if quote and quote.last_price:
        out.append(StockFact("last_price", quote.last_price, "INR per share", asof,
                             f"{symbol} last traded at ₹{quote.last_price} ({asof})", page, f"lastPrice {quote.last_price}"))  # fmt: skip
    if quote and quote.week52_high and quote.week52_low:
        out.append(StockFact("week52_high", quote.week52_high, "INR per share", asof, f"52-week high ₹{quote.week52_high}",
                             page, f"yearHigh {quote.week52_high}", "normal"))  # fmt: skip
        out.append(StockFact("week52_low", quote.week52_low, "INR per share", asof, f"52-week low ₹{quote.week52_low}",
                             page, f"yearLow {quote.week52_low}", "normal"))  # fmt: skip
    if quote and quote.issued_shares:
        out.append(StockFact("shares_outstanding", quote.issued_shares, "shares", asof,
                             f"{quote.issued_shares:,} shares issued", page, f"issuedSize {quote.issued_shares}", "normal"))  # fmt: skip
        if quote.last_price:
            mcap = market_cap(quote.issued_shares, quote.last_price)
            out.append(StockFact("market_cap", mcap, "INR", asof, f"Market cap ₹{mcap:,.0f} (fincalc: shares x last "
                                 "price)", page, f"issuedSize {quote.issued_shares}; lastPrice {quote.last_price}"))  # fmt: skip
    latest = next((sh for sh in shareholding if sh.as_of and sh.promoter_pct is not None), None)
    if latest:
        out.append(StockFact("promoter_holding", latest.promoter_pct, "%", latest.as_of.isoformat(),
                             f"Promoter and promoter group held {latest.promoter_pct}% at {latest.as_of}",
                             latest.xbrl or page, f"pr_and_prgrp {latest.promoter_pct}"))  # fmt: skip
    year_ago = today - timedelta(days=365)
    paid = [a for a in actions if a.dividend_per_share and a.ex_date and year_ago < a.ex_date <= today]
    if paid:
        ttm = sum((a.dividend_per_share for a in paid), Decimal(0))
        out.append(StockFact("dividend_per_share_ttm", ttm, "INR per share", f"TTM to {today}",
                             f"Dividends of ₹{ttm} per share went ex in the last 12 months ({len(paid)} payment(s))",
                             page, "; ".join(f"{a.subject} ex {a.ex_date}" for a in paid), "normal"))  # fmt: skip
    return out


def record_stock_facts(
    session: Session, run_id: int, facts: list[StockFact], accessed_at: datetime
) -> list[int]:
    ids = []
    for f in facts:
        c = Claim(run_id=run_id, stream="facts", statement=f"{f.statement} (NSE)", claim_type="numeric",
                  metric=f.metric, value=f.value, unit=f.unit, period=f.period, importance=f.importance,
                  status="verified", verifier_note="deterministic: read from NSE (primary exchange source)",
                  checks={"source": "nse_equity"})  # fmt: skip
        session.add(c)
        session.flush()
        session.add(Citation(claim_id=c.id, url=f.url, accessed_at=accessed_at, quote=f.quote[:1000]))
        ids.append(c.id)
    session.flush()
    return ids

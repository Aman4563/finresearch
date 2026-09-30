"""Baseline facts for a listed stock, read deterministically from its exchange (no LLM).

Each fact is recorded as a verified atomic claim citing the exchange page it came from and the access time: last
price, 52-week high and low, issued shares and market cap (fincalc), latest promoter holding and trailing
twelve-month dividends per share (summed from corporate actions by ex-date).

NSE is the default. A BSE-only stock reads the same from BSE (adapters/bse_equity.py): BSE publishes no issued-share
count on its quote, so the market cap is BSE's own published figure (cited as such), and the latest quarter's revenue,
net profit and EPS come from its Integrated Filing XBRL (cited to the XBRL file).
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


def stock_facts(symbol: str, quote, shareholding: list, actions: list, today: date, *, exchange: str = "NSE",
                results: dict | None = None) -> list[StockFact]:  # fmt: skip
    from finresearch.fincalc.valuation import market_cap

    bse = exchange == "BSE"
    page = (getattr(quote, "page_url", None) or "https://www.bseindia.com/") if bse else quote_page(symbol)
    out: list[StockFact] = []
    # the exchange's own field names, quoted in each citation
    f_last, f_high, f_low = (
        ("LTP", "Fifty2WkHigh_adj", "Fifty2WkLow_adj") if bse else ("lastPrice", "yearHigh", "yearLow")
    )
    asof = quote.as_of.strftime("%Y-%m-%d %H:%M IST") if quote and quote.as_of else today.isoformat()
    if quote and quote.last_price:
        out.append(StockFact("last_price", quote.last_price, "INR per share", asof,
                             f"{symbol} last traded at ₹{quote.last_price} ({asof})", page, f"{f_last} {quote.last_price}"))  # fmt: skip
    if quote and quote.week52_high and quote.week52_low:
        out.append(StockFact("week52_high", quote.week52_high, "INR per share", asof, f"52-week high ₹{quote.week52_high}",
                             page, f"{f_high} {quote.week52_high}", "normal"))  # fmt: skip
        out.append(StockFact("week52_low", quote.week52_low, "INR per share", asof, f"52-week low ₹{quote.week52_low}",
                             page, f"{f_low} {quote.week52_low}", "normal"))  # fmt: skip
    if quote and quote.issued_shares:
        out.append(StockFact("shares_outstanding", quote.issued_shares, "shares", asof,
                             f"{quote.issued_shares:,} shares issued", page, f"issuedSize {quote.issued_shares}", "normal"))  # fmt: skip
        if quote.last_price:
            mcap = market_cap(quote.issued_shares, quote.last_price)
            out.append(StockFact("market_cap", mcap, "INR", asof, f"Market cap ₹{mcap:,.0f} (fincalc: shares x last "
                                 "price)", page, f"issuedSize {quote.issued_shares}; lastPrice {quote.last_price}"))  # fmt: skip
    elif bse and quote and getattr(quote, "market_cap", None):
        out.append(StockFact("market_cap", quote.market_cap, "INR", asof, f"Market cap ₹{quote.market_cap:,.0f} "
                             "(as published by BSE; BSE's quote gives no issued-share count)", page,
                             f"MktCapFull {quote.market_cap / 10_000_000:.2f} crore"))  # fmt: skip
    latest = next((sh for sh in shareholding if sh.as_of and sh.promoter_pct is not None), None)
    if latest:
        out.append(StockFact("promoter_holding", latest.promoter_pct, "%", latest.as_of.isoformat(),
                             f"Promoter and promoter group held {latest.promoter_pct}% at {latest.as_of}",
                             latest.xbrl or page, f"pr_and_prgrp {latest.promoter_pct}"))  # fmt: skip
    if results:
        out += _results_facts(results)
    year_ago = today - timedelta(days=365)
    paid = [a for a in actions if a.dividend_per_share and a.ex_date and year_ago < a.ex_date <= today]
    if paid:
        ttm = sum((a.dividend_per_share for a in paid), Decimal(0))
        out.append(StockFact("dividend_per_share_ttm", ttm, "INR per share", f"TTM to {today}",
                             f"Dividends of ₹{ttm} per share went ex in the last 12 months ({len(paid)} payment(s))",
                             page, "; ".join(f"{a.subject} ex {a.ex_date}" for a in paid), "normal"))  # fmt: skip
    return out


def _results_facts(results: dict) -> list[StockFact]:
    """The latest quarter's revenue, net profit and basic EPS, each cited to the filing's XBRL."""
    q = (results.get("quarters") or [None])[-1]
    if not q or not q.get("xbrl"):
        return []
    basis = "consolidated" if q.get("consolidated") else "standalone"
    out = []
    for metric, key, unit, words in (("revenue_quarter", "revenue", "INR", "Revenue from operations"),
                                     ("net_profit_quarter", "profit", "INR", "Net profit attributable to owners"),
                                     ("eps_quarter", "eps", "INR per share", "Basic EPS")):  # fmt: skip
        v = q.get(key)
        if v is None:
            continue
        val = Decimal(str(v))
        shown = f"₹{val:,.2f}" if unit == "INR per share" else f"₹{val:,.0f}"
        out.append(StockFact(metric, val, unit, f"{q['label']} (quarter ended {q['period_end']})",
                             f"{words} {shown} in {q['label']} ({basis}, from the results filing's XBRL)",
                             q["xbrl"], f"{key} {v}", "high" if key != "eps" else "normal"))  # fmt: skip
    return out


def record_stock_facts(session: Session, run_id: int, facts: list[StockFact], accessed_at: datetime, *,
                       exchange: str = "NSE") -> list[int]:  # fmt: skip
    ids = []
    for f in facts:
        c = Claim(run_id=run_id, stream="facts", statement=f"{f.statement} ({exchange})", claim_type="numeric",
                  metric=f.metric, value=f.value, unit=f.unit, period=f.period, importance=f.importance,
                  status="verified", verifier_note=f"deterministic: read from {exchange} (primary exchange source)",
                  checks={"source": "bse_equity" if exchange == "BSE" else "nse_equity"})  # fmt: skip
        session.add(c)
        session.flush()
        session.add(Citation(claim_id=c.id, url=f.url, accessed_at=accessed_at, quote=f.quote[:1000]))
        ids.append(c.id)
    session.flush()
    return ids

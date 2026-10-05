"""The display-price rule (fincalc.price) and everything that consumes it, from recorded exchange payloads.

Recorded 30-Sep-2026 after 16:00 IST (tests/fixtures/prices/): TMCV (last trade 420.00 != official close 421.65),
INFY (last = close 994.10), SAIL (ex-dividend Rs 2.35: base price 179.47 vs previous close 181.82), SSDL (ex-dividend
Rs 3 and last != close), BSE TMCV header, NSE's TMCV 1-minute chart and the NIFTY 50 index page. The only in-session
NSE payload here is DERIVED from the recorded one (timestamp moved into the session, closePrice set to 0 as NSE sends
it before the close is computed, a new lastPrice): NSE's in-session field state is not recorded in this repo, and the
guard that matters (a quote stamped before 15:30 is always "last traded") is tested with closePrice left populated too.
The BSE in-session header (29-Sep-2026 11:31) is a real recording.
"""

from __future__ import annotations

import copy
import json
from datetime import date, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest

from finresearch.adapters.http import IST
from finresearch.adapters.nse import Quote
from finresearch.fincalc.price import OFFICIAL_CLOSE, disagreement, display_price, price_view, session_over

FIX = Path(__file__).parent / "fixtures" / "prices"


def payload(name: str) -> dict:
    return json.loads((FIX / name).read_text())


def nse(sym: str) -> Quote:
    return Quote.parse(payload(f"nse_quote_{sym}_20260930_after_close.json"))


def in_session(
    sym: str = "TMCV", *, last: float = 425.3, close: float = 0, at: str = "30-Sep-2026 12:51:10"
) -> Quote:
    """The recorded TMCV payload moved into the session (see the module docstring for what is derived)."""
    p = copy.deepcopy(payload(f"nse_quote_{sym}_20260930_after_close.json"))
    e = p["equityResponse"][0]
    e["lastUpdateTime"] = at
    e["tradeInfo"]["lastPrice"] = e["orderBook"]["lastPrice"] = last
    e["metaData"]["closePrice"] = close
    return Quote.parse(p)


def ist(h: int, m: int, day: int = 30) -> datetime:
    return datetime(2026, 9, day, h, m, tzinfo=IST)


# --------------------------------------------------------------------------- the rule, from recorded payloads
def test_tmcv_after_close_shows_the_official_close_and_notes_the_last_trade():
    q = nse("TMCV")
    assert (q.last_price, q.close_price, q.previous_close, q.base_price) == (
        D("420"),
        D("421.65"),
        D("430.15"),
        D("430.15"),
    )
    v = price_view(q, exchange="NSE", now=ist(20, 0))
    assert v.kind == OFFICIAL_CLOSE and v.label == "Close (official)" and v.session == "closed"
    assert v.price == D("421.65") and v.last_traded == D("420") and v.differs
    # golden: 421.65 - 430.15 = -8.50; -8.50 / 430.15 = -1.97605...% (NSE's own change field, -10.15, is vs lastPrice)
    assert v.change == D("-8.50") and round(v.change_pct, 4) == D("-1.9761")
    assert any("Last trade ₹420" in n for n in v.notes)


def test_infy_after_close_last_trade_equals_close():
    v = price_view(nse("INFY"), exchange="NSE")
    assert v.kind == OFFICIAL_CLOSE and v.price == D("994.1") and not v.differs and v.notes == []
    assert v.change == D("-21.3") and round(v.change_pct, 4) == D("-2.0977")  # -21.3 / 1015.4


def test_ex_date_change_is_measured_from_the_adjusted_base_price():
    q = nse("SAIL")  # ex-dividend Rs 2.35 on 30-Sep-2026: 181.82 - 2.35 = 179.47
    assert q.previous_close == D("181.82") and q.base_price == D("179.47")
    v = price_view(q, exchange="NSE")
    assert v.reference == D("179.47") and v.reference_kind == "base_price"
    assert v.change == D("2.02") and round(v.change_pct, 4) == D("1.1255")  # NSE's pChange: 1.13
    assert any("adjusted" in n for n in v.notes)
    s = price_view(nse("SSDL"), exchange="NSE")  # ex-dividend Rs 3, last 75.00 vs close 75.81
    assert s.price == D("75.81") and s.change == D("-5.11") and round(s.change_pct, 4) == D("-6.3149")


def test_in_session_is_last_traded_even_if_a_stale_close_is_present():
    v = price_view(in_session(), exchange="NSE", now=ist(12, 52))
    assert v.kind == "last_traded" and v.session == "open" and v.price == D("425.3")
    assert v.change == D("-4.85")  # 425.30 - 430.15
    # NSE leaving yesterday's closePrice in the payload must not turn a live quote into "close"
    stale = price_view(in_session(close=421.65), exchange="NSE", now=ist(12, 52))
    assert stale.kind == "last_traded" and stale.price == D("425.3")


def test_after_1530_before_the_close_is_published():
    q = in_session(last=420, at="30-Sep-2026 15:35:00")
    q.indicative_close = D("421.50")
    v = price_view(q, exchange="NSE", now=ist(15, 35))
    assert (
        v.kind == "last_traded" and v.session == "closing" and v.label.endswith("(close not yet published)")
    )
    assert any("indicative close: ₹421.50" in n for n in v.notes)


def test_a_quote_from_an_earlier_day_is_over_and_no_trade_falls_back_to_previous_close():
    assert session_over(ist(11, 0, day=29), ist(8, 0)) and not session_over(ist(11, 0), ist(11, 1))
    assert session_over(ist(16, 0)) and session_over(None)
    q = Quote(symbol="X", previous_close=D("100"), as_of=ist(9, 5))
    v = price_view(q, exchange="NSE")
    assert v.kind == "previous_close" and v.price == D("100") and v.change is None


def test_bse_in_session_header_is_last_traded_and_bse_after_close_uses_the_days_bar():
    from finresearch.adapters.bse_equity import build_quote

    fx = Path(__file__).parent / "fixtures" / "bse" / "equity"
    q = build_quote("500209", json.loads((fx / "header_500209.json").read_text()), None, None, None)
    v = price_view(q)
    assert v.exchange == "BSE" and v.kind == "last_traded"
    after = build_quote("544569", payload("bse_header_544569_20260930.json"), None,
                        payload("bse_trading_544569_20260930.json"), None)  # fmt: skip
    assert after.last_price == D("421.60") and after.close_price is None  # the header has no close field
    assert price_view(after).kind == "last_traded"  # until the adapter adds the day's bar
    after.close_price = D("421.60")  # BseEquity.quote reads it from the price history after the session
    v = price_view(after)
    assert (
        v.kind == OFFICIAL_CLOSE and v.price == D("421.60") and v.change == D("-8.65")
    )  # vs BSE PrevClose 430.25


def test_bse_reference_adjusts_for_a_dividend_and_refuses_a_split():
    q = Quote(symbol="X", last_price=D("98"), previous_close=D("110"), as_of=ist(11, 0))
    v = price_view(q, exchange="BSE", dividend_today=D("10"))
    assert (
        v.reference == D("100") and v.reference_kind == "previous_close_less_dividend" and v.change == D("-2")
    )
    s = price_view(q, exchange="BSE", action_today="Stock Split From Rs.10/- to Rs.2/-")
    assert s.change is None and s.change_pct is None and any("not shown" in n for n in s.notes)


def test_market_cap_golden_matches_nses_own_figure():
    from finresearch.api.markets import quote_json

    q = nse("TMCV")
    j = quote_json(q, "TMCV")
    # 3,682,789,352 shares x 421.65 = 1,552,848,130,270.80 = NSE's totalMarketCap to the paisa
    assert j["market_cap"] == "1552848130271" and q.exchange_market_cap == D("1552848130270.8")
    assert j["market_cap_basis"] == "issued shares × close (official)" and j["quality"] == []
    assert j["price"] == "421.65" and j["last_price"] == "420" and j["official_close"] == "421.65"
    assert j["price_kind"] == "official_close" and j["last_differs"] and j["change"] == "-8.50"
    assert j["change_pct"] == -1.9761 and j["reference_kind"] == "previous_close"  # base = previous close
    assert disagreement(D("101"), D("100")) == D("0.01") and disagreement(None, D("1")) is None


def test_quote_json_flags_a_market_cap_that_disagrees_with_the_exchange():
    from finresearch.api.markets import quote_json

    q = nse("TMCV")
    q.exchange_market_cap = D("1540000000000")
    j = quote_json(q, "TMCV")
    assert j["quality"] and j["quality"][0]["field"] == "market_cap"
    assert {v["source"] for v in j["quality"][0]["values"]} == {"issued shares × close (official)",
                                                                  "NSE quote tradeInfo.totalMarketCap"}  # fmt: skip


def test_ex_today_picks_todays_dividend_and_price_actions():
    from finresearch.adapters.nse_equity import CorporateAction
    from finresearch.api.markets import ex_today

    q = Quote(symbol="X", as_of=ist(11, 0))
    acts = [CorporateAction(symbol="X", subject="Dividend - Rs 2.35 Per Share", ex_date=date(2026, 9, 30),
                            record_date=None, dividend_per_share=D("2.35")),
            CorporateAction(symbol="X", subject="Bonus 1:1", ex_date=date(2026, 9, 30), record_date=None,
                            dividend_per_share=None),
            CorporateAction(symbol="X", subject="Dividend - Rs 9", ex_date=date(2026, 9, 1), record_date=None,
                            dividend_per_share=D("9"))]  # fmt: skip
    assert ex_today(acts, q) == (D("2.35"), "Bonus 1:1")
    assert ex_today(acts[2:], q) == (None, None) and ex_today(acts, None) == (None, None)


def test_consumers_use_the_display_price():
    from finresearch.portfolio.valuation import price_from_quote
    from finresearch.verify.stock_baseline import stock_facts

    q = nse("TMCV")
    p = price_from_quote(q, "NSE")
    assert p.price == D("421.65") and p.source == "NSE quote: close (official)"
    assert (
        display_price(q) == D("421.65")
        and display_price(in_session()) == D("425.3")
        and display_price(None) is None
    )
    facts = {f.metric: f for f in stock_facts("TMCV", q, [], [], date(2026, 9, 30))}
    assert facts["close_price"].value == D("421.65") and "last traded ₹420" in facts["close_price"].statement
    assert facts["market_cap"].value == D("3682789352") * D("421.65")


# --------------------------------------------------------------------------- monitor: the listing-day close
def test_listing_close_waits_for_the_official_close():
    import asyncio
    from types import SimpleNamespace

    from finresearch.monitor.jobs import CloseNotPublished, listing

    W = lambda: SimpleNamespace(meta={}, nse_symbol="TMCV", listing_date=date(2026, 9, 30), company_id=1)  # noqa: E731
    J = lambda: SimpleNamespace(params={"which": "close"})  # noqa: E731

    class Deps:
        async def quote(self, sym):
            return in_session(at="30-Sep-2026 15:35:00", last=420)

    with pytest.raises(CloseNotPublished):
        asyncio.run(listing(None, J(), W(), Deps(), ist(15, 45)))


# --------------------------------------------------------------------------- intraday: the chart's post-close point
@pytest.fixture
def chart_app(env, monkeypatch):
    from fastapi.testclient import TestClient

    from finresearch.adapters.nse_intraday import parse_chart
    from finresearch.api import create_app

    monkeypatch.setattr("finresearch.api.live._holidays", lambda: {})

    async def fetch(kind, sym):
        return parse_chart(payload("nse_chart_TMCV_20260930.json"), sym, kind, "https://www.nseindia.com/")

    app = create_app(clock=lambda: ist(20, 0))
    app.state.intraday_fetch = fetch
    with TestClient(app) as c:
        yield c


def test_intraday_after_close_ends_on_the_official_close(chart_app):
    r = chart_app.get("/api/stocks/TMCV/intraday?interval=5m").json()
    assert not r["live"] and r["last_kind"] == "official_close" and r["last_label"] == "Close (official)"
    assert r["last"] == 421.65 and r["official_close"] == 421.65 and r["last_traded"] == 420
    assert r["candles"][-1]["c"] == 420  # candles stop at 15:30: the last one closes on the last trade
    assert r["change"] == -8.5 and round(r["change_pct"], 4) == -1.9761


def test_post_close_is_none_in_session():
    from finresearch.api.intraday import post_close
    from finresearch.fincalc.candles import Tick

    assert post_close([Tick(ist(15, 29), D(1)), Tick(ist(15, 30), D(2))]) is None
    assert post_close([Tick(ist(15, 29), D(1)), Tick(ist(15, 31), D(3))]) == D(3)


# --------------------------------------------------------------------------- the audit, with fakes
class _Bar:
    def __init__(self, day, close, prev=None, vwap=None):
        self.day, self.close, self.prev_close, self.vwap = day, D(str(close)), (D(str(prev)) if prev else None), (D(str(vwap)) if vwap else None)  # fmt: skip


def _sources(**over):
    from finresearch.adapters.nse_intraday import parse_chart
    from finresearch.evals.data_audit import AuditSources

    day = date(2026, 9, 30)

    async def nse_quote(sym):
        return nse(sym)

    async def nse_history(sym, a, b):
        return [_Bar(day - timedelta(days=1), "430.15", "432.95"), _Bar(day, "421.65", "430.15", "424.76")]

    async def nse_intraday(sym):
        return parse_chart(payload("nse_chart_TMCV_20260930.json"), sym, "equity", "")

    async def none(*a):
        return None

    base = dict(nse_quote=nse_quote, nse_history=nse_history, bse_quote=none, bse_history=none,
                nse_intraday=nse_intraday, bse_intraday=none, bse_code_for_isin=none)  # fmt: skip
    base.update(over)
    return AuditSources(**base)


def _run(src, syms=("TMCV",)):
    import asyncio

    from finresearch.evals.data_audit import run_audit

    return asyncio.run(run_audit(src, syms, index=None, fund=None, bond=False, at=ist(20, 0)))


def test_audit_tmcv_is_clean_and_reports_last_vs_close_as_expected():
    rep = _run(_sources())
    by = {c.check: c for c in rep.checks}
    assert not rep.mismatches, [c for c in rep.mismatches]
    assert by["NSE quote close = NSE history close"].status == "ok"
    assert by["app display price = official close"].status == "ok"
    assert (
        by["last trade vs official close"].status == "expected"
        and "gap -1.65" in by["last trade vs official close"].note
    )
    assert by["market cap = NSE totalMarketCap"].status == "ok"
    assert by["NSE intraday post-close point = official close"].status == "ok"
    assert by["NSE day VWAP = history VWAP"].status == "ok"  # 424.75 vs 424.76 within 0.02 %
    assert by["app day change = history close − reference"].status == "ok"
    md = rep.markdown()
    assert md.startswith("Price audit at 2026-09-30 20:00 IST") and "| TMCV |" in md


def test_audit_flags_a_real_mismatch_and_survives_a_failing_source():
    day = date(2026, 9, 30)

    async def bad_history(sym, a, b):
        return [_Bar(day, "419.00", "430.15")]

    async def broken(sym):
        raise RuntimeError("HTTP 403")

    rep = _run(_sources(nse_history=bad_history, nse_intraday=broken))
    mism = {c.check for c in rep.mismatches}
    assert "NSE quote close = NSE history close" in mism
    assert any(c.status == "error" and "HTTP 403" in c.note for c in rep.checks)
    assert rep.as_json()["summary"]["mismatch"] >= 1


def test_listing_day_change_is_from_the_discovered_price_not_a_corporate_action():
    """#200: on its listing day NSE's previous close is the issue price (272) and the base price the pre-open call
    auction price (450). The change (-10 % at 405) is from 450, and the note must not call it a corporate action."""
    from datetime import UTC, date, datetime

    from finresearch.adapters.nse import Quote
    from finresearch.fincalc.price import REF_LABELS, price_view

    at = datetime(2026, 10, 5, 10, 30, tzinfo=UTC)  # 16:00 IST
    q = Quote(symbol="EXAMPLE", last_price=D(405), close_price=D(405), previous_close=D(272), base_price=D(450),
              listing_date=date(2026, 10, 5), as_of=at)  # fmt: skip
    v = price_view(q, now=at)
    assert (v.reference, v.reference_kind) == (D(450), "listing_price") and v.change == D(-45)
    assert any("issue price was ₹272" in n for n in v.notes) and not any(
        "corporate action" in n for n in v.notes
    )
    assert "pre-open call auction" in REF_LABELS["listing_price"]
    later = Quote(symbol="EXAMPLE", last_price=D(410), previous_close=D(405), base_price=D(405),
                  listing_date=date(2026, 10, 5), as_of=datetime(2026, 10, 6, 5, 0, tzinfo=UTC))  # fmt: skip
    assert price_view(later, now=datetime(2026, 10, 6, 5, 0, tzinfo=UTC)).reference_kind == "previous_close"

"""The valuation agents' market-fact tools (#504 part A, mcp_server.market_facts): the risk-free rate from the rate
chain, stored index history and the beta of a stock from stored closes. Synthetic closes only (made-up symbols); the
rate tests use the hand-typed RBI policy table and RBI's published 14-Oct-2026 cut-off quoted in fincalc.riskfree."""

from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal as D
from fractions import Fraction

import pytest

START = date(2026, 6, 1)
INDEX_URL = "https://nsearchives.nseindia.com/content/indices/ind_close_all_{:%d%m%Y}.csv"
EQUITY_URL = "https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{:%d%m%Y}.csv"


@pytest.fixture(autouse=True)
def _clean(request):
    """Each database test starts with no stored closes or corporate actions (the test database is shared)."""
    if "env" in request.fixturenames:
        request.getfixturevalue("env")
        from sqlalchemy import text

        from finresearch.db import session_scope

        with session_scope() as s:
            s.execute(text("TRUNCATE index_eod, index_eod_day, equity_eod, equity_eod_day, corp_action, "
                           "corp_action_fetch"))  # fmt: skip
    yield


def _days(n: int) -> list[date]:
    return [START + timedelta(days=i) for i in range(n)]


def _load_index(symbol: str, closes: list, name: str = "Nifty 50") -> list[date]:
    from finresearch.db import session_scope
    from finresearch.db.models import IndexEod, IndexEodDay

    days = _days(len(closes))
    with session_scope() as s:
        for d, c in zip(days, closes, strict=True):
            s.merge(IndexEodDay(day=d, status="loaded", url=INDEX_URL.format(d), rows=1))
            s.add(IndexEod(day=d, symbol=symbol, index_name=name, close=None if c is None else D(str(c)),
                           pe=D("22.50"), pb=D("3.40"), div_yield=D("1.25")))  # fmt: skip
    return days


def _load_stock(symbol: str, closes: dict[date, object], series: str = "EQ") -> None:
    from finresearch.db import session_scope
    from finresearch.db.models import EquityEod, EquityEodDay

    with session_scope() as s:
        for d, c in closes.items():
            s.merge(EquityEodDay(day=d, status="loaded", url=EQUITY_URL.format(d), rows=1))
            s.add(EquityEod(day=d, symbol=symbol, series=series, close=D(str(c))))


def _action(
    symbol: str, ex: date, status: str, factor: str | None, kind: str = "split", reason: str | None = None
):
    from finresearch.db import session_scope
    from finresearch.db.models import CorpAction, CorpActionFetch

    with session_scope() as s:
        s.merge(CorpActionFetch(source="nse", symbol=symbol, status="loaded", rows=1))
        s.add(CorpAction(source="nse", symbol=symbol, ex_date=ex, subject=kind, subject_key=kind, kind=kind,
                         status=status, price_factor=None if factor is None else D(factor), reason=reason,
                         terms={}))  # fmt: skip


def _call(fn: str, **args):
    from finresearch.db import session_scope
    from finresearch.mcp_server import market_facts

    with session_scope() as s:
        return getattr(market_facts, fn)(s, **args)


# two levels each, so every return is one of two values and the regression line is the line through two points
IDX = (20000, 20480)  # +2.4 %, then -2.34375 %
STK = (1000, 1050)  # +5 %, then -4.7619 %
TWO_POINT_BETA = (Fraction(1050, 1000) - Fraction(1000, 1050)) / (
    Fraction(20480, 20000) - Fraction(20000, 20480)
)


def _pattern(levels: tuple[int, int], n: int) -> list[int]:
    return [levels[i % 2] for i in range(n)]


# --------------------------------------------------------------------------- risk-free rate
def test_risk_free_rate_is_the_chains_proxy_answer_with_tier_date_and_source(env):
    """conftest pins today to 23-Oct-2026 with no owner entry: the RBI repo-rate proxy, repo 5.50 % in force from
    07-Oct-2026 (fincalc.policy_rates). For 364 days the simple yield is 5.50 %; effective annual, by the formula
    in fincalc.riskfree, (1 + 0.055 x 364/365)^(365/364) - 1 = 5.5004 %; continuous ln(1 + 0.055 x 364/365) x
    365/364 = 5.3545 %."""
    out = _call("risk_free_rate")
    y, n = 0.055, 364
    assert out["known"] is True and (out["tenor"], out["day"], out["tier"]) == ("364D", "2026-10-23", "proxy")
    assert out["yield_simple"] == "0.055000"
    assert out["rate_effective_annual"] == f"{(1 + y * n / 365) ** (365 / n) - 1:.6f}" == "0.055004"
    assert out["rate_effective_annual_pct"] == "5.5004" and out["rate_continuous"] == "0.053545"
    assert out["as_of"] == "2026-10-07" and out["source"].startswith("RBI repo-rate proxy: repo 5.50 %")
    assert out["url"].startswith("https://") and "held flat beyond 91 days" in out["note"]
    assert out["stale"] is False and "short rate" in out["caveat"] and "term premium" in out["caveat"]
    assert out["reproduce"] == {"tenor": "364D", "day": "2026-10-23"}
    assert _call("risk_free_rate", **out["reproduce"]) == out


def test_risk_free_rate_owner_tier_reproduces_rbis_published_cut_off(env, monkeypatch):
    """RBI's 91-day cut-off of 14-Oct-2026, 5.7281 %, entered by the owner (the worked example in fincalc.riskfree's
    docstring: 5.6876 % continuous). By hand: y x D/365 = 0.057281 x 91/365 = 0.01428102; ln(1.01428102) =
    0.01418000; x 365/91 = 0.05687584, so 5.6876 % continuous; e^0.05687584 - 1 = 0.05852437, so 5.8524 % effective
    annual (the docstring's 5.8525 % is e^0.056876 - 1 = 0.0585245 from the already rounded continuous rate)."""
    from finresearch.fincalc.riskfree import OwnerRate
    from finresearch.signals import rates

    entry = OwnerRate(date(2026, 10, 14), 91, D("0.057281"), "RBI 91-day T-bill auction result, 14-Oct-2026")
    monkeypatch.setattr(rates, "OWNER", lambda: [entry])
    out = _call("risk_free_rate", tenor="91d")
    assert (out["tier"], out["tenor"], out["as_of"]) == ("owner", "91D", "2026-10-14")
    assert out["yield_simple"] == "0.057281" and out["rate_effective_annual_pct"] == "5.8524"
    assert out["rate_continuous"] == "0.056876" and out["rate_effective_annual"] == "0.058524"
    assert out["source"] == "owner-entered rate: 91D for 14-Oct-2026"
    assert out["url"] == "RBI 91-day T-bill auction result, 14-Oct-2026"  # what the owner typed as its source


def test_risk_free_rate_says_unknown_with_the_reason_and_no_number(env):
    numbers = {"rate_effective_annual", "rate_effective_annual_pct", "yield_simple", "rate_continuous"}
    before = _call("risk_free_rate", day="2014-12-31")
    assert before["known"] is False and "before 01-Jan-2015" in before["reason"] and not numbers & set(before)
    for tenor in ("10Y", "3650D", "365D", "0D", ""):
        long = _call("risk_free_rate", tenor=tenor)
        assert long["known"] is False and "tenor must be 1D to 364D" in long["reason"], tenor
        assert "no yield beyond 364 days" in long["reason"] and not numbers & set(long)
    future = _call("risk_free_rate", day="2026-10-24")
    assert future["known"] is False and "in the future" in future["reason"]


def test_risk_free_rate_server_tool_and_a_stale_answer(env, monkeypatch):
    """The MCP tool returns the module's answer; a past day gets the rate then in force (repo 5.25 % from
    05-Dec-2025 until 07-Oct-2026, fincalc.policy_rates); a bad date is an unknown answer, not an exception; and
    from RBI's next scheduled decision (04-Dec-2026) on, with no decision read, the answer is `stale` with the
    reason in its note (the chain's own flag, #492)."""
    from finresearch.mcp_server import server
    from finresearch.signals import rates

    assert json.loads(server.risk_free_rate()) == _call("risk_free_rate")
    earlier = json.loads(server.risk_free_rate("91D", "2026-10-01"))
    assert (earlier["as_of"], earlier["yield_simple"]) == ("2025-12-05", "0.052500")
    bad = json.loads(server.risk_free_rate("91D", "yesterday"))
    assert bad == {"known": False, "reason": "day must be a date YYYY-MM-DD, got 'yesterday'"}
    monkeypatch.setattr(rates, "TODAY", lambda: date(2026, 12, 5))
    out = json.loads(server.risk_free_rate())
    assert out["known"] is True and out["stale"] is True
    assert "possibly stale" in out["note"] and "04-Dec-2026" in out["note"]


# --------------------------------------------------------------------------- index history
def test_index_history_returns_stored_closes_with_their_source_and_keeps_unknown_null(env):
    days = _load_index("NIFTY", [24000, "24120.5", None, 24300, "24250.25"])
    out = _call("index_history", symbol=" nifty ", start="2026-06-01", end="2026-06-05")
    assert out["known"] is True and (out["symbol"], out["index_name"], out["n"]) == ("NIFTY", "Nifty 50", 5)
    assert out["columns"] == ["day", "close", "pe", "pb", "div_yield_pct"]
    assert out["rows"][0] == ["2026-06-01", "24000", "22.5", "3.4", "1.25"]
    assert out["rows"][1][1] == "24120.5" and out["rows"][2] == ["2026-06-03", None, "22.5", "3.4", "1.25"]
    assert (out["first"], out["last"], out["next_start"]) == ("2026-06-01", "2026-06-05", None)
    assert (
        out["provenance"]["source_url"] == INDEX_URL.format(days[-1])
        and "index_eod" in out["provenance"]["dataset"]
    )
    assert _call("index_history", **out["reproduce"]) == out
    # default range: the year to `today`
    assert _call("index_history", symbol="NIFTY", today=date(2026, 6, 30))["n"] == 5
    assert _call("index_history", symbol="NIFTY", today=date(2027, 6, 30))["known"] is False


def test_index_history_pages_instead_of_truncating(env, monkeypatch):
    from finresearch.mcp_server import market_facts

    _load_index("NIFTY 500", list(range(100, 107)), name="Nifty 500")
    monkeypatch.setattr(market_facts, "MAX_ROWS", 3)
    seen, start = [], "2026-06-01"
    while start:
        page = _call("index_history", symbol="Nifty  500", start=start, end="2026-06-30")
        assert page["n"] <= 3 and ("more rows" in page["note"]) == (page["next_start"] is not None)
        seen += [r[0] for r in page["rows"]]
        start = page["next_start"]
    assert seen == [d.isoformat() for d in _days(7)]  # every stored day once, across three pages


def test_index_history_unknown_answers_name_no_stored_symbol(env):
    from finresearch.mcp_server import server

    _load_index("ZZPRIVATEINDEX", [100, 101, 102])
    _load_stock("ZZHELD", {d: 50 for d in _days(3)})
    for out in (
        _call("index_history", symbol="NOSUCH", end="2026-06-30"),
        _call("index_history", symbol=""),
        _call("index_history", symbol="NIFTY", start="2026-07-01", end="2026-06-01"),
        _call("stock_beta", symbol="NOSUCH", index="NOSUCHINDEX", end="2026-06-03"),
        json.loads(server.index_history("NIFTY", "junk")),
    ):
        assert out["known"] is False and "rows" not in out and "beta" not in out
        assert "ZZHELD" not in json.dumps(out) and "ZZPRIVATEINDEX" not in json.dumps(out)
    assert json.loads(server.index_history("ZZPRIVATEINDEX", "2026-06-01", "2026-06-03"))["n"] == 3


# --------------------------------------------------------------------------- stock beta
def _two_level_world(n: int = 101, symbol: str = "ACMEBANK") -> list[date]:
    days = _load_index("NIFTY", _pattern(IDX, n))
    _load_stock(symbol, dict(zip(days, _pattern(STK, n), strict=True)))
    return days


def test_stock_beta_from_stored_closes_equals_the_two_point_closed_form(env):
    """Independent check. The index alternates 20,000 and 20,480 and the stock 1,000 and 1,050, so every pair of
    returns is one of two points and least squares is the line through them:
    beta = (1.05 - 1000/1050) / (1.024 - 20000/20480) = 0.0976190 / 0.0474375 = 2.0578, with no residual
    (standard error 0, R-squared 1). 101 stored sessions give 100 returns; the largest move is the +5 %."""
    days = _two_level_world()
    out = _call("stock_beta", symbol="acmebank", end=days[-1].isoformat(), sessions=100)
    assert out["known"] is True and (out["symbol"], out["index"], out["n"]) == ("ACMEBANK", "NIFTY", 100)
    assert out["beta"] == f"{float(TWO_POINT_BETA):.4f}" == "2.0578"
    assert (out["se"], out["r_squared"], out["missing_sessions"]) == ("0.0000", "1.0000", 0)
    assert (out["window_start"], out["window_end"]) == (days[0].isoformat(), days[-1].isoformat())
    assert (out["largest_move"], out["largest_move_date"]) == ("0.0500", days[1].isoformat())
    assert (
        "no corporate-action history is stored for ACMEBANK" in out["adjustment"]
        and out["adjusted_for"] == []
    )
    prov = out["provenance"]
    assert prov["equity_source_url"] == EQUITY_URL.format(days[-1]) and prov[
        "index_source_url"
    ] == INDEX_URL.format(days[-1])
    assert "fincalc.market.beta" in prov["computation"] and prov["method_version"] == "market_facts/1"
    assert "citation" not in out  # no re-runnable citation exists for these tools yet: none is promised
    # the echoed arguments give the same answer again, whatever "today" is by then
    assert out["reproduce"] == {
        "symbol": "ACMEBANK",
        "index": "NIFTY",
        "end": days[-1].isoformat(),
        "sessions": 100,
    }
    assert _call("stock_beta", today=date(2027, 1, 1), **out["reproduce"]) == out
    # the default window ends at the latest stored session, which must be fresh
    assert _call("stock_beta", symbol="ACMEBANK", sessions=100, today=days[-1] + timedelta(days=3)) == out


def test_stock_beta_back_adjusts_a_stored_split_and_refuses_an_unexplained_one(env):
    """The same company with a 2-for-1 split going ex on session 51: NSE's stored closes are unadjusted, 2,000 and
    2,100 before it and 1,000 and 1,050 from it. With the factor 2 stored the back-adjusted series is the unsplit
    one, so the beta is the same 2.0578. Without it the ex-day "return" is 1,050 / 2,000 - 1 = -47.5 %."""
    days = _load_index("NIFTY", _pattern(IDX, 101))
    raw = {d: c * (2 if i < 51 else 1) for i, (d, c) in enumerate(zip(days, _pattern(STK, 101), strict=True))}
    _load_stock("SPLITCO", raw)
    args = {"symbol": "SPLITCO", "end": days[-1].isoformat(), "sessions": 100}
    blind = _call("stock_beta", **args)
    assert blind["known"] is False and "beta" not in blind
    assert (
        f"SPLITCO moved -47.5% on {days[51]}" in blind["reason"]
        and "no stored corporate action" in blind["reason"]
    )
    _action("SPLITCO", days[51], "known", "2")
    out = _call("stock_beta", **args)
    assert out["known"] is True and out["beta"] == "2.0578" and out["se"] == "0.0000"
    assert (
        out["adjusted_for"] == [[days[51].isoformat(), "2"]]
        and "back-adjusted for 1 stored" in out["adjustment"]
    )


def test_stock_beta_refuses_an_action_with_no_known_factor(env):
    days = _two_level_world(symbol="DEMERGECO")
    _action(
        "DEMERGECO",
        days[40],
        "unknown",
        None,
        kind="demerger",
        reason="the demerged unit's value is not stated",
    )
    out = _call("stock_beta", symbol="DEMERGECO", end=days[-1].isoformat(), sessions=100)
    assert out["known"] is False and f"went ex on {days[40]} with no known price factor" in out["reason"]
    assert "demerger" in out["reason"] and "not stated" in out["reason"]
    # an action before the window does not matter to the returns inside it
    assert _call("stock_beta", symbol="DEMERGECO", end=days[-1].isoformat(), sessions=60)["known"] is True


def test_stock_beta_pairs_only_sessions_both_have_and_refuses_a_gappy_series(env):
    """The stock has no close on 5 of the 101 index sessions (under the 10 % limit): the returns are taken between
    the sessions both have, so the answer must be fincalc.market.beta on exactly those closes (computed here from
    the synthetic arrays, not read back from the database). With 11 missing (over 10 %) it is refused."""
    from finresearch.fincalc import market

    days = _load_index("NIFTY", _pattern(IDX, 101))
    gone = {days[i] for i in (10, 11, 30, 55, 80)}
    stock = dict(zip(days, _pattern(STK, 101), strict=True))
    _load_stock("GAPCO", {d: c for d, c in stock.items() if d not in gone})
    out = _call("stock_beta", symbol="GAPCO", end=days[-1].isoformat(), sessions=100)
    kept = [d for d in days if d not in gone]
    index = dict(zip(days, _pattern(IDX, 101), strict=True))
    want = market.beta([stock[d] for d in kept], [index[d] for d in kept], kept, kept, as_of=days[-1])
    assert out["known"] is True and (out["missing_sessions"], out["n"]) == (5, 95)
    assert out["beta"] == f"{want.beta:.4f}" and out["se"] == f"{want.se:.4f}" and out["beta"] != "2.0578"

    more = {days[i] for i in range(20, 31)}  # 11 sessions
    _load_stock("HOLECO", {d: c for d, c in stock.items() if d not in more})
    bad = _call("stock_beta", symbol="HOLECO", end=days[-1].isoformat(), sessions=100)
    assert (
        bad["known"] is False and "misaligned: HOLECO has no stored close on 11 of the 101" in bad["reason"]
    )


def test_stock_beta_refuses_short_and_stale_series_and_bad_arguments(env):
    days = _two_level_world(n=81)  # 80 returns stored
    ask = {"symbol": "ACMEBANK", "end": days[-1].isoformat()}
    short = _call("stock_beta", **ask)  # the default 250 sessions
    assert (
        short["known"] is False and "short: only 80 sessions of NIFTY returns are stored" in short["reason"]
    )
    assert _call("stock_beta", sessions=80, **ask)["known"] is True
    for sessions in (59, 1251, "100", 80.0, True):
        out = _call("stock_beta", sessions=sessions, **ask)
        assert out["known"] is False and "sessions must be a whole number from 60 to 1250" in out["reason"]
    # no `end`: the latest stored session is 8 days old on the day of asking
    stale = _call("stock_beta", symbol="ACMEBANK", sessions=80, today=days[-1] + timedelta(days=8))
    assert (
        stale["known"] is False
        and f"stale: the stored NIFTY closes end on {days[-1]}, 8 days" in stale["reason"]
    )
    # the index is fresh but the stock's last close is 8 days before the window's end (8 of 81 sessions missing is
    # inside the 10 % gap limit, so it is the staleness rule that refuses it)
    _load_stock("HALTED", dict(zip(days[:-8], _pattern(STK, 73), strict=True)))
    halted = _call("stock_beta", symbol="HALTED", sessions=80, **{"end": days[-1].isoformat()})
    assert (
        halted["known"] is False
        and f"stale: the series ends on {days[-9]}, 8 days before" in halted["reason"]
    )
    future = _call("stock_beta", symbol="ACMEBANK", end="2026-12-31", today=date(2026, 12, 1))
    assert future["known"] is False and "in the future" in future["reason"]
    assert _call("stock_beta", symbol="")["known"] is False
    assert (
        "no stored closes for NOSUCH"
        in _call("stock_beta", symbol="NOSUCH", sessions=80, **{"end": ask["end"]})["reason"]
    )


def test_stock_beta_reads_the_price_series_only_and_says_when_actions_are_stored(env):
    """A government security or bond listed under the same symbol (series GS, N1) is never the stock's price; the
    trade-to-trade series BE is. With the stock's corporate-action history loaded and no action in the window, the
    answer says the closes were checked against it."""
    from finresearch.db import session_scope
    from finresearch.db.models import CorpActionFetch
    from finresearch.mcp_server import server

    days = _load_index("NIFTY", _pattern(IDX, 81))
    _load_stock("T2TCO", dict(zip(days, _pattern(STK, 81), strict=True)), series="BE")
    _load_stock("T2TCO", {d: 99 for d in days}, series="N1")  # a flat bond price: would give no beta at all
    with session_scope() as s:
        s.add(CorpActionFetch(source="nse", symbol="T2TCO", status="loaded", rows=0))
    out = json.loads(server.stock_beta("T2TCO", "NIFTY", days[-1].isoformat(), 80))
    assert out["known"] is True and out["beta"] == "2.0578" and out["n"] == 80
    assert out["adjustment"].startswith("back-adjusted for 0 stored") and out["frequency"] == "daily"
    assert "Scholes & Williams" in out["note"] and "standard error" in out["note"]

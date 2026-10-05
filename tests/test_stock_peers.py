"""Stock peer table (#178): metrics, peer selection and summary on synthetic data. Expected values are worked by hand
in the comments, not copied from the code's output."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from finresearch.fincalc import peers as P

ENDS = [date(2024, 9, 30), date(2024, 12, 31), date(2025, 3, 31), date(2025, 6, 30),
        date(2025, 9, 30), date(2025, 12, 31), date(2026, 3, 31), date(2026, 6, 30)]  # fmt: skip
REV = [100, 100, 100, 100, 110, 110, 120, 120]
PAT = [10, 10, 10, 10, 12, 12, 14, 14]
EPS = [1, 1, 1, 1, 5, 6, 7, 8]


def quarters(eps=EPS, consolidated=True, drop=()):
    return [P.Quarter(e, Decimal(r), Decimal(p), Decimal(x), consolidated, f"https://x/{e}.xml")
            for i, (e, r, p, x) in enumerate(zip(ENDS, REV, PAT, eps, strict=True)) if i not in drop]  # fmt: skip


SHEETS = [P.BalanceSheet(date(2025, 3, 31), Decimal(400), Decimal(450), True, "bs25"),
          P.BalanceSheet(date(2026, 3, 31), Decimal(480), Decimal(530), True, "bs26")]  # fmt: skip


def v(m):
    return None if m.value is None else float(m.value)


def test_ttm_eps_and_pe():
    eps, pe = P.pe_metric(Decimal(520), quarters())
    assert v(eps) == 26  # 5 + 6 + 7 + 8
    assert v(pe) == 20  # 520 / 26
    assert "30 Jun 2026" in pe.period and "consolidated" in eps.period


def test_negative_ttm_eps_is_not_meaningful_never_zero():
    eps, pe = P.pe_metric(Decimal(520), quarters(eps=[1, 1, 1, 1, 5, 6, 7, -30]))
    assert v(eps) == -12  # 5 + 6 + 7 - 30
    assert pe.value is None and pe.reason.startswith("n/m")


def test_missing_eps_and_gaps_give_reasons():
    _, pe = P.pe_metric(Decimal(520), quarters(drop=(5,)))  # Dec-2025 missing inside the window
    assert pe.value is None and "not consecutive" in pe.reason
    _, pe = P.pe_metric(Decimal(520), quarters()[:3])
    assert pe.value is None and "fewer than four" in pe.reason
    eps = list(EPS)
    eps[7] = None
    _, pe = P.pe_metric(Decimal(520), [P.Quarter(q.end, q.revenue, q.profit, e and Decimal(e), True)
                                        for q, e in zip(quarters(), eps, strict=True)])  # fmt: skip
    assert pe.value is None and "no EPS" in pe.reason


def test_basis_is_the_latest_quarters_and_never_mixed():
    qs = quarters()[:-1] + [P.Quarter(ENDS[-1], Decimal(1), Decimal(1), Decimal(1), False)]
    _, pe = P.pe_metric(Decimal(520), qs)  # latest is standalone: only one standalone quarter on file
    assert pe.value is None and "standalone" in pe.reason


def test_pb_roe_growth_margin():
    qs = quarters()
    assert v(P.pb_metric(Decimal(5200), SHEETS, True)) == pytest.approx(5200 / 480)
    roe = P.roe_metric(qs, SHEETS, True)  # TTM to Mar-2026: 10 + 12 + 12 + 14 = 48; average equity (400 + 480) / 2
    assert v(roe) == pytest.approx(48 / 440)
    assert v(P.growth_metric(qs, "revenue")) == pytest.approx(460 / 400 - 1)  # 110+110+120+120 vs 4 x 100
    assert v(P.growth_metric(qs, "profit")) == pytest.approx(52 / 40 - 1)  # 12+12+14+14 vs 4 x 10
    assert v(P.margin_metric(qs)) == pytest.approx(52 / 460)


def test_roe_needs_opening_equity_and_pb_needs_positive_book():
    assert P.roe_metric(quarters(), SHEETS[1:], True).value is None
    neg = [P.BalanceSheet(date(2026, 3, 31), Decimal(-5), None, True)]
    assert P.pb_metric(Decimal(5200), neg, True).reason.startswith("n/m")


def test_growth_off_a_loss_is_not_meaningful():
    qs = [P.Quarter(q.end, q.revenue, Decimal(-5) if i < 4 else q.profit, q.eps, True)
          for i, q in enumerate(quarters())]  # fmt: skip
    m = P.growth_metric(qs, "profit")
    assert m.value is None and m.reason.startswith("n/m")


def test_one_year_return_adjusts_for_a_bonus():
    bars = [(date(2025, 10, 3), 200), (date(2025, 10, 6), 205)]  # 5-Oct-2025 was a Sunday: Friday's close counts
    m = P.return_1y(Decimal(120), date(2026, 10, 5), bars, [(date(2026, 1, 15), "Bonus 1:1")])
    assert v(m) == pytest.approx(120 / (200 / 2) - 1)  # 1:1 bonus halves the old close: 0.20
    assert "x2" in m.basis
    assert P.return_1y(Decimal(120), date(2026, 10, 5), [], []).value is None


def _row(sym, **vals):
    return {"symbol": sym, **{k: {"value": x} for k, x in vals.items()}}


def test_summary_quartiles_and_percentile_skip_missing():
    peers = [_row("A", pe=10), _row("B", pe=20), _row("C", pe=30), _row("D", pe=40), _row("E", pe=None)]
    s = P.summarise({"pe": {"value": 25}}, peers)["pe"]
    # 10, 20, 30, 40: Q1 at position 0.75 -> 17.5, median 25, Q3 at 2.25 -> 32.5; 2 of 4 below 25 -> 50
    assert (s["n"], s["q1"], s["median"], s["q3"], s["percentile"]) == (4, 17.5, 25, 32.5, 50)
    assert P.summarise({"pe": {"value": 20}}, peers)["pe"]["percentile"] == 37.5  # (1 below + 1 tie / 2) / 4
    few = P.summarise({}, peers[:2])["roe"]
    assert few["median"] is None and few["reason"]


def test_summary_keeps_negative_growth_but_drops_non_positive_multiples():
    peers = [_row("A", pe=-5, roe=-0.1), _row("B", pe=10, roe=0.1), _row("C", pe=20, roe=0.2), _row("D", pe=30, roe=0.3)]
    s = P.summarise({}, peers)
    assert s["pe"]["n"] == 3 and s["pe"]["median"] == 20
    assert s["roe"]["n"] == 4 and s["roe"]["median"] == pytest.approx(0.15)


def test_finest_level_with_five_peers_and_fallback():
    own = {"basic_industry": "Cement", "industry": "Cement & Products", "sector": "Construction Materials",
           "macro": "Commodities"}  # fmt: skip
    uni = [{"symbol": f"S{i}", "basic_industry": "Cement" if i < 3 else "Other", "industry": "Cement & Products",
            "sector": "Construction Materials", "macro": "Commodities"} for i in range(7)]  # fmt: skip
    lvl, peers = P.choose_level(own, [*uni, {"symbol": "ME", **own}], "ME")
    assert lvl == "industry" and len(peers) == 7  # 3 cement peers < 5, so one level coarser; the company excluded
    lvl, peers = P.choose_level(own, uni[:2], "ME")
    assert lvl == "basic_industry" and len(peers) == 2  # nothing reaches 5: the level with the most peers
    assert P.choose_level({}, uni, "ME") == (None, [])


def test_nearest_by_market_cap_on_a_log_scale():
    peers = [{"symbol": s, "market_cap": m} for s, m in (("A", 50), ("B", 200), ("C", 400), ("D", 90), ("E", None))]
    got = [p["symbol"] for p in P.nearest_by_mcap(peers, 100, cap=4)]
    assert got == ["D", "A", "B", "C"]  # |ln .9| < |ln .5| = |ln 2| (tie: by symbol) < ln 4; no market cap last


# --------------------------------------------------------------------------- API (stored peers, live company row)
def _stored(sym, mcap, pe, basic="Cement", industry="Cement & Products"):
    metrics = {m: {"value": None, "reason": "not in this test"} for m in P.METRICS}
    metrics["market_cap"] = {"value": mcap}
    metrics["pe"] = {"value": pe}
    return {"symbol": sym, "name": f"{sym} Ltd", "basic_industry": basic, "industry": industry,
            "sector": "Construction Materials", "macro": "Commodities", "metrics": metrics, "inputs": {"x": 1}}  # fmt: skip


def test_peers_api_reads_the_store_and_fetches_only_the_company(env, monkeypatch):
    from fastapi.testclient import TestClient

    from finresearch.api import create_app
    from finresearch.signals import stock_peers as SP

    calls: list[str] = []

    async def live_row(eq, symbol, today):
        calls.append(symbol)
        return _stored(symbol, 100.0, 25.0)

    monkeypatch.setattr(SP, "live_row", live_row)
    with TestClient(create_app()) as c:
        assert c.get("/api/stocks/EXAMPLE/peers").json()["status"] == "not_computed"
        rows = {s: _stored(s, m, pe, basic="Cement" if i < 2 else "Other")
                for i, (s, m, pe) in enumerate((("PA", 90.0, 10.0), ("PB", 400.0, 20.0), ("PC", 50.0, 30.0),
                                                ("PD", 200.0, 40.0), ("PE", 1000.0, None), ("EXAMPLE", 1.0, 1.0)))}  # fmt: skip
        SP.save({"version": 1, "as_of": "2026-10-05", "generated_at": "2026-10-05T01:00:00+00:00",
                 "universe": "NIFTY 500", "universe_source": SP.UNIVERSE_URL, "rows": rows, "failed": {}})  # fmt: skip
        r = c.get("/api/stocks/EXAMPLE/peers").json()
    assert calls == ["EXAMPLE"]  # the company only; peers come from the store
    assert r["status"] == "ok" and r["level"] == "industry"  # 2 basic-industry peers < 5; 5 share the industry
    assert r["industry"] == "Cement & Products" and r["candidates"] == 5
    assert r["company"]["metrics"]["market_cap"]["value"] == 100.0  # the live row, not the stored 1.0
    # |ln(m / 100)|: PA .105, PC = PD = ln 2 (.693, by symbol), PB ln 4, PE ln 10
    assert [p["symbol"] for p in r["peers"]] == ["PA", "PC", "PD", "PB", "PE"]
    assert all("inputs" not in p for p in r["peers"]) and "inputs" not in r["company"]
    s = r["summary"]["pe"]  # 10, 20, 30, 40 (PE has none): median 25, own 25 -> 2 below + 0 ties of 4 = 50
    assert (s["n"], s["median"], s["percentile"]) == (4, 25.0, 50.0)


# --------------------------------------------------------------------------- the nightly build's per-stock step
class FakeEq:
    def __init__(self, fail=False):
        import json
        from pathlib import Path
        from types import SimpleNamespace

        from finresearch.adapters.nse import Quote

        raw = json.loads((Path(__file__).parent / "fixtures/nse/quote_INFY_20260928.json").read_text())
        self.calls: list[str] = []

        async def quote(symbol):
            self.calls.append("quote")
            if fail:
                raise RuntimeError("blocked")
            return Quote.parse(raw)

        self.nse = SimpleNamespace(quote=quote)

    async def history(self, symbol, start, end, *, cache_ttl=None):
        from types import SimpleNamespace

        self.calls.append("history")
        return [SimpleNamespace(day=date(2025, 9, 26), close=Decimal("800"))]

    async def corporate_actions(self, symbol):
        from types import SimpleNamespace

        self.calls.append("actions")
        return [SimpleNamespace(ex_date=date(2026, 5, 2), subject="Dividend - Rs 20 Per Share")]


@pytest.fixture
def fake_results(monkeypatch):
    from finresearch.api import markets

    calls = []

    async def results_from_nse(eq, sym, n, *, balance_sheets=None):
        calls.append(sym)
        for s in SHEETS:
            balance_sheets[s.end] = {"equity_owners": s.equity_owners, "total_equity": s.total_equity,
                                     "consolidated": True, "xbrl": s.source}  # fmt: skip
        return {"quarters": [{"period_end": q.end.isoformat(), "revenue": float(q.revenue), "profit": float(q.profit),
                              "eps": float(q.eps), "consolidated": True, "xbrl": q.source} for q in quarters()]}  # fmt: skip

    monkeypatch.setattr(markets, "results_from_nse", results_from_nse)
    return calls


async def test_row_from_live_inputs_and_weekly_results_reuse(fake_results):
    from finresearch.signals import stock_peers as SP

    today = date(2026, 9, 28)
    eq = FakeEq()
    row = SP.compute_row("INFY", await SP.fetch_inputs(eq, "INFY", today), today)
    m = row["metrics"]
    assert row["basic_industry"] == "Computers - Software & Consulting" and row["industry"] == "IT - Software"
    assert m["price"]["value"] == pytest.approx(1003.2)
    assert m["market_cap"]["value"] == pytest.approx(4058232462 * 1003.2)
    assert m["pe"]["value"] == pytest.approx(1003.2 / 26)  # TTM EPS 5 + 6 + 7 + 8
    assert m["pb"]["value"] == pytest.approx(4058232462 * 1003.2 / 480)
    assert m["return_1y"]["value"] == pytest.approx(1003.2 / 800 - 1)  # a dividend is not a split: no adjustment
    assert eq.calls == ["quote", "actions", "history"] and fake_results == ["INFY"]
    eq2 = FakeEq()
    again = SP.compute_row("INFY", await SP.fetch_inputs(eq2, "INFY", date(2026, 10, 2), row), date(2026, 10, 2))
    assert fake_results == ["INFY"] and eq2.calls == ["quote", "history"]  # results 4 days old: reused
    assert again["metrics"]["pe"]["value"] == m["pe"]["value"]


async def test_build_keeps_a_failed_stocks_previous_row_marked_stale(env, fake_results, monkeypatch):
    from finresearch.signals import stock_peers as SP

    monkeypatch.setattr(SP, "PAUSE_S", 0)
    today = date(2026, 9, 28)
    data = await SP.build(FakeEq(), today, ["INFY", "OTHER"])
    SP.save(data)
    assert set(data["rows"]) == {"INFY", "OTHER"} and not data["failed"]
    with pytest.raises(RuntimeError):  # most of the universe failing raises: the caller keeps the old store
        await SP.build(FakeEq(fail=True), today, ["INFY", "OTHER"])

    class OneFails(FakeEq):
        async def history(self, symbol, start, end, *, cache_ttl=None):
            if symbol == "OTHER":
                raise RuntimeError("blocked")
            return await super().history(symbol, start, end)

    again = await SP.build(OneFails(), today, ["INFY", "OTHER", "THIRD"])
    assert again["failed"].keys() == {"OTHER"} and again["rows"]["OTHER"]["stale"] is True
    assert "stale" not in again["rows"]["INFY"] and "THIRD" in again["rows"]

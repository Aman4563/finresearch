"""Unsupported corporate actions (#237): a demerger, rights issue, merger, ISIN change, buyback, capital reduction or
consolidation on a held stock makes its lots and the later tax years incomplete until the user resolves it.

Synthetic NSE corporate-action rows only (made-up symbol and ISIN)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal as D

import pytest

ISIN = "INE000D01019"
URL = "https://www.nseindia.com/api/corporates-corporateActions?index=equities&symbol=EXDEMO"
EX = date(2024, 6, 3)
REASON = "unsupported corporate action: demerger on 2024-06-03 — cost split not modelled; enter the cost allocation manually"


@pytest.mark.parametrize(
    ("subject", "kind"),
    [
        ("Demerger", "demerger"),
        ("Scheme Of Arrangement - Demerger Of Example Retail Business", "demerger"),
        ("Rights 1:5 @ Premium Rs 100/-", "rights"),
        ("Scheme Of Amalgamation", "merger"),
        ("Merger", "merger"),
        ("Scheme Of Arrangement", "scheme_of_arrangement"),
        ("Change In ISIN", "isin_change"),
        ("Buy Back", "buyback"),
        ("Buyback Of Shares", "buyback"),
        ("Capital Reduction", "capital_reduction"),
        ("Reduction Of Capital", "capital_reduction"),
        ("Face Value Consolidation From Rs 1/- Per Share To Rs 10/- Per Share", "consolidation"),
        ("Consolidation Of Shares", "consolidation"),
        ("Dividend - Rs 5 Per Share", None),
        ("Bonus 1:1", None),  # modelled: a nil-cost lot
        ("Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 2/- Per Share", None),  # modelled
        ("Annual General Meeting", None),
        ("Interest Payment", None),
    ],
)
def test_classify_action(subject, kind):
    from finresearch.portfolio.service import classify_action, parse_action

    assert classify_action(subject) == kind
    if kind == "consolidation":
        assert parse_action(subject) == []  # never re-denominated as if it were a split


@pytest.fixture
def db(env):
    from sqlalchemy import text

    from finresearch.db import session_scope

    with session_scope() as s:
        s.execute(text("TRUNCATE portfolio_disposal, portfolio_lot, portfolio_txn, portfolio_holding, portfolio_import, "
                       "portfolio_snapshot, portfolio_setting CASCADE"))  # fmt: skip
    yield session_scope


def _txn(s, day, kind, q, price):
    from finresearch.portfolio.service import manual_txn

    return manual_txn(s, {"asset_type": "stock", "name": "Example Demerge Ltd", "account": "Manual", "isin": ISIN,
                          "nse_symbol": "EXDEMO", "day": day, "kind": kind, "quantity": D(q), "price": D(price)})  # fmt: skip


def _fy(view, fy):
    return next(f for f in view["fys"] if f["fy"] == fy)


def test_demerger_marks_the_holding_and_later_tax_years_incomplete_until_resolved(db):
    from finresearch.db.models import PortfolioHolding
    from finresearch.portfolio.report import snapshot, tax_view
    from finresearch.portfolio.service import record_unsupported, resolve_action

    today = date(2026, 9, 30)
    with db() as s:
        hid = _txn(s, date(2023, 1, 2), "buy", 100, 100).holding_id
        _txn(s, date(2024, 3, 1), "sell", 10, 150)  # FY 2023-24: before the ex-date
        _txn(s, date(2024, 9, 2), "sell", 10, 200)  # FY 2024-25: after it
        h = s.get(PortfolioHolding, hid)
        acts = [
            (EX, "Demerger"),
            (date(2024, 8, 1), "Dividend - Rs 5 Per Share"),
            (EX, "Demerger"),
        ]  # NSE repeats
        assert record_unsupported(s, h, acts, source_url=URL) == ["demerger 2024-06-03"]
        assert record_unsupported(s, h, acts, source_url=URL) == []  # once
        (p,) = h.meta["pending_actions"]
        assert (p["type"], p["ex_date"], p["subject"], p["source_url"], p["status"]) == (
            "demerger", "2024-06-03", "Demerger", URL, "pending")  # fmt: skip
        view = tax_view(s, {}, today, D("0.30"))
        later, earlier = _fy(view, 2025), _fy(view, 2024)
        assert (
            later["complete"] is False
            and later["tax"] is None
            and later["unclassified"]["corporate_action"] == 1
        )
        assert REASON in later["unclassified"]["detail"]
        assert earlier["complete"] is True and earlier["tax"] is not None
        (d,) = [x for x in view["disposals"] if x["sold"] == "2024-09-02"]
        assert d["term"] == "unknown" and REASON in d["notes"]
        (row,) = snapshot(s, {}, today)["holdings"]
        assert (
            row["pending_actions"][0]["reason"] == REASON
            and row["cost_known"] is False
            and row["cost"] is None
        )
        assert REASON in row["warnings"]
        # resolution: a note; the action never comes back on the next sync
        resolve_action(s, hid, p["key"], "cost split per the company's demerger circular, entered by hand")
        assert _fy(tax_view(s, {}, today, D("0.30")), 2025)["complete"] is True
        (row,) = snapshot(s, {}, today)["holdings"]
        assert row["pending_actions"] == [] and row["cost_known"] is True and REASON not in row["warnings"]
        assert h.meta["pending_actions"][0]["status"] == "resolved"
        assert record_unsupported(s, h, acts, source_url=URL) == []


def test_unsupported_action_makes_the_holding_xirr_incomplete_not_a_number(db):
    """#263: a demerged holding's cash flows miss the new company's shares, so its XIRR (and its share of the overall
    XIRR) would read as a loss that never happened. It is None with the reason, and left out of the overall XIRR."""
    from finresearch.db.models import PortfolioHolding
    from finresearch.portfolio.report import snapshot
    from finresearch.portfolio.service import manual_txn, record_unsupported
    from finresearch.portfolio.valuation import PriceInfo

    today = date(2026, 9, 30)
    with db() as s:
        hid = _txn(s, date(2023, 1, 2), "buy", 100, 100).holding_id
        other = manual_txn(s, {"asset_type": "stock", "name": "Example Plain Ltd", "account": "Manual",
                               "isin": "INE000F01011", "nse_symbol": "EXPLAIN", "day": date(2023, 1, 2), "kind": "buy",
                               "quantity": D(10), "price": D(100)}).holding_id  # fmt: skip
        prices = {hid: PriceInfo(price=D(60)), other: PriceInfo(price=D(121))}
        before = {r["id"]: r for r in snapshot(s, prices, today)["holdings"]}
        assert before[hid]["xirr"] is not None  # no action recorded yet: a plain XIRR
        record_unsupported(s, s.get(PortfolioHolding, hid), [(EX, "Demerger")], source_url=URL)
        snap = snapshot(s, prices, today)
        rows = {r["id"]: r for r in snap["holdings"]}
        assert rows[hid]["xirr"] is None and rows[hid]["xirr_reason"] == REASON
        assert rows[other]["xirr"] is not None
        # the overall XIRR is the plain holding's alone: 1,000 on 2-Jan-2023 grew to 1,210 on 30-Sep-2026
        # (1,367 days): 1.21 ** (365 / 1367) - 1 = 0.05221...
        assert abs(snap["summary"]["xirr"] - (1.21 ** (365 / 1367) - 1)) < 1e-4
        assert "1 holding(s)" in snap["summary"]["xirr_reason"]
        assert "unsupported corporate action" in snap["summary"]["xirr_reason"]


def test_action_not_held_on_its_ex_date_is_ignored(db):
    from finresearch.db.models import PortfolioHolding
    from finresearch.portfolio.service import record_unsupported

    with db() as s:
        hid = _txn(s, date(2023, 1, 2), "buy", 10, 100).holding_id
        _txn(s, date(2023, 6, 1), "sell", 10, 120)  # sold out before the demerger
        h = s.get(PortfolioHolding, hid)
        assert (
            record_unsupported(s, h, [(EX, "Demerger"), (date(2022, 1, 3), "Rights 1:5")], source_url=URL)
            == []
        )
        assert not (h.meta or {}).get("pending_actions")


def test_resolve_needs_a_note_and_a_known_action(db):
    from finresearch.db.models import PortfolioHolding
    from finresearch.portfolio.service import record_unsupported, resolve_action

    with db() as s:
        hid = _txn(s, date(2023, 1, 2), "buy", 10, 100).holding_id
        record_unsupported(s, s.get(PortfolioHolding, hid), [(EX, "Demerger")], source_url=URL)
        with pytest.raises(ValueError, match="note"):
            resolve_action(s, hid, "demerger:2024-06-03", "  ")
        with pytest.raises(LookupError):
            resolve_action(s, hid, "merger:2024-06-03", "a note")


def test_reconciliation_mismatch_names_the_pending_action(db):
    from finresearch.db.models import PortfolioHolding
    from finresearch.portfolio.connectors.base import BrokerHolding
    from finresearch.portfolio.connectors.merge import reconcile_snapshot
    from finresearch.portfolio.service import record_unsupported

    with db() as s:
        hid = _txn(s, date(2023, 1, 2), "buy", 10, 100).holding_id
        record_unsupported(s, s.get(PortfolioHolding, hid), [(EX, "Demerger")], source_url=URL)
        (r,) = reconcile_snapshot(s, account="Manual", holdings=[BrokerHolding(name="EXDEMO", quantity=D(12),
                                                                                isin=ISIN, symbol="EXDEMO")])  # fmt: skip
        assert r["status"] == "differs" and r["pending_action"] == REASON


# --------------------------------------------------------------------------- API, data health and the brief
class FakeEquity:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def corporate_actions(self, symbol):
        from finresearch.adapters.nse_equity import CorporateAction

        if symbol == "EXFAIL":
            raise RuntimeError("HTTP 403")

        rows = [{"symbol": symbol, "subject": "Demerger", "exDate": "03-Jun-2024", "recDate": "03-Jun-2024"},
                {"symbol": symbol, "subject": "Dividend - Rs 5 Per Share", "exDate": "01-Aug-2024", "recDate": ""}]  # fmt: skip
        return [CorporateAction.parse(r) for r in rows]


@pytest.fixture
def client(db):
    from fastapi.testclient import TestClient

    from finresearch.api import create_app
    from finresearch.api.markets import MarketSources

    async def quote(symbol):
        raise LookupError(symbol)

    app = create_app()
    app.state.markets = MarketSources(
        quote=quote, equity=lambda: FakeEquity(), today=lambda: date(2026, 9, 30)
    )
    with TestClient(app) as c:
        yield c


H = {"Origin": "http://127.0.0.1:3000", "X-FinResearch": "1"}


def test_sync_records_pending_action_and_manual_resolution(client):
    body = {"asset_type": "stock", "name": "Example Demerge Ltd", "nse_symbol": "EXDEMO", "isin": ISIN,
            "account": "Manual", "day": "2023-01-02", "kind": "buy", "quantity": "100", "price": "100"}  # fmt: skip
    hid = client.post("/api/portfolio/transactions", headers=H, json=body).json()["holding_id"]
    client.post("/api/portfolio/transactions", headers=H, json={**body, "day": "2024-09-02", "kind": "sell",
                                                                "quantity": "10", "price": "200"})  # fmt: skip
    sync = client.post("/api/portfolio/actions/sync", headers=H).json()
    assert sync["added"] == {} and sync["pending"] == {"Example Demerge Ltd": ["demerger 2024-06-03"]}
    detail = client.get(f"/api/portfolio/holdings/{hid}").json()
    (p,) = detail["pending_actions"]
    assert p["reason"] == REASON and p["source_url"].endswith("symbol=EXDEMO")
    assert _fy(client.get("/api/portfolio/tax").json(), 2025)["complete"] is False
    from finresearch.db import session_scope
    from finresearch.monitor.digest import build_brief

    with session_scope() as s:
        brief = build_brief(s, datetime(2026, 9, 30, 3, 5, tzinfo=UTC))
    assert any(REASON in x["text"] for x in brief["health"])
    # resolve through a manual transaction that says so (the cost allocation entered by hand)
    r = client.post("/api/portfolio/transactions", headers=H,
                    json={**body, "day": "2024-06-03", "kind": "opening", "name": "Example Demerged Co",
                          "nse_symbol": "EXNEWCO", "isin": "INE000E01016", "quantity": "100", "price": "30",
                          "meta": {"acquired": "2023-01-02", "resolves_action": f"{hid}:{p['key']}"}})  # fmt: skip
    assert r.status_code == 201
    detail = client.get(f"/api/portfolio/holdings/{hid}").json()
    assert detail["pending_actions"][0]["status"] == "resolved"
    assert "manual transaction" in detail["pending_actions"][0]["note"]
    assert _fy(client.get("/api/portfolio/tax").json(), 2025)["complete"] is True


def test_resolve_endpoint(client):
    body = {"asset_type": "stock", "name": "Example Demerge Ltd", "nse_symbol": "EXDEMO", "isin": ISIN,
            "account": "Manual", "day": "2023-01-02", "kind": "buy", "quantity": "100", "price": "100"}  # fmt: skip
    hid = client.post("/api/portfolio/transactions", headers=H, json=body).json()["holding_id"]
    client.post("/api/portfolio/actions/sync", headers=H)
    url = f"/api/portfolio/holdings/{hid}/actions/resolve"
    assert client.post(url, headers=H, json={"key": "demerger:2024-06-03", "note": ""}).status_code == 422
    assert client.post(url, headers=H, json={"key": "nope", "note": "x y z"}).status_code == 404
    ok = client.post(url, headers=H, json={"key": "demerger:2024-06-03", "note": "no cost change: checked"})
    assert ok.status_code == 200 and ok.json()["pending_actions"][0]["status"] == "resolved"


def test_data_health_row(db):
    from finresearch.portfolio import health

    today = date(2026, 9, 30)
    rows = [{"id": 1, "asset_type": "stock", "closed": False, "pending_actions": [{"reason": REASON}],
             "actions_synced": "2026-09-29"},
            {"id": 2, "asset_type": "stock", "closed": False, "pending_actions": [], "actions_synced": "2026-09-29"},
            {"id": 3, "asset_type": "mf", "closed": False}]  # fmt: skip
    r = health.corporate_actions(rows, today)
    assert (r["coverage_pct"], r["status"]) == (50.0, "partial") and REASON in r["detail"]
    assert health.corporate_actions(rows[1:], today)["status"] == "ok"
    assert health.corporate_actions(rows[2:], today)["status"] == "not_applicable"
    assert sum(health.WEIGHTS.values()) == 100


def test_data_health_row_is_not_ok_when_actions_were_never_synced_or_the_sync_is_stale():
    """#264: before any "Sync corporate actions" nothing is recorded, which read as "none recorded": ok, 100 %."""
    from finresearch.portfolio import health

    today = date(2026, 9, 30)
    never = [{"id": 1, "name": "Example A", "asset_type": "stock", "closed": False, "pending_actions": []},
             {"id": 2, "name": "Example B", "asset_type": "stock", "closed": False, "pending_actions": []}]  # fmt: skip
    r = health.corporate_actions(never, today)
    assert (r["coverage_pct"], r["status"]) == (None, "unknown")
    assert "2 of 2 stock holding(s) never synced" in r["detail"]
    assert r["href"] == "/portfolio#import"
    # one synced 30 days ago (the limit: still current), one 31 days ago: half covered
    edge = [{**never[0], "actions_synced": "2026-08-31"}, {**never[1], "actions_synced": "2026-08-30"}]
    r = health.corporate_actions(edge, today)
    assert (r["coverage_pct"], r["status"]) == (50.0, "partial")
    assert f"more than {health.ACTIONS_SYNC_MAX_DAYS} days ago (oldest 2026-08-30)" in r["detail"]
    # one synced today, one bought after the last sync (never synced): half covered, never ok
    mixed = [{**never[0], "actions_synced": "2026-09-30"}, never[1]]
    assert health.corporate_actions(mixed, today)["coverage_pct"] == 50.0
    assert health.corporate_actions([mixed[0]], today)["status"] == "ok"


def test_sync_stamps_only_the_stocks_it_read(client):
    """A successful sync records the day on the holding (the health row's input); a failed fetch does not."""
    body = {"asset_type": "stock", "name": "Example Demerge Ltd", "nse_symbol": "EXDEMO", "isin": ISIN,
            "account": "Manual", "day": "2023-01-02", "kind": "buy", "quantity": "100", "price": "100"}  # fmt: skip
    ok = client.post("/api/portfolio/transactions", headers=H, json=body).json()["holding_id"]
    bad = client.post("/api/portfolio/transactions", headers=H,
                      json={**body, "name": "Example Fails Ltd", "nse_symbol": "EXFAIL",
                            "isin": "INE000G01019"}).json()["holding_id"]  # fmt: skip
    sync = client.post("/api/portfolio/actions/sync", headers=H).json()
    assert sync["errors"] == ["NSE EXFAIL: RuntimeError"]
    from finresearch.db import session_scope
    from finresearch.db.models import PortfolioHolding

    with session_scope() as s:
        assert s.get(PortfolioHolding, ok).meta["actions_synced"] == "2026-09-30"
        assert "actions_synced" not in (s.get(PortfolioHolding, bad).meta or {})

"""Decision journal for every trade, pre-trade checklist and behaviour report (feature #5) through the API, on
synthetic data only (Example Ltd names, test database). Expected values are computed by hand in the comments."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from decimal import Decimal as D
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

TODAY = date(2026, 9, 30)
ORIGIN = {"Origin": "http://127.0.0.1:3000", "X-FinResearch": "1"}
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def client(env):
    from sqlalchemy import text

    from finresearch.api import create_app
    from finresearch.db import session_scope

    with session_scope() as s:  # the test database is shared by the session: start every test empty
        s.execute(text("TRUNCATE trade_note, alert, portfolio_disposal, portfolio_lot, portfolio_txn, "
                       "portfolio_holding, portfolio_import, portfolio_snapshot, portfolio_setting, investor_profile "
                       "CASCADE"))  # fmt: skip
    app = create_app()
    app.state.journal_today = TODAY

    async def no_signal(asset, code):
        raise LookupError("offline test")

    app.state.journal_signal = no_signal
    with TestClient(app) as c:
        c.app_ = app
        yield c


def txn(c, **kw) -> dict:
    body = {"asset_type": "stock", "account": "Manual", **kw}
    r = c.post("/api/portfolio/transactions", headers=ORIGIN, json=body)
    assert r.status_code == 201, r.text
    return r.json()


def notes(c, **params) -> list[dict]:
    r = c.get("/api/journal/notes", params=params)
    assert r.status_code == 200, r.text
    return r.json()["notes"]


# --------------------------------------------------------------------------- drafts
def test_drafts_one_per_holding_side_and_day_and_idempotent(client):
    a = txn(
        client,
        name="Example Textiles",
        nse_symbol="EXMPL",
        day="2026-09-25",
        kind="buy",
        quantity="4",
        price="100",
    )
    txn(
        client, holding_id=a["holding_id"], day="2026-09-25", kind="buy", quantity="6", price="110"
    )  # partial fill
    txn(
        client,
        name="Example Bank",
        nse_symbol="EXBANK",
        day="2026-06-01",
        kind="buy",
        quantity="5",
        price="400",
    )  # old
    from finresearch.db import session_scope
    from finresearch.db.models import PortfolioTxn

    with session_scope() as s:  # an SIP instalment from a CAS: not a decision of that day
        s.add(PortfolioTxn(holding_id=a["holding_id"], day=date(2026, 9, 26), kind="buy", quantity=D(1), price=D(100),
                           amount=D(100), source="cas", dedupe_key="test-sip", meta={"cas_type_txn": "PURCHASE_SIP"}))  # fmt: skip
    got = notes(client)
    assert len(got) == 1
    n = got[0]
    assert (n["status"], n["source"], n["side"], n["trade_day"], n["instrument"]) == (
        "draft",
        "auto",
        "buy",
        "2026-09-25",
        "NSE:EXMPL",
    )
    assert n["quantity"] == 10 and n["price"] == pytest.approx((400 + 660) / 10)  # 106 average
    assert len(n["txn_ids"]) == 2
    assert notes(client) == got  # a second look adds nothing
    # a later partial fill imported on another pass joins the same entry
    txn(client, holding_id=a["holding_id"], day="2026-09-25", kind="buy", quantity="10", price="100")
    got = notes(client)
    assert len(got) == 1 and len(got[0]["txn_ids"]) == 3 and got[0]["quantity"] == 20


def test_thesis_turns_a_draft_active_and_verdict_reviews_it(client):
    from finresearch.db import session_scope
    from finresearch.portfolio import cache

    a = txn(
        client,
        name="Example Textiles",
        nse_symbol="EXMPL",
        day="2026-09-20",
        kind="buy",
        quantity="10",
        price="100",
    )
    nid = notes(client)[0]["id"]
    r = client.patch(f"/api/journal/notes/{nid}", headers=ORIGIN,
                     json={"thesis": "  Margins recover as cotton prices fall ", "invalidation": "two weak quarters",
                           "confidence_pct": 60, "expected_holding_days": 365, "review_on": "2026-12-31"})  # fmt: skip
    assert r.status_code == 200 and r.json()["status"] == "active"
    assert r.json()["thesis"] == "Margins recover as cotton prices fall"
    with session_scope() as s:
        cache.write(s, cache.VALUATION, {"day": "2026-09-30", "value": 1200.0,
                                         "holdings": {str(a["holding_id"]): {"price": 120.0, "value": 1200.0,
                                                                             "price_as_of": "2026-09-30"}}})  # fmt: skip
    r = client.patch(f"/api/journal/notes/{nid}", headers=ORIGIN, json={"outcome_verdict": "too_early"})
    out = r.json()
    assert out["status"] == "reviewed" and out["outcome"]["return_pct"] == 20.0  # 120 / 100 - 1
    assert out["outcome"]["days_since_trade"] == 10
    assert (
        client.patch(f"/api/journal/notes/{nid}", headers=ORIGIN, json={"confidence_pct": 101}).status_code
        == 422
    )


# --------------------------------------------------------------------------- planned trades
def test_planned_trade_becomes_the_entry_of_the_trade_that_follows(client):
    a = txn(
        client,
        name="Example Textiles",
        nse_symbol="EXMPL",
        day="2025-10-15",
        kind="buy",
        quantity="10",
        price="100",
    )
    r = client.post("/api/journal/notes", headers=ORIGIN,
                    json={"side": "sell", "status": "planned", "holding_id": a["holding_id"], "trade_day": "2026-09-22",
                          "quantity": "4", "price": "150", "thesis": "Valuation now full", "checklist": {"status": "ok"}})  # fmt: skip
    assert r.status_code == 201 and r.json()["source"] == "pretrade"
    pid = r.json()["id"]
    notes(client)  # the cursor moves past the old buy (outside the 30-day window: no draft)
    txn(client, holding_id=a["holding_id"], day="2026-09-26", kind="sell", quantity="4", price="152")
    got = notes(client)
    assert [n["id"] for n in got] == [pid]
    n = got[0]
    assert (
        n["status"] == "active"
        and n["trade_day"] == "2026-09-26"
        and n["price"] == 152
        and len(n["txn_ids"]) == 1
    )
    assert client.post("/api/journal/notes", headers=ORIGIN, json={"side": "buy"}).status_code == 422


# --------------------------------------------------------------------------- reminders
def test_review_reminder_once_without_the_thesis(client):
    from sqlalchemy import select

    from finresearch.db import session_scope
    from finresearch.db.models import Alert
    from finresearch.portfolio.journal import review_step

    r = client.post("/api/journal/notes", headers=ORIGIN,
                    json={"side": "buy", "name": "Example Pharma", "thesis": "SECRET-THESIS new plant",
                          "invalidation": "SECRET-EXIT", "review_on": "2026-09-30", "trade_day": "2026-03-01"})  # fmt: skip
    nid = r.json()["id"]
    now = datetime(
        2026, 9, 29, 19, 0, tzinfo=UTC
    )  # 00:30 IST on 30-Sep: the review date has arrived in India
    with session_scope() as s:
        assert review_step(s, now)["reminders"] == 1
    with session_scope() as s:
        assert review_step(s, now)["reminders"] == 0  # once
        alerts = s.scalars(select(Alert).where(Alert.kind == "journal_review")).all()
        assert len(alerts) == 1
        a = alerts[0]
        assert "Example Pharma" in a.message and "SECRET" not in a.message and a.data["path"] == "/journal"
    assert client.get("/api/journal/notes").json()["due"] == [nid]
    # a new review date re-arms the reminder; before it arrives (IST) nothing fires
    client.patch(f"/api/journal/notes/{nid}", headers=ORIGIN, json={"review_on": "2026-10-01"})
    with session_scope() as s:
        assert review_step(s, now)["reminders"] == 0
        assert review_step(s, datetime(2026, 9, 30, 18, 31, tzinfo=UTC))["reminders"] == 1  # 00:01 IST 1-Oct


# --------------------------------------------------------------------------- pre-trade checklist
def _seed_valuation(ex_id: int, bank_id: int) -> None:
    from finresearch.db import session_scope
    from finresearch.portfolio import cache

    with session_scope() as s:
        cache.write(s, cache.VALUATION, {"day": "2026-09-30", "value": 10000.0, "holdings": {
            str(ex_id): {"value": 1500.0, "price": 150.0, "sector": "Textiles"},
            str(bank_id): {"value": 8500.0, "price": 425.0, "sector": "Banks"}}})  # fmt: skip
        cache.write(s, cache.SIGNALS, {"items": {"stock:EXMPL": {
            "action": "REDUCE", "probability": 0.42, "probability_interval": [0.31, 0.54], "event": "beats NIFTY",
            "horizon": "12 months", "validation": "backtested", "n": 83, "day": "2026-09-30"}}})  # fmt: skip


def by_key(out: dict) -> dict:
    return {i["key"]: i for i in out["items"]}


def test_checklist_sale_tax_long_term_soon_and_block(client):
    ex = txn(
        client,
        name="Example Textiles",
        nse_symbol="EXMPL",
        day="2025-10-15",
        kind="buy",
        quantity="10",
        price="100",
    )
    bank = txn(
        client,
        name="Example Bank",
        nse_symbol="EXBANK",
        day="2025-01-02",
        kind="buy",
        quantity="20",
        price="400",
    )
    _seed_valuation(ex["holding_id"], bank["holding_id"])
    out = client.post("/api/journal/pretrade", headers=ORIGIN,
                      json={"side": "sell", "holding_id": ex["holding_id"], "quantity": "10", "price": "150"}).json()  # fmt: skip
    it = by_key(out)
    # gain 10 × (150 - 100) = 500, short-term (bought 15-Oct-2025): 20 % = 100 + 4 % cess = 104
    assert it["tax"]["value"] == pytest.approx(104.0) and it["tax"]["short_term"] == 500.0
    # long-term from 16-Oct-2026 (held more than 12 months), 16 days away; then inside the ₹1.25 lakh exemption: 0
    lt = it["long_term_soon"]
    assert (
        lt["status"] == "warn"
        and lt["lots"][0]["long_term_from"] == "2026-10-16"
        and lt["lots"][0]["days"] == 16
    )
    assert lt["value"] == pytest.approx(104.0)
    assert it["signal"]["value"] == "REDUCE" and it["signal"]["probability_interval"] == [0.31, 0.54]
    assert it["signal"]["status"] == "info"  # selling while the signal says REDUCE is not "against" it
    assert it["red_flags"]["status"] == "unknown"
    assert out["status"] == "warn"
    over = client.post("/api/journal/pretrade", headers=ORIGIN,
                       json={"side": "sell", "holding_id": ex["holding_id"], "quantity": "11", "price": "150"}).json()  # fmt: skip
    assert over["status"] == "block" and by_key(over)["units"]["status"] == "block"
    assert client.post("/api/journal/pretrade", headers=ORIGIN,
                       json={"side": "sell", "name": "x", "quantity": "1", "price": "1"}).status_code == 422  # fmt: skip


def test_checklist_buy_concentration_hand_computed(client):
    ex = txn(
        client,
        name="Example Textiles",
        nse_symbol="EXMPL",
        day="2025-10-15",
        kind="buy",
        quantity="10",
        price="100",
    )
    bank = txn(
        client,
        name="Example Bank",
        nse_symbol="EXBANK",
        day="2025-01-02",
        kind="buy",
        quantity="20",
        price="400",
    )
    _seed_valuation(ex["holding_id"], bank["holding_id"])
    out = client.post("/api/journal/pretrade", headers=ORIGIN,
                      json={"side": "buy", "holding_id": ex["holding_id"], "quantity": "5", "price": "150"}).json()  # fmt: skip
    it = by_key(out)
    assert (
        it["position_size"]["value"] == pytest.approx(7.5) and it["position_size"]["status"] == "ok"
    )  # 750 / 10000
    c = it["concentration"]
    # weight 1500 / 10000 = 15 % -> 2250 / 10750 = 20.93 %, above the medium-risk default 8 %
    assert c["weight_before"] == pytest.approx(15.0) and c["value"] == pytest.approx(20.93, abs=0.01)
    assert c["status"] == "warn" and c["limit_pct"] == pytest.approx(8.0)
    # N_eff = 1 / Σw²: 1 / (0.15² + 0.85²) = 1.342; 1 / (0.2093² + 0.7907²) = 1.495
    assert c["n_effective_before"] == pytest.approx(1.34, abs=0.01)
    assert c["n_effective_after"] == pytest.approx(1.49, abs=0.01)
    # buying while the signal says REDUCE is flagged
    assert it["signal"]["status"] == "warn"
    assert "no journal entry" in it["thesis"]["detail"]


def test_checklist_elss_lock_and_unknown_valuation(client):
    f = txn(client, asset_type="mf", name="Example ELSS Tax Saver Fund - Direct Growth", scheme_code="999901",
            day="2024-03-01", kind="buy", quantity="10", price="50")  # fmt: skip
    txn(client, holding_id=f["holding_id"], day="2025-09-01", kind="buy", quantity="10", price="60")
    out = client.post("/api/journal/pretrade", headers=ORIGIN,
                      json={"side": "sell", "holding_id": f["holding_id"], "quantity": "15", "price": "70"}).json()  # fmt: skip
    it = by_key(out)
    # FIFO: all 10 units of Mar-2024 (unlocked from 1-Mar-2027: still locked) and 5 of Sep-2025 (locked): 15 locked
    assert it["elss_lock"]["status"] == "block" and it["elss_lock"]["value"] == 15
    assert it["exit_load"]["status"] == "unknown"
    assert it["concentration"]["status"] == "unknown"
    assert out["status"] == "block"


# --------------------------------------------------------------------------- behaviour report
def test_behaviour_report_hand_computed(client):
    from finresearch.portfolio.history import History, Position

    a = txn(client, name="Example Alpha", nse_symbol="EXA", day="2026-05-04", kind="buy", quantity="10", price="100",
            charges="1")  # fmt: skip
    txn(
        client,
        holding_id=a["holding_id"],
        day="2026-06-01",
        kind="sell",
        quantity="10",
        price="120",
        charges="2",
    )
    b = txn(
        client, name="Example Beta", nse_symbol="EXB", day="2026-05-04", kind="buy", quantity="20", price="50"
    )
    days = [date(2026, 5, 4), date(2026, 5, 29), date(2026, 6, 1), date(2026, 6, 30)]
    hist = History(days=days, value=[2000.0, 2110.0, 960.0, 920.0], index=[1.0, 1.05, 1.06, 1.04],
                   closes={"NSE:EXA": {date(2026, 5, 29): 115.0, date(2026, 6, 1): 120.0},
                           "NSE:EXB": {date(2026, 5, 29): 48.0, date(2026, 6, 1): 45.0, date(2026, 6, 30): 46.0}},
                   positions=[Position("NSE:EXA", "Example Alpha", "stock", "equity", [a["holding_id"]], 0, None, None, 0,
                                       None, "EXA"),
                              Position("NSE:EXB", "Example Beta", "stock", "equity", [b["holding_id"]], 20, 46.0,
                                       date(2026, 6, 30), 920.0, None, "EXB")])  # fmt: skip

    async def fake_history():
        return hist, TODAY, "fp"

    client.app_.state.portfolio_history = fake_history
    r = client.get("/api/portfolio/behaviour")
    assert r.status_code == 200, r.text
    out = r.json()
    assert (out["from"], out["to"]) == ("2026-04-01", "2026-09-30")
    d = out["disposition"]
    # 1-Jun: two stocks held; EXA sold at (1200 - 2) / 10 = 119.8 > 100.1 (cost incl. charges): a realised gain;
    # EXB closed 45 < 50: a paper loss. PGR = 1 / (1 + 0) = 1, PLR = 0 / (0 + 1) = 0, SE = 0: no t
    assert (d["realised_gains"], d["paper_gains"], d["realised_losses"], d["paper_losses"]) == (1, 0, 0, 1)
    assert d["pgr"] == 1.0 and d["plr"] == 0.0 and d["t"] is None
    june = next(m for m in out["turnover"]["months"] if m["month"] == "2026-06")
    # BOM (29-May closes): 10 × 115 + 20 × 48 = 2110; sales 1150 / 2110; May's purchases still held 2110 / 2110
    assert june["sales"] == pytest.approx(1150 / 2110) and june["purchases"] == pytest.approx(1.0)
    assert june["turnover"] == pytest.approx((1150 / 2110 + 1) / 2)
    ch = out["churn"]
    # charges 1 + 2 = 3; STCG (1200 - 2) - (1000 + 1) = 197 at 20 % = 39.40 + cess 1.58 = 40.98
    assert ch["charges"] == pytest.approx(3.0) and ch["tax"] == pytest.approx(40.98, abs=0.005)
    assert ch["tax_short_term"] == pytest.approx(40.98, abs=0.005)
    hp = out["holding_periods"]
    assert hp["realised"]["winners"]["median_days"] == 28  # 4-May -> 1-Jun
    assert hp["open"]["in_loss"]["n"] == 1  # EXB: 46 < 50
    assert out["trades"] == 3 and out["trades_by_side"] == {"buy": 2, "sell": 1}
    assert "never sent to an LLM" in out["privacy"]
    assert client.get("/api/portfolio/behaviour", params={"from": "2026-05-01"}).status_code == 422


# --------------------------------------------------------------------------- privacy, export, migration
def test_journal_never_reaches_an_llm():
    pat = re.compile(
        r"portfolio\.(journal|behaviour|pretrade)|TradeNote|trade_note|/api/journal|/api/portfolio/behaviour"
    )
    for sub in ("agents", "suggest", "mcp_server", "bridge", "orchestrator", "verify", "render"):
        for p in (ROOT / "src/finresearch" / sub).rglob("*"):
            if p.is_file() and p.suffix in (".py", ".md", ".json"):
                assert not pat.search(p.read_text(errors="ignore")), p
    for mod in ("journal", "behaviour", "pretrade"):
        assert "never sent to an LLM" in (ROOT / f"src/finresearch/portfolio/{mod}.py").read_text()


def test_export_includes_the_journal(client):
    client.post(
        "/api/journal/notes", headers=ORIGIN, json={"side": "buy", "name": "Example Pharma", "thesis": "t"}
    )
    from finresearch.api.brief import export_tables
    from finresearch.db import session_scope

    with session_scope() as s:
        assert [r["name"] for r in export_tables(s)["trade_journal"]] == ["Example Pharma"]


def test_migration_matches_the_model_and_is_the_single_head():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    from finresearch.db.models import Base

    script = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    assert script.get_heads() == ["d8b4f2a6c1e9"]
    src = Path(script.get_revision("d8b4f2a6c1e9").path).read_text()
    cols = set(re.findall(r'sa\.Column\("(\w+)"', src))
    assert cols == {c.name for c in Base.metadata.tables["trade_note"].columns}
    assert set(re.findall(r'op\.create_table\(\s*"(\w+)"', src)) == {"trade_note"}


def test_entry_survives_its_trade_being_deleted(client):
    # no foreign key: deleting the transaction (and so the holding) leaves the entry, unlinked
    a = txn(client, name="Example Textiles", nse_symbol="EXMPL", day="2026-09-25", kind="buy", quantity="4", price="100")
    nid = notes(client)[0]["id"]
    assert client.delete(f"/api/portfolio/transactions/{a['id']}", headers=ORIGIN).status_code == 200
    n = next(x for x in notes(client) if x["id"] == nid)
    assert n["holding_id"] is None and n["txn_ids"] == [] and n["name"] == "Example Textiles"

"""Cross-source duplicates (#236): one reconciliation for every import path (portfolio.dedupe).

The five scenarios A-E double-counted on main (20 units instead of 10). Synthetic instruments and ids only."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal as D

import pytest

from finresearch.portfolio.connectors.base import BrokerHolding, BrokerTrade

TODAY = date(2026, 9, 30)
NOW = datetime(2026, 9, 30, 12, tzinfo=UTC)
ISIN = "INE000A01011"
ZHEAD = "symbol,isin,trade_date,exchange,segment,series,trade_type,auction,quantity,price,trade_id,order_id,order_execution_time\n"


@pytest.fixture
def db(env):
    from sqlalchemy import text

    from finresearch.db import session_scope

    with session_scope() as s:
        s.execute(text("TRUNCATE portfolio_disposal, portfolio_lot, portfolio_txn, portfolio_holding, portfolio_import, "
                       "portfolio_snapshot, portfolio_setting CASCADE"))  # fmt: skip
    yield session_scope


def _units(s) -> dict[tuple[str, str], D]:
    from finresearch.portfolio.service import lot_units

    return {k: u for k, u in lot_units(s).items() if u}


def _total(s, ikey: str = f"ISIN:{ISIN}") -> D:
    return sum((u for (ik, _), u in _units(s).items() if ik == ikey), D(0))


def _zcsv(*rows: tuple[str, int, int, str, str]) -> bytes:
    """Zerodha Console tradebook rows: (side, qty, price, trade id, order id), all on TODAY."""
    body = "".join(f"EXMPL,{ISIN},2026-09-30,NSE,EQ,EQ,{side},false,{q},{p},{tid},{oid},2026-09-30T10:00:00\n"
                   for side, q, p, tid, oid in rows)  # fmt: skip
    return (ZHEAD + body).encode()


def _trade(q=10, price=100, tid="T1", oid="O1", day=TODAY) -> BrokerTrade:
    return BrokerTrade(day=day, side="buy", quantity=D(q), price=D(price), name="EXMPL", isin=ISIN, symbol="EXMPL",
                       exchange="NSE", trade_id=tid, order_id=oid)  # fmt: skip


def _apply(s, content: bytes, name: str, sha: str):
    from finresearch.portfolio.importers import parse_tradebook
    from finresearch.portfolio.service import apply

    return apply(s, parse_tradebook(content, name), filename=name, sha256=sha * 64, saved_path=None)


def _manual(s, account: str, q=10, price=100, **kw):
    from finresearch.portfolio.service import manual_txn

    return manual_txn(s, {"asset_type": "stock", "name": "EXMPL", "account": account, "isin": ISIN, "day": TODAY,
                          "kind": "buy", "quantity": D(q), "price": D(price), **kw})  # fmt: skip


def _sync(s, account="Zerodha", source="zerodha_api", trades=(), holdings=(), **kw):
    from finresearch.portfolio.connectors.merge import merge_sync

    return merge_sync(s, account=account, source=source, label=account, holdings=list(holdings), trades=list(trades),
                      today=TODAY, now=NOW, **kw)  # fmt: skip


# --------------------------------------------------------------------------- the five verified scenarios
def test_A_api_sync_then_same_broker_tradebook(db):
    with db() as s:
        assert _sync(s, trades=[_trade()]).added == 1
        out = _apply(s, _zcsv(("buy", 10, 100, "T1", "O1")), "tradebook.csv", "b")
        assert out["added"] == 0 and _total(s) == D(10)
        (c,) = out["cross_source"]
        assert c["matched"] == "order or trade id" and c["sources"] == ["zerodha_api"]
        assert c["label"] == "already present from zerodha_api"


def test_B_manual_then_api_sync_in_another_account(db):
    with db() as s:
        _manual(s, "Manual")
        r = _sync(s, trades=[_trade()], holdings=[BrokerHolding(name="EXMPL", quantity=D(10), isin=ISIN,
                                                                symbol="EXMPL", avg_price=D(100))])  # fmt: skip
        assert r.added == 0 and _total(s) == D(10)
        assert [c["sources"] for c in r.cross_source] == [["manual"]]


def test_C_manual_then_tradebook(db):
    with db() as s:
        _manual(s, "Zerodha")
        out = _apply(s, _zcsv(("buy", 10, 100, "T1", "O1")), "t.csv", "c")
        assert out["added"] == 0 and _total(s) == D(10)
        assert out["cross_source"][0]["matched"] == "same day, side, quantity and price"


def test_D_coin_mf_baseline_then_cas(db):
    from finresearch.portfolio.importers import ImportedTxn, ImportResult
    from finresearch.portfolio.service import apply, delete_import

    fund = "ISIN:INF000K01NT8"
    with db() as s:
        mf = BrokerHolding(
            name="EX FUND", quantity=D(100), isin="INF000K01NT8", asset_type="mf", avg_price=D(50)
        )
        assert len(_sync(s, mf_holdings=[mf]).baselines) == 1
        res = ImportResult("cas", "CAMS", [ImportedTxn(account="EX AMC · folio 1", asset_type="mf", name="EX FUND",
                                                       isin="INF000K01NT8", day=date(2025, 1, 1), kind="buy",
                                                       quantity=D(100), price=D(50), amount=D(5000), source="cas",
                                                       ext="Purchase")])  # fmt: skip
        out = apply(s, res, filename="cas.pdf", sha256="d" * 64, saved_path=None)
        assert out["added"] == 1 and _total(s, fund) == D(100)  # the CAS history stands for the Coin baseline
        (sup,) = out["superseded_baselines"]
        assert sup["account"] == "Zerodha MF" and sup["sources"] == ["zerodha_api"]
        delete_import(s, out["import_id"])  # undoing the CAS brings the baseline back
        assert _units(s) == {(fund, "Zerodha MF"): D(100)}


def test_D_partial_cas_is_a_conflict_not_a_merge(db):
    from finresearch.portfolio.importers import ImportedTxn, ImportResult
    from finresearch.portfolio.service import apply

    with db() as s:
        mf = BrokerHolding(
            name="EX FUND", quantity=D(100), isin="INF000K01NT8", asset_type="mf", avg_price=D(50)
        )
        _sync(s, mf_holdings=[mf])
        res = ImportResult("cas", "CAMS", [ImportedTxn(account="EX AMC · folio 1", asset_type="mf", name="EX FUND",
                                                       isin="INF000K01NT8", day=date(2025, 1, 1), kind="buy",
                                                       quantity=D(40), price=D(50), amount=D(2000), source="cas",
                                                       ext="Purchase")])  # fmt: skip
        out = apply(s, res, filename="cas.pdf", sha256="e" * 64, saved_path=None)
        assert not out["superseded_baselines"] and out["conflicts"][0]["sources"] == ["zerodha_api"]


def test_E_groww_api_then_groww_order_csv(db):
    csv = ("Stock name,Symbol,ISIN,Type,Quantity,Value,Exchange,Exchange Order Id,Execution date and time,Order status\n"
           f"Example Ltd,EXMPL,{ISIN},BUY,10,1000,NSE,X1,30-09-2026 10:30 AM,Executed\n")  # fmt: skip
    with db() as s:
        _sync(
            s, account="Groww", source="groww_api", trades=[_trade(tid="G1", oid="G1")]
        )  # Groww's own order id
        out = _apply(s, csv.encode(), "g.csv", "e")
        assert out["added"] == 0 and _total(s) == D(10)
        assert out["cross_source"][0]["sources"] == ["groww_api"]


def test_preview_shows_already_present_not_new(db):
    from finresearch.portfolio.importers import parse_tradebook
    from finresearch.portfolio.service import preview

    with db() as s:
        _sync(s, trades=[_trade()])
        p = preview(
            s, parse_tradebook(_zcsv(("buy", 10, 100, "T1", "O1"), ("buy", 5, 101, "T9", "O9")), "t.csv")
        )
        assert (p["new_rows"], p["duplicates"]) == (1, 0) and len(p["cross_source"]) == 1
        assert sorted(r["status"] for r in p["rows_preview"]) == ["already present from zerodha_api", "new"]
        assert p["holdings"][0]["units_after"] == "15.000"


# --------------------------------------------------------------------------- manual entries vs earlier imports
def test_manual_entry_after_an_import_is_refused_unless_confirmed(db):
    from finresearch.portfolio.service import DuplicateEntry

    with db() as s:
        _apply(s, _zcsv(("buy", 10, 100, "T1", "O1")), "t.csv", "f")
        with pytest.raises(DuplicateEntry, match="already present from zerodha"):
            _manual(s, "Manual")
        assert _total(s) == D(10)
        _manual(s, "Manual", allow_duplicate=True)  # the user says it is a second, real trade
        assert _total(s) == D(20)


def test_manual_entry_api_returns_409(client_api):
    c, headers = client_api
    body = {"asset_type": "stock", "name": "EXMPL", "isin": ISIN, "account": "Manual", "day": TODAY.isoformat(),
            "kind": "buy", "quantity": "10", "price": "100"}  # fmt: skip
    import base64

    tb = {"filename": "t.csv", "content_b64": base64.b64encode(_zcsv(("buy", 10, 100, "T1", "O1"))).decode(),
          "dry_run": False}  # fmt: skip
    assert c.post("/api/portfolio/import/tradebook", headers=headers, json=tb).json()["added"] == 1
    r = c.post("/api/portfolio/transactions", headers=headers, json=body)
    assert r.status_code == 409 and "already present from zerodha (in Zerodha)" in r.json()["detail"]
    ok = c.post("/api/portfolio/transactions", headers=headers, json={**body, "allow_duplicate": True})
    assert ok.status_code == 201


@pytest.fixture
def client_api(env):
    from fastapi.testclient import TestClient
    from sqlalchemy import text

    from finresearch.api import create_app
    from finresearch.db import session_scope

    with session_scope() as s:
        s.execute(text("TRUNCATE portfolio_disposal, portfolio_lot, portfolio_txn, portfolio_holding, portfolio_import "
                       "CASCADE"))  # fmt: skip
    with TestClient(create_app()) as c:
        yield c, {"Origin": "http://127.0.0.1:3000", "X-FinResearch": "1"}


# --------------------------------------------------------------------------- genuine repeats and partial overlaps
def test_two_real_buys_in_one_tradebook_are_both_kept(db):
    with db() as s:
        out = _apply(s, _zcsv(("buy", 10, 100, "T1", "O1"), ("buy", 10, 100, "T2", "O2")), "t.csv", "1")
        assert out["added"] == 2 and _total(s) == D(20)


def test_api_then_tradebook_with_a_second_real_buy(db):
    with db() as s:
        _sync(s, trades=[_trade()])
        out = _apply(s, _zcsv(("buy", 10, 100, "T1", "O1"), ("buy", 10, 100, "T2", "O2")), "t.csv", "2")
        assert out["added"] == 1 and len(out["cross_source"]) == 1 and _total(s) == D(20)


def test_tradebook_then_api_with_a_different_exchange_trade_is_not_merged(db):
    with db() as s:
        _apply(s, _zcsv(("buy", 10, 100, "T1", "O1")), "t.csv", "3")
        r = _sync(s, trades=[_trade(tid="T2", oid="O2")])  # same day, side, units and price: another order
        assert r.added == 1 and not r.cross_source and _total(s) == D(20)


def test_manual_then_two_real_buys(db):
    with db() as s:
        _manual(s, "Manual")
        out = _apply(s, _zcsv(("buy", 10, 100, "T1", "O1"), ("buy", 10, 100, "T2", "O2")), "t.csv", "4")
        assert out["added"] == 1 and len(out["cross_source"]) == 1 and _total(s) == D(20)


def test_partial_overlap_is_a_conflict(db):
    with db() as s:
        _manual(s, "Zerodha")
        out = _apply(s, _zcsv(("buy", 6, 100, "T1", "O1")), "t.csv", "5")
        assert out["added"] == 0 and _total(s) == D(10)
        (c,) = out["conflicts"]
        assert (c["units"], c["other_units"], c["sources"]) == ("6", "10", ["manual"])


def test_same_units_at_a_different_price_is_a_conflict(db):
    with db() as s:
        _manual(s, "Zerodha", price=150)
        out = _apply(s, _zcsv(("buy", 10, 100, "T1", "O1")), "t.csv", "6")
        assert out["added"] == 0 and out["conflicts"] and not out["cross_source"]


def test_fills_of_one_order_cover_a_manual_entry(db):
    with db() as s:
        _manual(s, "Zerodha")
        out = _apply(s, _zcsv(("buy", 4, 100, "T1", "O1"), ("buy", 6, 100, "T2", "O1")), "t.csv", "7")
        assert (
            out["added"] == 0
            and [c["matched"] for c in out["cross_source"]] == ["same day, side and total units"] * 2
        )
        assert _total(s) == D(10)


def test_two_broker_accounts_are_never_matched(db):
    """The same buy in two demat accounts is two trades: only a manual entry is compared across accounts."""
    with db() as s:
        _apply(s, _zcsv(("buy", 10, 100, "T1", "O1")), "t.csv", "8")
        r = _sync(s, account="Groww", source="groww_api", trades=[_trade(tid="G1", oid="G1")])
        assert r.added == 1 and _total(s) == D(20)

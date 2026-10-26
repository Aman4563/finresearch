"""The read-only broker reconciliation (#368, P1.6 (e), dq.broker): Groww's F&O positions and holdings as last synced
against the app's own book (the real fno_mark legs; the portfolio's transactions), from rows already stored: no Groww
call. Personal data, so every check note carries counts only. Synthetic only: made-up symbols (ZZIDX..., ZYXWV), a
fake ISIN and a made-up company name; test database only."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text

TABLES = ("broker_connection, broker_sync_log, fno_mark, alert_eval_slot, portfolio_lot, portfolio_txn, "
          "portfolio_holding, portfolio_import, dq_check, dataset_status")  # fmt: skip
R = date(2026, 10, 21)  # a Wednesday session
READ = datetime(2026, 10, 21, 10, 35, tzinfo=UTC)  # 16:05 IST that day
NIGHT = datetime(2026, 10, 21, 15, 30, tzinfo=UTC)  # 21:00 IST: the evening data-quality pass
CE, PE, FUT = "ZZIDX26OCT25000CE", "ZZIDX26OCT24000PE", "ZZIDX26OCTFUT"


@pytest.fixture
def clean(env):
    from finresearch.db import session_scope

    with session_scope() as s:
        s.execute(text(f"TRUNCATE {TABLES} CASCADE"))
    yield env
    with session_scope() as s:  # leave no synthetic personal data for later tests
        s.execute(text(f"TRUNCATE {TABLES} CASCADE"))


def connect(s, positions=None, at=READ, funds=None) -> None:
    from finresearch.db.models import BrokerConnection

    state = {} if positions is None else {"fno_positions": {"at": at.isoformat(), "rows": positions}}
    if funds:
        state["funds"] = funds
    s.add(BrokerConnection(key="groww", enabled=True, auto_sync=True, config={}, state=state, status="connected",
                           last_sync_at=at))  # fmt: skip


def pos(symbol: str, qty: int, known: bool = True) -> dict:
    return {
        "symbol": symbol,
        "quantity": float(qty),
        "direction_known": known,
        "segment": "FNO",
        "product": "NRML",
    }


def mark(s, symbol: str, qty: int | None, day: date = R) -> None:
    from finresearch.db.models import FnoMark

    s.add(FnoMark(day=day, book="real", leg_key=f"groww:{symbol}", position_key="ZZIDX:2026-10-27", symbol="ZZIDX",
                  expiry=date(2026, 10, 27), right="call", strike=Decimal(25000), qty=qty))  # fmt: skip


def marks_done(s, day: date = R) -> None:
    from finresearch.db.models import AlertEvalSlot

    s.add(AlertEvalSlot(slot=f"fno_marks:{day.isoformat()}", started_at=READ, result={"status": "done"}))


def checks(now=NIGHT) -> dict:
    from finresearch.db import session_scope
    from finresearch.dq import broker

    with session_scope() as s:
        return {c.check: c for c in broker.broker_checks(s, now)}


def seed(**kw):
    from finresearch.db import session_scope

    with session_scope() as s:
        for fn, args in kw.items():
            for a in args:
                {"connect": connect, "mark": mark, "marks_done": marks_done}[fn](s, *a)


def test_positions_matching_the_book_are_ok(clean):
    seed(connect=[([pos(CE, -75), pos(PE, 75)],)], mark=[(CE, -75), (PE, 75)], marks_done=[()])
    c = checks()["fno_positions_vs_book"]
    assert (c.status, c.value, c.critical) == ("ok", 0, True)
    assert c.note == "2 Groww F&O position(s) read 2026-10-21 match the app's book for that session"


def test_each_kind_of_position_mismatch_is_counted(clean):
    # Groww: CE -75 (book -150), PE 75 (not in the book), the book's future not at Groww: three mismatches
    seed(connect=[([pos(CE, -75), pos(PE, 75)],)], mark=[(CE, -150), (FUT, 50)], marks_done=[()])
    c = checks()["fno_positions_vs_book"]
    assert (c.status, c.value) == ("warn", 3)
    assert c.note == ("3 mismatch(es) between Groww's F&O positions read 2026-10-21 and the app's book: 1 not in the "
                      "book, 1 no longer at Groww, 1 quantity differs (details: finresearch dq broker)")  # fmt: skip


@pytest.mark.parametrize("case,why", [
    ("not_connected", "Groww is not connected"),
    ("never_read", "never read"),
    ("marks_not_written", "no F&O marks for 2026-10-21"),
    ("direction_unknown", "long or short unknown"),
    ("stale_read", "the app's book has moved on to 2026-10-22"),
])  # fmt: skip
def test_what_cannot_be_compared_is_unknown_never_ok(clean, case, why):
    if case == "never_read":
        seed(connect=[(None,)])
    elif case == "marks_not_written":
        seed(connect=[([pos(CE, -75)],)], mark=[(CE, -75)])
    elif case == "direction_unknown":
        seed(connect=[([pos(CE, -75, known=False)],)], mark=[(CE, None)], marks_done=[()])
    elif case == "stale_read":
        seed(connect=[([pos(CE, -75)],)], mark=[(CE, -75)], marks_done=[(), (R + timedelta(days=1),)])
    c = checks(NIGHT + timedelta(days=1))["fno_positions_vs_book"]
    assert (c.status, c.critical) == ("unknown", True) and why in c.note


SYNCED = datetime(
    2026, 10, 21, 10, 40, tzinfo=UTC
)  # the 16:10 IST sync: its holdings stand for 20-Oct (no T trades)
ISIN, NAME = "INE999Q01017", "Zyxwv Phantom Industries Ltd"


def synced(s, rows: list[dict], at: datetime = SYNCED, read: bool = True) -> None:
    from finresearch.db.models import BrokerSyncLog

    summary = {"reconciliation": rows, "reconciled": all(r["ok"] for r in rows), "differences": 0} if read else {
        "reconciliation": [], "reconciled": None, "differences": None}  # fmt: skip
    s.add(
        BrokerSyncLog(
            key="groww", trigger="scheduled", status="ok", started_at=at, finished_at=at, summary=summary
        )
    )


def row(ikey: str, units: str, status: str = "ok") -> dict:
    return {"name": NAME, "ikey": ikey, "account": "Groww", "broker_units": units, "app_units": units, "diff": "0",
            "ok": status == "ok", "status": status, "broker_avg_price": "100"}  # fmt: skip


def holding(
    s, ikey: str, trades: list[tuple[date, str, int]], isin: str | None = ISIN, sym: str | None = None
) -> None:
    from finresearch.db.models import PortfolioHolding, PortfolioTxn

    h = PortfolioHolding(
        ikey=ikey, account="Groww", asset_type="stock", name=NAME, isin=isin, nse_symbol=sym, meta={}
    )
    s.add(h)
    s.flush()
    for i, (d, kind, q) in enumerate(trades):
        s.add(PortfolioTxn(holding_id=h.id, day=d, kind=kind, quantity=Decimal(q), price=Decimal(100),
                           amount=Decimal(100 * q), source="manual", dedupe_key=f"{ikey}:{i}", meta={}))  # fmt: skip


def held(rows, holdings=(), now=NIGHT, read=True):
    from finresearch.db import session_scope

    with session_scope() as s:
        synced(s, rows, read=read)
        for h in holdings:
            holding(s, *h)
    return checks(now)["holdings_vs_book"]


BUY = (date(2026, 10, 14), "buy", 10)


@pytest.mark.parametrize("trades,want", [
    ([BUY], ("ok", 0)),
    ([BUY, (date(2026, 10, 19), "sell", 4)], ("warn", 1)),  # an edit on or before 20-Oct: the book holds 6, Groww 10
    ([BUY, (R, "buy", 5)], ("ok", 0)),  # bought on the sync day: not in Groww's holdings yet, nor compared
])  # fmt: skip
def test_holdings_against_the_book_as_of_the_snapshot_day(clean, trades, want):
    c = held([row(f"ISIN:{ISIN}", "10")], [(f"ISIN:{ISIN}", trades)])
    assert ((c.status, c.value), c.critical) == (want, True)


def test_a_holding_is_found_the_way_the_sync_found_it(clean):
    # the sync stored NSE:ZYXWV; the app keeps it under its ISIN key with the same NSE symbol
    c = held([row("NSE:ZYXWV", "10")], [(f"ISIN:{ISIN}", [BUY], ISIN, "ZYXWV")])
    assert (c.status, c.value) == ("ok", 0)


def test_holdings_on_one_side_only_are_mismatches(clean):
    c = held([row(f"ISIN:{ISIN}", "10")], [("ISIN:INE998Q01018", [BUY], "INE998Q01018")])
    assert (c.status, c.value) == ("warn", 2)  # Groww's not in the book, the book's not at Groww
    assert "1 not in the book, 1 not at Groww" in c.note


@pytest.mark.parametrize("case,why", [
    ("t1", "Groww's own count was undecided"),
    ("cut", "the sync kept only the first 300 rows"),
    ("stale", "last read on 2026-10-21"),
    ("unread", "no Groww sync has read the holdings"),
])  # fmt: skip
def test_holdings_that_cannot_be_vouched_for_are_unknown(clean, case, why):
    rows = [row(f"ISIN:{ISIN}", "10", status="unknown" if case == "t1" else "ok")]
    if case == "cut":
        rows += [row(f"ISIN:INE{i:06d}A01", "0") for i in range(299)]  # resolved rows: Groww 0, the book 0
    c = held(rows, [(f"ISIN:{ISIN}", [BUY])], now=NIGHT + timedelta(days=5 if case == "stale" else 0),
             read=case != "unread")  # fmt: skip
    assert (c.status, c.critical) == ("unknown", True) and why in c.note


FREE = lambda _p: 50 * 2**30  # noqa: E731


def nightly():
    from finresearch.db import session_scope
    from finresearch.db.models import DatasetStatus
    from finresearch.dq import run

    with session_scope() as s:
        run.run_all(s, NIGHT, holidays=set(), free_bytes=FREE)
    with session_scope() as s:
        row = s.get(DatasetStatus, "broker_groww")
        return None if row is None else row.status


@pytest.mark.parametrize("case,status", [("none", "unknown"), ("never_read", "unknown"), ("marks_pending", "unknown"),
                                         ("mismatch", "warn"), ("all_ok", "ok")])  # fmt: skip
def test_dataset_status_through_the_nightly_pass(clean, case, status):
    if case == "never_read":
        seed(connect=[(None,)])
    elif case == "marks_pending":  # positions read for the session, its marks not written yet
        seed(connect=[([pos(CE, -75)],)], mark=[(CE, -75)])
    elif case in ("mismatch", "all_ok"):
        seed(connect=[([pos(CE, -75)],)], mark=[(CE, -75 if case == "all_ok" else -150)], marks_done=[()])
        held([row(f"ISIN:{ISIN}", "10")], [(f"ISIN:{ISIN}", [BUY])])
    assert nightly() == status


def test_only_ok_warn_or_unknown_and_no_page_is_gated(clean):
    from finresearch.db import session_scope
    from finresearch.dq import gate

    with session_scope() as s:
        before = {p: gate.health(s, ds)["status"] for p, ds in gate.DEPENDENTS.items()}
    seed(
        connect=[([pos(CE, -75), pos(PE, 75, known=False)],)], mark=[(CE, -150), (PE, None)], marks_done=[()]
    )
    held([row(f"ISIN:{ISIN}", "10", status="unknown")], [(f"ISIN:{ISIN}", [BUY])])
    got = checks()
    assert {c.status for c in got.values()} == {"warn", "unknown"} and all(c.critical for c in got.values())
    assert nightly() == "warn"
    assert all("broker_groww" not in ds for ds in gate.DEPENDENTS.values())
    with session_scope() as s:
        assert {p: gate.health(s, ds)["status"] for p, ds in gate.DEPENDENTS.items()} == before


MARKERS = ("ZZTEST26OCT25000CE", "734519", "Zyxwv", "INE999Q01017", "8812345")


def test_no_instrument_quantity_or_name_leaves_the_reconciliation(clean, caplog):
    import json

    from finresearch.db import session_scope
    from finresearch.dq import report

    seed(connect=[([pos("ZZTEST26OCT25000CE", 734519)],)], marks_done=[()])
    held([row(f"ISIN:{ISIN}", "8812345")])
    nightly()
    with session_scope() as s:
        notes = [n or "" for n in s.scalars(select_notes())]
        health = report.data_health(s)
    blob = "\n".join([*notes, json.dumps(health, default=str), *(h["text"] for h in report.brief_lines(health)),
                      *(r.getMessage() for r in caplog.records)])  # fmt: skip
    assert any(
        d["dataset"] == "broker_groww" for d in health["datasets"]
    )  # the reconciliation ran and is shown
    assert not [m for m in MARKERS if m in blob]


def select_notes():
    from sqlalchemy import select

    from finresearch.db.models import DqCheck

    return select(DqCheck.note)


def test_a_crash_is_unknown_with_the_exception_type_only(clean, monkeypatch, caplog):
    from finresearch.dq import broker

    def boom(s, now):
        raise KeyError("ZZTEST26OCT25000CE")

    monkeypatch.setattr(broker, "fno_positions", boom)
    held([row(f"ISIN:{ISIN}", "10")], [(f"ISIN:{ISIN}", [BUY])])
    got = checks()
    assert got["fno_positions_vs_book"].note == "not verified: could not be checked (KeyError)"
    assert got["holdings_vs_book"].status == "ok"  # the other check still ran
    assert not any("ZZTEST" in r.getMessage() for r in caplog.records)


def test_reads_stored_rows_only(clean, monkeypatch):
    """No Groww call: every HTTP send is refused while the reconciliation runs, and the module imports no connector,
    adapter or fnobook code (the static half of the same promise)."""
    import ast
    from pathlib import Path

    import httpx

    def refuse(*a, **k):
        raise AssertionError("a network request was made")

    monkeypatch.setattr(httpx.Client, "send", refuse)
    monkeypatch.setattr(httpx.AsyncClient, "send", refuse)
    seed(connect=[([pos(CE, -75)],)], mark=[(CE, -75)], marks_done=[()])
    held([row(f"ISIN:{ISIN}", "10")], [(f"ISIN:{ISIN}", [BUY])])
    assert {c.status for c in checks().values()} == {"ok"}
    src = Path(__file__).parents[1] / "src/finresearch/dq/broker.py"
    mods = [
        n.module for n in ast.walk(ast.parse(src.read_text())) if isinstance(n, ast.ImportFrom) and n.module
    ]
    banned = ("finresearch.portfolio.connectors", "finresearch.adapters", "finresearch.fnobook")
    assert not [m for m in mods if m.startswith(banned)], mods


def test_mirrored_constants_match_the_connector_and_the_merge():
    from finresearch.dq import broker
    from finresearch.portfolio.connectors import merge
    from finresearch.portfolio.connectors.groww import GrowwConnector

    assert (GrowwConnector.key, GrowwConnector.account) == (broker.KEY, broker.ACCOUNT)
    assert GrowwConnector.holdings_include_today == broker.GROWW_HOLDINGS_INCLUDE_TODAY
    assert broker.UNITS_TOL == merge.UNITS_TOL
    cut = merge.MergeResult(reconciliation=[{"ok": True}] * (broker.RECON_CAP + 50)).as_dict()[
        "reconciliation"
    ]
    assert len(cut) == broker.RECON_CAP

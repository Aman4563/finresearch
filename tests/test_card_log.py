"""#360 (P0.10): every plan, suggest and strategy-check response appends exactly one row to the append-only card log,
carrying its inputs (request, live chain, card inputs), each card's verdict, failed gates, PoP and EV per model and
evidence label, and the engine's git SHA. The table refuses UPDATE and DELETE. Offline: test_fno_signal's FakeFno."""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import func, select, text
from test_fno_plan_api import PCS, _profile, client  # noqa: F401  (the fixture)

from finresearch.fincalc import dates

CARD_FIELDS = {"title", "action", "qualifier", "label", "failed", "probability", "ev", "evidence", "spec_key"}


def _rows():
    from finresearch.db import session_scope
    from finresearch.db.models import CardLog

    with session_scope() as s:
        return [
            {c.name: getattr(r, c.name) for c in CardLog.__table__.columns}
            for r in s.scalars(select(CardLog).order_by(CardLog.id))
        ]


def _count() -> int:
    from finresearch.db import session_scope
    from finresearch.db.models import CardLog

    with session_scope() as s:
        return s.scalar(select(func.count()).select_from(CardLog))


def _common(row, kind):
    assert row["kind"] == kind and row["symbol"] == "NIFTY" and row["created_at"] is not None
    # the engine that produced the card: this checkout's full commit SHA
    assert row["engine_sha"] and len(row["engine_sha"]) == 40
    inp = row["inputs"]
    assert inp["request"] and inp["chains"], "the request and the live chain(s) it was built on"
    ch = inp["chains"][0]
    assert ch["symbol"] == "NIFTY" and ch["underlying"] and ch["rows"] and "as_of" in ch and ch["source"]
    assert row["response"], "the full response the owner saw"
    for c in row["cards"]:
        assert set(c) >= CARD_FIELDS, CARD_FIELDS - set(c)
        assert {"real_world", "risk_neutral"} <= set(c["probability"]) and {
            "real_world",
            "risk_neutral",
        } <= set(c["ev"])


def test_plan_appends_exactly_one_row_with_every_field(client):  # noqa: F811
    _profile("1000000")
    n = _count()
    card = client.post("/api/fno/plan", json={"symbol": "NIFTY", "expiry": "2026-10-06", "legs": PCS}).json()
    assert _count() == n + 1
    row = _rows()[-1]
    _common(row, "plan")
    assert row["expiry"].isoformat() == "2026-10-06" and card["card_log_id"] == row["id"]
    assert row["inputs"]["request"]["legs"][0]["strike"] in ("22600", 22600, "22600.0")
    pin = row["inputs"]["plan"]
    assert pin["capital"] == 1000000 and pin["max_loss_pct"] == 2 and pin["rate"] and pin["closes"]
    [c] = row["cards"]
    assert (
        c["action"] == card["action"]
        and c["qualifier"] == card["qualifier"]
        and c["failed"] == card["failed"]
    )
    assert c["probability"]["real_world"] == card["probability"]["real_world"]
    assert c["probability"]["risk_neutral"] == card["probability"]["risk_neutral"]
    assert c["ev"]["real_world"] is not None  # the card's "EV after costs (real-world)" check
    assert c["evidence"]["level"] == card["evidence"]["level"] and c["evidence"]["match"] == "related"


def test_suggest_appends_one_row_for_the_whole_response(client):  # noqa: F811
    _profile("1000000")
    n = _count()
    got = client.get("/api/fno/NIFTY/suggest").json()
    assert _count() == n + 1
    row = _rows()[-1]
    _common(row, "suggest")
    assert got["card_log_id"] == row["id"]
    assert [c["title"] for c in row["cards"]] == [c["title"] for c in got["cards"]]
    assert [c["action"] for c in row["cards"]] == [c["action"] for c in got["cards"]]
    # #383: each served card names its row and index, so a paper trade taken from it can be linked back
    assert [(c["card_log_id"], c["card_index"]) for c in got["cards"]] == [
        (row["id"], i) for i in range(len(got["cards"]))
    ]


def test_strategy_check_appends_one_row_without_inventing_a_verdict(client):  # noqa: F811
    _profile("0")
    body = {"symbol": "NIFTY", "expiry": "2026-10-06", "legs": [
        {"right": "call", "strike": "22800", "side": "buy", "lots": 1},
        {"right": "call", "strike": "23000", "side": "sell", "lots": 1}]}  # fmt: skip
    n = _count()
    r = client.post("/api/fno/strategy", json=body).json()
    assert _count() == n + 1
    row = _rows()[-1]
    _common(row, "strategy")
    assert r["card_log_id"] == row["id"]
    [c] = row["cards"]
    # the strategy check runs no gates: its verdict and failed gates are unknown, never "ok"
    assert c["action"] is None and c["failed"] is None
    an = r["analysis"]
    assert c["probability"]["risk_neutral"] == an["risk_neutral"]["pop"]
    assert (
        c["ev"]["risk_neutral"] == an["risk_neutral"]["ev"]
        and c["ev"]["real_world"] == an["real_world"]["ev"]
    )


def test_a_refused_request_logs_nothing(client):  # noqa: F811
    n = _count()
    bad = client.post("/api/fno/plan", json={"symbol": "NIFTY", "expiry": "2020-01-02", "legs": PCS})
    assert bad.status_code == 422 and _count() == n


@pytest.mark.parametrize("sql", ["UPDATE card_log SET kind = 'x'", "DELETE FROM card_log"])
def test_the_table_refuses_update_and_delete(client, sql):  # noqa: F811
    from sqlalchemy.exc import DBAPIError

    from finresearch.db import session_scope

    _profile("1000000")
    client.post("/api/fno/plan", json={"symbol": "NIFTY", "expiry": "2026-10-06", "legs": PCS})
    n = _count()
    assert n >= 1
    with pytest.raises(DBAPIError, match="append-only"), session_scope() as s:
        s.execute(text(sql))
    assert _count() == n


def test_release_report_counts_verdicts_before_and_after(client, monkeypatch):  # noqa: F811
    from finresearch.db import session_scope
    from finresearch.fnobook import card_log

    with session_scope() as s:
        s.execute(text("TRUNCATE card_log"))
    rows = [
        ("2026-10-01", [("ENTER", None), ("WAIT", None)]),
        ("2026-10-02", [("ENTER", "no proven edge — size small"), ("AVOID", None)]),
        ("2026-10-20", [("ENTER", "no proven edge — size small"), ("AVOID", None), ("AVOID", None)]),
        ("2026-10-21", [(None, None)]),  # a strategy check: no verdict
    ]
    with session_scope() as s:
        for day, cards in rows:
            s.add(card_log.row("suggest", symbol="NIFTY", expiry=None, inputs={}, response={},
                               cards=[{"action": a, "qualifier": q} for a, q in cards],
                               created_at=datetime.fromisoformat(day + "T10:00:00+05:30")))  # fmt: skip
    with session_scope() as s:
        got = card_log.verdict_counts(s, datetime(2026, 10, 10, tzinfo=dates.IST))
    assert got["before"] == {"ENTER": 1, "NO PROVEN EDGE": 1, "WAIT": 1, "AVOID": 1, "no verdict": 0}
    assert got["after"] == {"ENTER": 0, "NO PROVEN EDGE": 1, "WAIT": 0, "AVOID": 2, "no verdict": 1}
    assert got["responses"] == {"before": 2, "after": 2}


def test_release_report_cli_prints_the_before_after_table(client):  # noqa: F811
    from typer.testing import CliRunner

    from finresearch.cli import app
    from finresearch.db import session_scope
    from finresearch.fnobook import card_log

    with session_scope() as s:
        s.execute(text("TRUNCATE card_log"))
        s.add(card_log.row("plan", symbol="NIFTY", expiry=None, inputs={}, response={},
                           cards=[{"action": "AVOID", "qualifier": None}],
                           created_at=datetime(2026, 10, 1, 10, tzinfo=dates.IST)))  # fmt: skip
    r = CliRunner().invoke(app, ["fno", "card-log", "--since", "2026-10-10T00:00:00+05:30"])
    assert r.exit_code == 0, r.output
    assert "AVOID" in r.output and "NO PROVEN EDGE" in r.output and "before" in r.output
    bad = CliRunner().invoke(app, ["fno", "card-log", "--since", "2026-10-10"])  # no zone: ambiguous, refused
    assert bad.exit_code != 0


def test_the_migration_creates_the_same_append_only_trigger_as_the_models():
    """Tests build the schema with create_all; the live database with Alembic: both must install the trigger."""
    import importlib.util
    from pathlib import Path

    from finresearch.db.models import CARD_LOG_APPEND_ONLY

    path = Path(__file__).parents[1] / "migrations" / "versions" / "d4a8c2e6f1b9_card_log.py"
    spec = importlib.util.spec_from_file_location("mig_card_log", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.APPEND_ONLY == CARD_LOG_APPEND_ONLY

"""/api/portfolio/ais: import (JSON and PDF), the stored AIS without identifiers, and the check. Synthetic data only."""

from __future__ import annotations

import base64
import json

import pytest
from fastapi.testclient import TestClient
from test_ais import AIS_PDF_LINES, FIX, PDF_PW, SECRETS, _pdf

ORIGIN = {"Origin": "http://127.0.0.1:3000", "X-FinResearch": "1"}
TXNS = [  # the app side of tests/test_ais.py::_app, entered by hand
    {"name": "Example Ltd", "isin": "INE000X01011", "day": "2025-04-10", "kind": "buy", "quantity": 20, "price": 1000},
    {"name": "Example Ltd", "isin": "INE000X01011", "day": "2025-07-10", "kind": "dividend", "amount": 1500},
    {"name": "Example Ltd", "isin": "INE000X01011", "day": "2025-11-03", "kind": "sell", "quantity": 10,
     "price": 1234.5, "charges": 25},
    {"name": "Sample Industries Ltd", "isin": "INE000X01029", "day": "2025-04-15", "kind": "buy", "quantity": 10,
     "price": 1800},
    {"name": "Sample Industries Ltd", "isin": "INE000X01029", "day": "2025-08-20", "kind": "dividend", "amount": 9000},
    {"name": "Sample Industries Ltd", "isin": "INE000X01029", "day": "2026-01-12", "kind": "sell", "quantity": 5,
     "price": 2000},
    {"name": "Gamma Bluechip Fund - Direct Growth", "asset_type": "mf", "day": "2025-06-05", "kind": "buy",
     "quantity": 400, "price": 50},
    {"name": "Gamma Bluechip Fund - Direct Growth", "asset_type": "mf", "day": "2026-02-20", "kind": "sell",
     "quantity": 100, "price": 50},
    {"name": "Alpha Flexi Cap Fund - Direct IDCW", "asset_type": "mf", "day": "2025-12-15", "kind": "dividend",
     "amount": 6000},
    {"name": "Beta Ltd", "day": "2025-06-01", "kind": "dividend", "amount": 250},
]  # fmt: skip


@pytest.fixture
def client(env):
    from sqlalchemy import text

    from finresearch.api import create_app
    from finresearch.db import session_scope

    with session_scope() as s:
        s.execute(text("TRUNCATE portfolio_disposal, portfolio_lot, portfolio_txn, portfolio_holding, "
                       "portfolio_import, portfolio_ais"))  # fmt: skip
    with TestClient(create_app()) as c:
        yield c


def _seed(c) -> None:
    for t in TXNS:
        r = c.post("/api/portfolio/transactions", headers=ORIGIN, json=t)
        assert r.status_code == 201, r.text


def _post(c, blob: bytes, **kw):
    return c.post("/api/portfolio/ais/import", headers=ORIGIN,
                  json={"filename": "ais.json", "content_b64": base64.b64encode(blob).decode(), **kw})  # fmt: skip


def _stored_text() -> str:
    from sqlalchemy import text

    from finresearch.db import session_scope

    with session_scope() as s:
        rows = s.execute(text("SELECT * FROM portfolio_ais")).mappings().all()
        imports = s.execute(text("SELECT * FROM portfolio_import")).mappings().all()
    return json.dumps([dict(r) for r in [*rows, *imports]], default=str)


def test_json_import_dry_run_then_store_and_check(client):
    _seed(client)
    blob = FIX.read_bytes()
    dry = _post(client, blob)
    assert dry.status_code == 200, dry.text
    assert dry.json()["dry_run"] is True and dry.json()["fy"] == 2026
    assert client.get("/api/portfolio/ais").json()["statements"] == []  # a dry run stores nothing
    r = _post(client, blob, dry_run=False)
    assert r.status_code == 200 and r.json()["already_imported"] is None
    chk = client.get("/api/portfolio/ais/2026").json()
    # hand count: dividends Example (match), Sample (net of TDS), Demo (AIS only), Alpha MF (AMC match), Beta (app
    # only); sales Example, Gamma (match), Sample (mismatch); purchases Gamma (match) and the Example and Sample buys,
    # which the AIS fixture does not report (app only)
    assert chk["counts"] == {"mismatch": 2, "only_ais": 1, "only_app": 3, "matched": 5}
    sale = next(r for r in chk["rows"] if r["category"] == "sale" and r["isin"] == "INE000X01011")
    assert (sale["status"], sale["app_amount"]) == (
        "matched",
        12345.0,
    )  # gross 10 x 1,234.50, charges not deducted
    listing = client.get("/api/portfolio/ais").json()
    assert [s["fy"] for s in listing["statements"]] == [2026]
    assert {"fy": 2026, "label": "FY 2025-26"} in listing["app_years"]
    again = _post(client, blob)
    assert again.json()["already_imported"] == 2026
    assert client.get("/api/portfolio/ais/2025").status_code == 404
    assert client.delete("/api/portfolio/ais/2026", headers=ORIGIN).json() == {"deleted": 2026}
    assert client.get("/api/portfolio/ais/2026").status_code == 404


def test_no_identifier_is_stored_or_returned(client):
    _seed(client)
    r = _post(client, FIX.read_bytes(), dry_run=False)
    pdf = _post(client, _pdf(AIS_PDF_LINES, PDF_PW), password=PDF_PW, dry_run=True)
    texts = [_stored_text(), r.text, pdf.text, client.get("/api/portfolio/ais/2026").text,
             client.get("/api/portfolio/ais").text]  # fmt: skip
    for t in texts:
        for s in (*SECRETS, PDF_PW):
            assert s not in t, s


def test_pdf_password_and_year_errors_are_safe(client):
    blob = _pdf(AIS_PDF_LINES, PDF_PW)
    no_pw = _post(client, blob)
    assert no_pw.status_code == 422 and "ddmmyyyy" in no_pw.json()["detail"]
    bad = _post(client, blob, password="zzzzz9999z31121999")
    assert bad.status_code == 422 and "zzzzz9999z31121999" not in bad.text
    ok = _post(client, blob, password=PDF_PW)
    assert ok.status_code == 200 and ok.json()["format"] == "pdf" and PDF_PW not in ok.text
    wrong_year = _post(client, FIX.read_bytes(), fy=2025)
    assert wrong_year.status_code == 422 and "FY 2025-26" in wrong_year.json()["detail"]
    no_year = json.loads(FIX.read_text())
    del no_year["financialYear"]
    for blk in no_year["sftInfo"]:  # dates in two FYs: the year cannot be inferred
        blk["details"][0]["transactionDate"] = "01/04/2024"
    r = _post(client, json.dumps(no_year).encode())
    assert r.status_code == 422 and r.json()["detail"].startswith("fy:")
    assert _post(client, json.dumps(no_year).encode(), fy=2026).status_code == 200
    assert client.post("/api/portfolio/ais/import", headers={"Origin": "https://evil.example"},
                       json={}).status_code == 403  # fmt: skip


def test_inbox_imports_ais_json_and_leaves_ais_pdf_waiting(client):
    import os
    from datetime import UTC, datetime

    from finresearch.portfolio.connectors.inbox import ensure_dirs, scan

    now = datetime(2026, 10, 5, 12, tzinfo=UTC)
    root = ensure_dirs()
    (root / "ais_2025-26.json").write_bytes(FIX.read_bytes())
    (root / "AIS_2025-26.pdf").write_bytes(_pdf(AIS_PDF_LINES, PDF_PW))
    (root / "other.json").write_text('{"hello": "world"}')
    old = now.timestamp() - 60
    for p in root.iterdir():
        if p.is_file():
            os.utime(p, (old, old))
    by = {f["file"]: f for f in scan(now=now)["files"]}
    assert (
        by["ais_2025-26.json"]["status"] == "imported" and by["ais_2025-26.json"]["kind"] == "AIS FY 2025-26"
    )
    assert by["AIS_2025-26.pdf"]["status"] == "waiting" and (root / "AIS_2025-26.pdf").exists()
    assert by["other.json"]["status"] == "failed"
    assert [s["fy"] for s in client.get("/api/portfolio/ais").json()["statements"]] == [2026]
    assert (root / "processed" / "ais_2025-26.json").exists()  # the user's own file moves; no copy is made
    assert not (root.parent / "imports").exists() or not any((root.parent / "imports").iterdir())
    for s in SECRETS:
        assert s not in _stored_text()
    (root / "again.json").write_bytes(FIX.read_bytes())
    os.utime(root / "again.json", (old, old))
    assert {f["file"]: f for f in scan(now=now)["files"]}["again.json"]["status"] == "duplicate"

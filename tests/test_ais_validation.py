"""#216: the AIS check is "Experimental" until a saved import parses with no unrecognised rows and matching totals.
Synthetic data only (fake names, ISINs and amounts)."""

from __future__ import annotations

import base64
import json

import pytest
from fastapi.testclient import TestClient

ORIGIN = {"Origin": "http://127.0.0.1:3000", "X-FinResearch": "1"}
APP = [  # bought in FY 2024-25, so FY 2025-26 has only the dividend to compare
    {"name": "Example Ltd", "isin": "INE000X01011", "day": "2024-05-02", "kind": "buy", "quantity": 20, "price": 1000},
    {"name": "Example Ltd", "isin": "INE000X01011", "day": "2025-07-10", "kind": "dividend", "amount": 1500},
]  # fmt: skip
DIVIDEND = {"infoCode": "TDS-194", "infoDesc": "Dividend", "infoSrc": "EXAMPLE LIMITED (MUMA12345B)",
            "amountReported": "1500.00"}  # fmt: skip
SALARY = {"infoCode": "TDS-192", "infoDesc": "Salary", "infoSrc": "SOME EMPLOYER (MUMS54321C)",
          "amountReported": "900000.00"}  # understood (a TDS section), just not compared  # fmt: skip
ODD = {"infoCode": "ZZ-NEW", "infoDesc": "A brand new information category", "amountReported": "5.00"}


def _ais(*rows: dict) -> bytes:
    return json.dumps({"financialYear": "2025-26", "tdsTcsInfo": list(rows)}).encode()


@pytest.fixture
def client(env):
    from sqlalchemy import text

    from finresearch.api import create_app
    from finresearch.db import session_scope

    with session_scope() as s:
        s.execute(text("TRUNCATE portfolio_disposal, portfolio_lot, portfolio_txn, portfolio_holding, "
                       "portfolio_import, portfolio_ais, portfolio_setting"))  # fmt: skip
    with TestClient(create_app()) as c:
        for t in APP:
            assert c.post("/api/portfolio/transactions", headers=ORIGIN, json=t).status_code == 201
        yield c


def _post(c, blob: bytes, **kw):
    return c.post("/api/portfolio/ais/import", headers=ORIGIN,
                  json={"filename": "ais.json", "content_b64": base64.b64encode(blob).decode(), **kw})  # fmt: skip


def test_unknown_code_counts_as_unrecognised_but_a_tds_section_does_not():
    from finresearch.portfolio.ais import parse_ais_json

    st = parse_ais_json(_ais(DIVIDEND, SALARY, ODD))
    assert (len(st.items), st.ignored, st.unrecognised) == (1, 2, 1)


def test_experimental_until_a_clean_saved_import(client):
    assert client.get("/api/portfolio/ais").json()["validated"] == {}
    assert _post(client, _ais(DIVIDEND, SALARY)).status_code == 200  # a dry run never validates
    assert client.get("/api/portfolio/ais").json()["validated"] == {}
    r = _post(client, _ais(DIVIDEND, SALARY), dry_run=False)
    assert r.status_code == 200, r.text
    assert r.json()["check"]["totals"]["dividend"] == {"ais": 1500.0, "app": 1500.0}
    v = client.get("/api/portfolio/ais").json()["validated"]
    assert set(v) == {"json"} and v["json"]["fy"] == 2026 and v["json"]["validated_at"]
    # a later import that would not validate never clears the flag
    assert _post(client, _ais(DIVIDEND, ODD), dry_run=False).status_code == 200
    assert client.get("/api/portfolio/ais").json()["validated"] == v


@pytest.mark.parametrize(("rows", "why"), [
    ((DIVIDEND, ODD), "1 row(s) could not be recognised"),
    (({**DIVIDEND, "amountReported": "1600.00"},), "dividend totals differ"),
    ((SALARY,), "no dividend, sale or purchase row in the AIS to compare"),
])  # fmt: skip
def test_not_validated_with_unrecognised_rows_mismatched_totals_or_nothing_compared(client, rows, why):
    from finresearch.db import session_scope
    from finresearch.portfolio.ais import parse_ais_json
    from finresearch.portfolio.ais_store import check, validation_gaps

    assert _post(client, _ais(*rows), dry_run=False).status_code == 200
    assert client.get("/api/portfolio/ais").json()["validated"] == {}
    st = parse_ais_json(_ais(*rows))
    with session_scope() as s:
        assert why in validation_gaps(st, check(s, 2026, st.items))

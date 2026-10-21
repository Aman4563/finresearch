"""Carry-forward inputs and ledger through the API (#344). Synthetic data only; values hand-computed beside them.

TODAY is 30-Sep-2026 (FY 2026-27). A share bought 1-Jan-2020 and sold 1-Oct-2024 at a ₹50,000 loss gives an LTCL in
FY 2024-25 (fy 2025); a second sold 1-Oct-2025 at a ₹2,00,000 gain is LTCG in FY 2025-26 (fy 2026).
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from test_portfolio import ORIGIN, client  # noqa: F401

from finresearch.db import session_scope
from finresearch.suggest.profile import ADVISOR_FIELDS, PRIVATE_FIELDS


@pytest.fixture
def api(client):  # noqa: F811
    with session_scope() as s:
        s.execute(text("TRUNCATE investor_profile"))
    for name, buy, sell, day in (("Example Loss Ltd", "150000", "100000", "2024-10-01"),
                                 ("Example Gain Ltd", "100000", "300000", "2025-10-01")):  # fmt: skip
        hid = client.post("/api/portfolio/transactions", headers=ORIGIN,
                          json={"asset_type": "stock", "name": name, "account": "Manual", "day": "2020-01-01",
                                "kind": "buy", "quantity": "1", "price": buy}).json()["holding_id"]  # fmt: skip
        r = client.post("/api/portfolio/transactions", headers=ORIGIN,
                        json={"holding_id": hid, "day": day, "kind": "sell", "quantity": "1", "price": sell})  # fmt: skip
        assert r.status_code == 201
    return client


def _fy(tax, fy):
    return next(x for x in tax["fys"] if x["fy"] == fy)


def test_unknown_filing_is_shown_as_unknown_until_the_user_answers(api):
    tax = api.get("/api/portfolio/tax").json()
    (e,) = tax["carry_forward"]["entries"]
    assert (e["origin_fy"], e["kind"], e["amount"], e["status"], e["last_fy"]) == (2025, "long", 50000.0,
                                                                                    "unknown_filing", 2033)  # fmt: skip
    y = _fy(tax, 2026)
    # loss not applied: (2,00,000 - 1,25,000) × 12.5 % = 9,375 + 375 cess = 9,750; if allowed 3,125 + 125 = 3,250
    assert y["total"] == 9750.0 and y["brought_forward"]["complete"] is False
    assert y["brought_forward"]["tax_if_pending_allowed"] == 3250.0
    assert not any("not included" in c for c in tax["caveats"])

    r = api.put("/api/portfolio/tax/losses", headers=ORIGIN, json={"returns_filed_on_time": {"2025": True}})
    assert r.status_code == 200 and r.json()["returns_filed_on_time"] == {"2025": True}
    tax = api.get("/api/portfolio/tax").json()
    assert _fy(tax, 2026)["total"] == 3250.0 and tax["carry_forward"]["entries"][0]["status"] == "used"


def test_manual_brought_forward_loss_with_source(api):
    body = {"returns_filed_on_time": {"2023": True},
            "brought_forward": [{"origin_fy": 2023, "kind": "short", "amount_inr": "25000",
                                 "source": "ITR AY 2023-24 Schedule CFL"}]}  # fmt: skip
    assert api.put("/api/portfolio/tax/losses", headers=ORIGIN, json=body).status_code == 200
    assert (
        api.get("/api/portfolio/tax/losses").json()["brought_forward"][0]["source"]
        == "ITR AY 2023-24 Schedule CFL"
    )
    tax = api.get("/api/portfolio/tax").json()
    m = next(e for e in tax["carry_forward"]["entries"] if e["origin_fy"] == 2023)
    # FY 2024-25 has only a loss, so the 25,000 STCL is first used in FY 2025-26 against the LTCG
    assert (m["source"], m["status"], m["used"]) == ("ITR AY 2023-24 Schedule CFL", "used", {"2026": 25000.0})
    # profile is otherwise untouched by this endpoint
    assert api.get("/api/profile").json()["tax_slab_pct"] in ("30", 30, "30.0")


@pytest.mark.parametrize("bad", [
    {"brought_forward": [{"origin_fy": 2023, "kind": "short", "amount_inr": "0", "source": "x"}]},
    {"brought_forward": [{"origin_fy": 2023, "kind": "short", "amount_inr": "10", "source": ""}]},
    {"brought_forward": [{"origin_fy": 2023, "kind": "other", "amount_inr": "10", "source": "x"}]},
    {"brought_forward": [{"origin_fy": 2015, "kind": "long", "amount_inr": "10", "source": "x", "apply_from_fy": 2024}]},
    {"brought_forward": [{"origin_fy": 2023, "kind": "long", "amount_inr": "10", "source": "x"},
                         {"origin_fy": 2023, "kind": "long", "amount_inr": "20", "source": "y"}]},
])  # fmt: skip
def test_bad_manual_losses_are_rejected(api, bad):
    assert api.put("/api/portfolio/tax/losses", headers=ORIGIN, json=bad).status_code == 422


def test_tax_losses_are_private_and_never_sent_to_a_model():
    assert "tax_losses" in PRIVATE_FIELDS and "tax_losses" not in ADVISOR_FIELDS

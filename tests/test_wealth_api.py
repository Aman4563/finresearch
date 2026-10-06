"""/api/wealth end to end on synthetic household data (test database only), plus privacy, wording and migration."""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal as D
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from finresearch.wealth import calc

TODAY = date(2026, 9, 30)
ORIGIN = {"Origin": "http://127.0.0.1:3000", "X-FinResearch": "1"}
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def client(env):
    from sqlalchemy import text

    from finresearch.api import create_app
    from finresearch.db import session_scope

    with session_scope() as s:
        s.execute(text("TRUNCATE wealth_valuation, wealth_asset, wealth_loan, wealth_goal, wealth_policy, "
                       "portfolio_snapshot, portfolio_setting, investor_profile"))  # fmt: skip
    app = create_app()
    app.state.wealth_today = TODAY
    with TestClient(app) as c:
        yield c


def post(c, path, body):
    r = c.post(path, json=body, headers=ORIGIN)
    assert r.status_code == 200, r.text
    return r.json()


def strings(x):
    if isinstance(x, str):
        yield x
    elif isinstance(x, dict):
        for k, v in x.items():
            yield str(k)
            yield from strings(v)
    elif isinstance(x, list):
        for v in x:
            yield from strings(v)


def seed(c):
    from finresearch.db import session_scope
    from finresearch.db.models import PortfolioSnapshot

    with session_scope() as s:
        s.add(PortfolioSnapshot(day=date(2026, 9, 29), value=D(1_000_000), invested=D(900_000),
                                by_asset={"Equity funds": 800_000, "Debt funds": 200_000}, complete=True))  # fmt: skip
    r = c.put("/api/wealth/household", headers=ORIGIN, json={
        "age": 35, "monthly_income_inr": "150000", "monthly_expenses_inr": "60000", "dependants": 2, "earners": 1})  # fmt: skip
    assert r.status_code == 200, r.text
    cash = post(c, "/api/wealth/assets", {"kind": "cash", "name": "Savings", "institution": "HDFC Bank",
                                          "value": "300000", "value_date": "2026-09-01"})  # fmt: skip
    fd = post(c, "/api/wealth/assets", {"kind": "fd", "name": "FD", "institution": "HDFC Bank Ltd", "principal": "400000",
                                        "rate_pct": "7", "start_date": "2025-09-30", "maturity_date": "2027-09-30",
                                        "liquid": True})  # fmt: skip
    epf = post(c, "/api/wealth/assets", {"kind": "epf", "name": "EPF", "rate_pct": "8.25", "value": "500000",
                                         "value_date": "2026-03-31"})  # fmt: skip
    home = post(c, "/api/wealth/assets", {"kind": "real_estate", "name": "Flat", "value": "5000000",
                                          "value_date": "2026-01-01"})  # fmt: skip
    loan = post(c, "/api/wealth/loans", {"kind": "home", "name": "Home loan", "lender": "SBI", "principal": "3000000",
                                         "rate_pct": "8.5", "tenure_months": 240, "start_date": "2024-09-30"})  # fmt: skip
    post(c, "/api/wealth/policies", {"kind": "term", "name": "Term plan", "cover_inr": "10000000"})
    goal = post(c, "/api/wealth/goals", {"name": "Child education", "target_inr": "2500000", "target_date": "2038-06-30",
                                         "priority": "high",
            "in_cover": True, "monthly_sip": "10000", "step_up_pct": "10",
                                         "linked_asset_ids": [fd["id"]], "portfolio_pct": "20"})  # fmt: skip
    return {"cash": cash, "fd": fd, "epf": epf, "home": home, "loan": loan, "goal": goal}


def test_term_cover_counts_goal_money_once(client):
    """A ₹10 lakh goal funded by a linked ₹4 lakh cash balance, no other money, no loans, no expenses to replace:
    the family needs ₹10 lakh for the goal and has ₹4 lakh, so the cover gap is ₹6 lakh. (Netting the linked cash
    from the goal and again from the assets gave ₹2 lakh.)"""
    r = client.put("/api/wealth/household", headers=ORIGIN, json={
        "age": 35, "monthly_expenses_inr": "60000", "support_years": 0})  # fmt: skip
    assert r.status_code == 200, r.text
    cash = post(client, "/api/wealth/assets", {"kind": "cash", "name": "Savings", "value": "400000",
                                               "value_date": "2026-09-01"})  # fmt: skip
    post(client, "/api/wealth/goals", {"name": "House deposit", "target_inr": "1000000", "target_date": "2030-06-30",
                                       "in_cover": True, "linked_asset_ids": [cash["id"]]})  # fmt: skip
    t = client.get("/api/wealth").json()["insurance"]["term"]
    assert t["expenses_pv"] == 0 and t["loans"] == 0 and t["existing"] == 0
    assert t["goal_gaps"] == pytest.approx(1_000_000) and t["assets"] == pytest.approx(400_000)
    assert t["gap"] == pytest.approx(600_000)


def test_empty_household_asks_for_inputs(client):
    w = client.get("/api/wealth").json()
    assert w["net_worth"]["net_worth"] == 0 and w["history"] == []
    assert w["portfolio"]["day"] is None
    assert "Add your monthly essential expenses" in w["emergency"]["message"]
    assert w["insurance"]["term"] is None
    assert "not investment advice" in w["disclaimer"].lower() or "Not investment advice" in w["disclaimer"]


def test_net_worth_and_checks(client):
    ids = seed(client)
    w = client.get("/api/wealth").json()
    fd = float(calc.fd_value(400_000, 7, date(2025, 9, 30), TODAY))
    epf = float(calc.provident_value(500_000, "8.25", date(2026, 3, 31), TODAY))
    e = calc.emi(3_000_000, "8.5", 240)
    loan = float(calc.outstanding(3_000_000, "8.5", e, 24))
    nw = w["net_worth"]
    assert nw["portfolio"] == 1_000_000 and nw["portfolio_day"] == "2026-09-29"
    assert nw["manual"] == pytest.approx(300_000 + fd + epf + 5_000_000, abs=0.02)
    assert nw["liabilities"] == pytest.approx(loan, abs=0.01)
    assert nw["net_worth"] == pytest.approx(1_000_000 + 300_000 + fd + epf + 5_000_000 - loan, abs=0.05)
    assert nw["liquid_net_worth"] == pytest.approx(
        nw["net_worth"] - 5_000_000 - epf + loan, abs=0.05
    )  # home loan left out

    # DICGC: "HDFC Bank" and "HDFC Bank Ltd" are one bank; 3 L + FD > 5 L
    hdfc = w["emergency"]["banks"][0]
    assert hdfc["over_limit"] and hdfc["total"] == pytest.approx(300_000 + fd, abs=0.01)
    assert hdfc["uninsured"] == pytest.approx(300_000 + fd - 500_000, abs=0.01)
    # emergency: cash + 90 % of the breakable FD, over ₹60,000 a month; single earner with dependants -> 12
    assert w["emergency"]["months"] == pytest.approx((300_000 + 0.9 * fd) / 60_000, rel=1e-9)
    assert w["emergency"]["target_months"] == 12
    # debt: EMI / income
    assert w["debt"]["foir_pct"] == pytest.approx(float(e) / 150_000 * 100)
    pv = w["loans"][0]["prepay_vs_invest"]
    assert pv["deductible_share"] == 0 and pv["after_tax_loan_pct"] == 8.5  # new regime by default
    # allocation: glide for 35 / medium = 65 % equity
    assert w["allocation"]["target"]["Equity"] == 65
    assert w["allocation"]["by_class"]["Real estate"] == 5_000_000
    assert w["allocation"]["by_class"]["Equity"] == 800_000
    # term cover: needs method uses 25 years to retirement and includes the loan and the high-priority goal gap
    t = w["insurance"]["term"]
    assert t["years"] == 25 and t["loans"] == pytest.approx(loan, abs=0.01) and t["existing"] == 10_000_000
    # the goal marked for cover: its full target, because the linked FD and the portfolio share that fund it are
    # already in the financial assets the cover deducts (netting them here too would count them twice)
    assert t["goal_gaps"] == pytest.approx(2_500_000, abs=0.01)
    # goals: funding = linked FD + 20 % of the portfolio
    g = w["goals"][0]
    assert g["funded_now"] == pytest.approx(fd + 200_000, abs=0.01)
    # history: month ends from the first dated entry (loan start Sep-2024) to today
    assert w["history"][0]["date"] == "2024-09-30" and w["history"][-1]["date"] == "2026-09-30"
    assert w["history"][-1]["net_worth"] == nw["net_worth"]
    early = w["history"][0]
    assert early["portfolio"] == 0 and early["manual"] == 0 and early["liabilities"] == 3_000_000

    # the goal plan is reproducible for a seed and changes with it
    a = client.get(f"/api/wealth/goals/{ids['goal']['id']}/plan?n=1000").json()
    b = client.get(f"/api/wealth/goals/{ids['goal']['id']}/plan?n=1000").json()
    assert a == b and a["n"] == 1000 and 0 <= a["p_success"] <= 1
    c = client.get(f"/api/wealth/goals/{ids['goal']['id']}/plan?n=1000&seed=99").json()
    assert c["seed"] == 99 and c["terminal_pcts"] != a["terminal_pcts"]
    assert a["start"] == pytest.approx(fd + 200_000, abs=0.01)

    # no "you should" anywhere in what the page shows
    for text in [*strings(w), *strings(a)]:
        assert not re.search(r"\byou should\b", text, re.I), text


def test_crud_validation_and_unlinking(client):
    ids = seed(client)
    bad = client.post(
        "/api/wealth/assets", headers=ORIGIN, json={"kind": "fd", "name": "x", "principal": "1"}
    )
    assert bad.status_code == 422
    bad = client.post("/api/wealth/loans", headers=ORIGIN, json={"kind": "car", "name": "c", "principal": "500000",
                      "rate_pct": "12", "tenure_months": 60, "emi": "100", "start_date": "2026-01-01"})  # fmt: skip
    assert bad.status_code == 422
    bad = client.post("/api/wealth/goals", headers=ORIGIN, json={"name": "g", "target_inr": "1", "target_date": "2030-01-01",
                                                                 "linked_asset_ids": [99999]})  # fmt: skip
    assert bad.status_code == 422
    assert (
        client.post(
            "/api/wealth/assets",
            headers={"Origin": "http://evil.example"},
            json={"kind": "cash", "name": "x", "value": "1"},
        ).status_code
        == 403
    )
    # a dated value, then deleting the asset unlinks it from the goal
    r = client.post(
        f"/api/wealth/assets/{ids['cash']['id']}/values",
        headers=ORIGIN,
        json={"day": "2026-09-30", "value": "350000"},
    )
    assert r.status_code == 200
    w = client.get("/api/wealth").json()
    cash = next(a for a in w["assets"] if a["id"] == ids["cash"]["id"])
    assert cash["valuation"]["value"] == 350_000 and len(cash["history"]) == 2
    assert client.delete(f"/api/wealth/assets/{ids['fd']['id']}", headers=ORIGIN).status_code == 200
    g = client.get("/api/wealth").json()["goals"][0]
    assert g["linked_asset_ids"] == []
    # update a loan with a statement balance
    body = {
        **{k: v for k, v in ids["loan"].items() if k != "id"},
        "outstanding": "2800000",
        "outstanding_as_of": "2026-09-01",
    }
    r = client.put(f"/api/wealth/loans/{ids['loan']['id']}", headers=ORIGIN, json=body)
    assert r.status_code == 200, r.text
    ln = client.get("/api/wealth").json()["loans"][0]
    assert ln["outstanding_now"] == 2_800_000  # no whole month since the statement date
    assert client.delete(f"/api/wealth/goals/{ids['goal']['id']}", headers=ORIGIN).status_code == 200
    assert client.get(f"/api/wealth/goals/{ids['goal']['id']}/plan").status_code == 404


def test_assumptions_edit_and_reset(client):
    r = client.put(
        "/api/wealth/assumptions", headers=ORIGIN, json={"equity": {"mu_pct": 10, "sigma_pct": 16}, "seed": 5}
    )
    assert r.status_code == 200 and r.json()["equity"]["mu_pct"] == 10 and r.json()["seed"] == 5
    assert client.put("/api/wealth/assumptions", headers=ORIGIN, json={"n": 10}).status_code == 422
    r = client.put("/api/wealth/assumptions", headers=ORIGIN, json={})
    assert r.json()["equity"]["mu_pct"] == 11 and r.json()["seed"] == 20260930


def test_household_is_private_and_survives_a_profile_save(client):
    from finresearch.suggest.profile import UI_FIELDS, Profile

    client.put("/api/wealth/household", headers=ORIGIN, json={"age": 40, "monthly_income_inr": "123456"})
    prof = client.get("/api/profile").json()
    assert prof["household"]["monthly_income_inr"] == "123456"
    # the advisor prompt carries only the ADVISOR_FIELDS allow-list (#215): the household never reaches the LLM
    from finresearch.suggest.advisor import advisor_profile
    from finresearch.suggest.profile import ADVISOR_FIELDS

    assert "household" in UI_FIELDS and "household" not in ADVISOR_FIELDS
    dumped = advisor_profile(Profile.model_validate(prof))
    assert "household" not in dumped and "123456" not in str(dumped)
    stored = Profile.model_validate(prof).model_dump(
        mode="json", exclude=UI_FIELDS
    )  # the decision's stored inputs
    assert "household" not in stored and "123456" not in str(stored)
    # the profile page PUTs the whole profile back: the household survives
    assert client.put("/api/profile", headers=ORIGIN, json=prof).status_code == 200
    assert client.get("/api/wealth/household").json()["age"] == 40


def test_no_instructions_in_wealth_sources():
    for p in (ROOT / "src/finresearch/wealth").glob("*.py"):
        assert not re.search(r"\byou should\b", p.read_text(), re.I), p


def test_migration_matches_models_and_is_the_single_head():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    from finresearch.db.models import Base

    script = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    assert len(script.get_heads()) == 1
    rev = script.get_revision("c6e2a4f8b1d3")
    src = Path(rev.path).read_text()
    created = set(re.findall(r'op\.create_table\(\s*"(\w+)"', src))
    assert created == {t for t in Base.metadata.tables if t.startswith("wealth_")}
    dropped = set(re.findall(r'op\.drop_table\("(\w+)"\)', src))
    assert dropped == created

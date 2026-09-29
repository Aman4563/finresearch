"""Personal suggestions: rules on live data, lot limits, enforcement over the agent, and the decision journal."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from finresearch.suggest.advisor import Suggestion, enforce
from finresearch.suggest.profile import Profile, Rule, default_profile
from finresearch.suggest.rules import (
    Inputs,
    Metric,
    bidding_days_left,
    evaluate,
    lot_limits,
    subscription_metrics,
)

FIX = Path(__file__).parent / "fixtures" / "nse"


def live_orient():
    from finresearch.adapters.nse import parse_ipo_detail

    return parse_ipo_detail(
        "ORIENTCABL", json.loads((FIX / "ipo_detail_ORIENTCABL_20260928_1636_live.json").read_text())
    )


def test_subscription_metrics_from_the_live_nse_table():
    m = subscription_metrics(live_orient())
    assert round(m["qib_times"].value, 2) == Decimal("0.05") and round(m["nii_times"].value, 2) == Decimal(
        "15.78"
    )
    assert round(m["rii_times"].value, 2) == Decimal("8.67") and m["qib_times"].as_of.startswith(
        "2026-09-28T16:36"
    )


def test_rules_fire_clear_or_stay_unknown():
    metrics = {"qib_times": Metric(Decimal("0.05"), "nse"), "nii_times": Metric(None, "nse")}
    qib = Rule(id="q", metric="qib_times", op="<", value=Decimal(1))
    assert evaluate(qib, metrics).status == "fired"
    assert evaluate(Rule(id="q2", metric="qib_times", op=">=", value=Decimal(1)), metrics).status == "clear"
    assert evaluate(Rule(id="n", metric="nii_times", op="<", value=Decimal(2)), metrics).status == "unknown"
    assert evaluate(Rule(id="t", metric="total_times", op="<", value=Decimal(2)), metrics).status == "unknown"


def test_lot_limits_by_capital_and_sebi_category_caps():
    lot_cost = Decimal(272 * 55)  # Orient Cables: ₹14,960 a lot
    assert lot_limits(Profile(capital_per_ipo_inr=Decimal(15000)), lot_cost) == {"by_capital": 1, "by_category": 13,
                                                                                "min_lots": 1}  # fmt: skip
    shni = lot_limits(Profile(capital_per_ipo_inr=Decimal(500000), category="shni"), lot_cost)
    assert shni == {"by_capital": 33, "by_category": 66, "min_lots": 14}  # above ₹2 lakh: 14 lots = ₹2,09,440
    assert lot_limits(Profile(), None)["by_capital"] is None


def test_bidding_days_left_counts_exchange_days_inclusive():
    assert bidding_days_left(date(2026, 9, 28), date(2026, 9, 29)) == 2  # Mon and Tue
    assert bidding_days_left(date(2026, 9, 26), date(2026, 9, 29)) == 2  # Saturday: Mon and Tue left
    assert bidding_days_left(date(2026, 9, 30), date(2026, 9, 29)) == 0


def _agent(**kw) -> Suggestion:
    base = {"action": "APPLY", "category": "retail", "lots": 3, "conditions": [], "exit_plan": "sell on listing",
            "watch": [], "rationale_markdown": "x", "confidence": "medium"}  # fmt: skip
    return Suggestion.model_validate({**base, **kw})


def _inputs(profile: Profile, metrics: dict[str, Metric]) -> Inputs:
    return Inputs(metrics=metrics, rules=[evaluate(r, metrics) for r in profile.rules])


def test_a_fired_skip_rule_overrides_the_agent():
    p = default_profile()
    inputs = _inputs(p, {"qib_times": Metric(Decimal("0.05"), "nse"), "gate_ok": Metric(Decimal(1), "gate"),
                         "max_lots_by_capital": Metric(Decimal(1), "fincalc")})  # fmt: skip
    out = enforce(_agent(), inputs, {"by_capital": 1, "by_category": 13, "min_lots": 1}, p)
    assert (
        out["action"] == "SKIP" and out["lots"] == 0 and "rule qib-floor fired" in out["enforcement_notes"][0]
    )
    assert out["agent"]["action"] == "APPLY"  # the agent's own view is kept for the journal


def test_unknown_skip_rules_make_apply_conditional_and_lots_are_capped():
    p = default_profile()
    inputs = _inputs(
        p, {"gate_ok": Metric(Decimal(1), "gate"), "max_lots_by_capital": Metric(Decimal(1), "fincalc")}
    )
    out = enforce(_agent(lots=3), inputs, {"by_capital": 1, "by_category": 13, "min_lots": 1}, p)
    assert out["action"] == "APPLY-CONDITIONAL" and out["lots"] == 1
    assert any("qib_times < 1" in c for c in out["conditions"])
    assert any("lots reduced from 3 to 1" in n for n in out["enforcement_notes"])


def test_capital_below_the_category_minimum_is_a_skip():
    p = Profile(capital_per_ipo_inr=Decimal(150000), category="shni", rules=[])
    out = enforce(
        _agent(category="shni", lots=14), Inputs(), {"by_capital": 10, "by_category": 66, "min_lots": 14}, p
    )
    assert out["action"] == "SKIP" and out["lots"] == 0


def test_warn_rules_are_reported():
    p = Profile(rules=[Rule(id="hot", description="NII very hot", metric="nii_times", op=">", value=Decimal(10),
                            action="warn")])  # fmt: skip
    out = enforce(_agent(lots=1), _inputs(p, {"nii_times": Metric(Decimal("15.8"), "nse")}),
                  {"by_capital": 1, "by_category": 13, "min_lots": 1}, p)  # fmt: skip
    assert out["action"] == "APPLY" and out["warnings"] == ["rule hot: NII very hot"]


# --------------------------------------------------------------------------- API
@pytest.fixture
def api(env, tmp_path):
    from fastapi.testclient import TestClient

    from finresearch.api import create_app
    from finresearch.bridge.types import AgentResult, Tier
    from finresearch.db import session_scope
    from finresearch.db.models import AgentStep, Claim, ResearchRun
    from finresearch.ingest.documents import get_or_create_company

    class Router:
        def __init__(self):
            self.tasks = []

        async def run(self, task):
            self.tasks.append(task)
            out = {"action": "APPLY", "category": "retail", "lots": 2, "conditions": ["UPI before 17:00"],
                   "exit_plan": "sell on listing day", "watch": ["listing 5-Oct"],
                   "rationale_markdown": "Retail 8.67x", "confidence": "medium"}  # fmt: skip
            return AgentResult(
                task_name=task.name, tier=Tier.CLAUDE_MAX, model="opus", ok=True, structured_output=out
            )

    from finresearch.db.models import InvestorProfile

    with session_scope() as s:
        s.query(InvestorProfile).delete()  # every test starts from the default profile
        co = get_or_create_company(s, "sug-" + tmp_path.name[-8:], "Sug Co")
        co.nse_symbol = "ORIENTCABL"
        run = ResearchRun(company_id=co.id, kind="ipo_report", status="done",
                          manifest={"facts": {"issue_close": "2026-09-29"}})  # fmt: skip
        s.add(run)
        s.flush()
        for metric, value, unit in (("price_band_upper", 272, "INR per share"), ("lot_size", 55, "shares")):
            s.add(Claim(run_id=run.id, stream="facts", statement=metric, claim_type="numeric", metric=metric,
                        value=Decimal(value), unit=unit, period="offer", status="verified"))  # fmt: skip
        s.add(AgentStep(run_id=run.id, key="synthesis", stage="synthesis", role="synthesizer", status="done",
                        output={"report_markdown": "# Sug\\nNo figures.", "overall_verdict": "APPLY-CONDITIONAL"}))  # fmt: skip
        run_id = run.id
    router = Router()

    async def fetch(symbol):
        return live_orient()

    with TestClient(create_app(router=router, live_fetch=fetch)) as c:
        c.router, c.run_id = router, run_id
        yield c


def test_profile_round_trip_and_validation(api):
    p = api.get("/api/profile").json()
    assert p["category"] == "retail" and {r["id"] for r in p["rules"]} == {"qib-floor", "gate", "one-lot"}
    p["capital_per_ipo_inr"] = "30000"
    assert api.put("/api/profile", json=p).json()["capital_per_ipo_inr"] == "30000"
    assert api.get("/api/profile").json()["capital_per_ipo_inr"] == "30000"
    bad = {**p, "rules": [p["rules"][0], p["rules"][0]]}
    assert api.put("/api/profile", json=bad).status_code == 422
    assert api.put("/api/profile", json={**p, "category": "whale"}).status_code == 422


def test_profile_identity_and_preferences_are_optional_and_validated(api):
    from finresearch.db import session_scope
    from finresearch.db.models import InvestorProfile
    from finresearch.suggest.advisor import PROFILE_NAME

    # a profile saved before the identity fields existed still loads, with defaults
    old = {k: v for k, v in api.get("/api/profile").json().items()
           if k not in ("display_name", "avatar_color", "preferences")}  # fmt: skip
    with session_scope() as s:
        s.add(InvestorProfile(name=PROFILE_NAME, data=old))
    p = api.get("/api/profile").json()
    assert p["display_name"] == "" and p["avatar_color"] is None
    assert p["preferences"] == {"default_landing": "/", "number_format": "lakh_crore", "compact_tables": False,
                                "reduce_motion": False}  # fmt: skip
    assert api.put("/api/profile", json=old).status_code == 200  # an old client's body is still accepted

    p.update(
        display_name="  Aman   Yadav ",
        avatar_color="accent",
        preferences={"default_landing": "/ipos", "number_format": "million", "reduce_motion": True},
    )
    saved = api.put("/api/profile", json=p).json()
    assert saved["display_name"] == "Aman Yadav" and saved["avatar_color"] == "accent"
    assert (
        saved["preferences"]["default_landing"] == "/ipos" and saved["preferences"]["compact_tables"] is False
    )
    assert api.get("/api/profile").json()["preferences"]["number_format"] == "million"
    for bad in (
        {"avatar_color": "purple"},
        {"display_name": "x" * 61},
        {"preferences": {"number_format": "billion"}},
        {"preferences": {"default_landing": "/admin"}},
    ):
        assert api.put("/api/profile", json={**p, **bad}).status_code == 422, bad


def test_advisor_prompt_leaves_out_identity_and_preferences(api):
    p = api.get("/api/profile").json()
    api.put("/api/profile", json={**p, "display_name": "Zed Quux", "preferences": {"compact_tables": True}})
    d = api.post(f"/api/runs/{api.run_id}/suggest").json()
    prompt = api.router.tasks[-1].system_prompt
    assert "Zed Quux" not in prompt and "compact_tables" not in prompt and '"capital_per_ipo_inr"' in prompt
    assert "display_name" not in d["inputs"]["profile"]


def test_profile_stats_counts_activity(api):
    st = api.get("/api/profile/stats").json()
    assert st["runs"] >= 1 and st["runs_done"] >= 1 and st["first_run_at"]
    assert {"decisions", "applied", "watches", "active_watches", "profile_updated_at"} <= set(st)
    before = st["decisions"]
    api.post(f"/api/runs/{api.run_id}/suggest")
    assert api.get("/api/profile/stats").json()["decisions"] == before + 1


def test_suggest_enforces_the_live_qib_rule_and_journals_the_outcome(api):
    d = api.post(f"/api/runs/{api.run_id}/suggest").json()
    # live day-2 QIB 0.05x fires the default qib-floor rule, whatever the agent said
    assert d["action"] == "SKIP" and d["lots"] == 0 and d["suggestion"]["agent"]["action"] == "APPLY"
    rules = {r["rule"]["id"]: r["status"] for r in d["inputs"]["rules"]}
    assert rules == {"qib-floor": "fired", "gate": "clear", "one-lot": "clear"}
    assert d["inputs"]["metrics"]["lot_cost"]["value"] == "14960"
    task = api.router.tasks[0]
    assert "mcp__finresearch__save_claim" not in task.allowed_tools and "WebSearch" not in task.allowed_tools
    assert '"qib_times"' in task.system_prompt

    upd = api.patch(f"/api/decisions/{d['id']}", json={"user_action": "applied", "applied_lots": 1, "allotted_lots": 1,
                                                        "issue_price": "272", "listing_price": "300",
                                                        "exit_price": "310", "exit_date": "2026-10-05"}).json()  # fmt: skip
    assert upd["outcome"] == {"listing_gain_pct": "10.29", "exit_return_pct": "13.97", "profit_inr": "2090.00",
                              "followed_suggestion": False}  # fmt: skip
    assert api.get("/api/decisions", params={"run_id": api.run_id}).json()[0]["company_name"] == "Sug Co"
    assert api.patch("/api/decisions/999999", json={}).status_code == 404


def test_suggest_without_live_data_turns_rules_into_conditions(api, monkeypatch):
    from finresearch.api import create_app

    async def offline(symbol):
        return None

    from fastapi.testclient import TestClient

    with TestClient(create_app(router=api.router, live_fetch=offline)) as c:
        d = c.post(f"/api/runs/{api.run_id}/suggest").json()
    assert d["action"] == "APPLY-CONDITIONAL" and d["lots"] == 1  # ₹15,000 covers one ₹14,960 lot
    assert any("qib_times < 1" in x for x in d["suggestion"]["conditions"])
    assert datetime.fromisoformat(d["inputs"]["at"]).tzinfo is not None


def test_lot_and_price_fall_back_to_the_nse_issue_information(env, tmp_path):
    from finresearch.db import session_scope
    from finresearch.db.models import ResearchRun
    from finresearch.suggest.rules import gather

    info = {"Price Range": "Rs.258 to Rs.272", "Bid Lot": "55 Equity Shares and in multiples thereof"}
    with session_scope() as s:
        run = ResearchRun(kind="ipo_report", status="done", manifest={"facts": {"issue_info": info,
                                                                                 "issue_close": "2026-09-29"}})  # fmt: skip
        s.add(run)
        s.flush()
        rid = run.id
        inputs = gather(
            s, rid, default_profile(), live_detail=None, now=datetime(2026, 9, 28, 17), gate_ok=True
        )
    m = inputs.metrics
    assert (m["price_band_upper"].value, m["lot_size"].value, m["lot_cost"].value) == (272, 55, 14960)
    assert m["lot_size"].source == "NSE issue information (run facts)" and m["max_lots_by_capital"].value == 1
    assert {r.rule.id: r.status for r in inputs.rules}["one-lot"] == "clear"
    assert m["bidding_days_left"].value == 2
    with session_scope() as s:  # 01:30 IST on 30-Sep (still 29-Sep in UTC): the issue has closed
        late = gather(s, rid, default_profile(), live_detail=None, now=datetime(2026, 9, 29, 20, 0, tzinfo=UTC),
                      gate_ok=True)  # fmt: skip
    assert late.metrics["bidding_days_left"].value == 0


async def test_suggest_refuses_runs_that_are_not_ipo_reports(env, tmp_path):
    from finresearch.db import session_scope
    from finresearch.db.models import AgentStep, ResearchRun
    from finresearch.ingest.documents import get_or_create_company
    from finresearch.suggest.advisor import suggest

    with session_scope() as s:
        co = get_or_create_company(s, "mf-" + tmp_path.name[-8:], "A fund")
        run = ResearchRun(company_id=co.id, kind="fund_report", status="done", manifest={})
        s.add(run)
        s.flush()
        s.add(AgentStep(run_id=run.id, key="synthesis", stage="synthesis", role="synthesizer", status="done",
                        output={"report_markdown": "# Fund", "verdict": "HOLD"}))  # fmt: skip
        run_id = run.id

    async def fetch(symbol):
        raise AssertionError("no live IPO data for a fund")

    with pytest.raises(ValueError, match="IPO"):
        await suggest(run_id, router=object(), fetch=fetch)

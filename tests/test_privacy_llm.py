"""#215: personal financial data never reaches a model. With a synthetic portfolio, journal, AIS, wealth records and
household profile in the database, every AgentTask the code can build (each research role, the IPO advisor, Ask
about this report) and the MCP tools the agents call are checked for the synthetic markers. Made-up names, fake
ISINs and amounts only."""

from __future__ import annotations

import ast
from datetime import date
from decimal import Decimal as D
from pathlib import Path

import pytest

SRC = Path(__file__).parents[1] / "src" / "finresearch"
ORIGIN = {"Origin": "http://127.0.0.1:3000", "X-FinResearch": "1"}
# every marker is personal: none may appear in a prompt or a tool answer the model reads
MARKERS = ("Zyxwv Phantom", "INE999Q01017", "734519", "MARKER-JOURNAL-QUOKKA", "MARKER-AIS-WOMBAT", "918273",
           "MARKER-WEALTH-NUMBAT", "4455667", "MARKER-GOAL-BILBY", "8812345", "Marker Display Kakapo", "6677889")  # fmt: skip


class Capture:
    """A bridge router that records each task and answers like the agent would."""

    def __init__(self, out):
        self.tasks, self.out = [], out

    async def run(self, task, **kw):
        from finresearch.bridge.types import AgentResult, Tier

        self.tasks.append(task)
        return AgentResult(task_name=task.name, tier=Tier.CLAUDE_MAX, model="opus", ok=True,
                           structured_output=self.out, session_id="s1")  # fmt: skip


class Refuse:
    async def run(self, task, **kw):
        raise AssertionError(f"a model call was made: {task.name}")


ADVICE = {"action": "APPLY", "category": "retail", "lots": 1, "conditions": [], "exit_plan": "sell on listing",
          "watch": [], "rationale_markdown": "ok", "confidence": "medium"}  # fmt: skip


@pytest.fixture
def world(env, tmp_path):
    """An IPO run with a finished report, plus synthetic personal data in every personal table."""
    from fastapi.testclient import TestClient

    from finresearch.api import create_app
    from finresearch.db import session_scope
    from finresearch.db.models import (AgentStep, Claim, InvestorProfile, PortfolioAis, ResearchRun, TradeNote,
                                       WealthAsset, WealthGoal)  # fmt: skip
    from finresearch.ingest.documents import get_or_create_company
    from finresearch.suggest.advisor import save_profile
    from finresearch.suggest.profile import default_profile

    with TestClient(create_app()) as c:
        r = c.post("/api/portfolio/transactions", headers=ORIGIN,
                   json={"name": "Zyxwv Phantom Industries Ltd", "isin": "INE999Q01017", "day": "2025-05-02",
                         "kind": "buy", "quantity": 1, "price": 734519})  # fmt: skip
        assert r.status_code == 201, r.text
    with session_scope() as s:
        s.query(InvestorProfile).delete()
        p = default_profile().model_copy(update={"display_name": "Marker Display Kakapo", "fno_capital_inr": D(6677889)})
        p.household.monthly_income_inr = D(8812345)
        save_profile(s, p)
        s.add(TradeNote(side="buy", name="Zyxwv Phantom Industries Ltd", thesis="MARKER-JOURNAL-QUOKKA"))
        s.merge(PortfolioAis(fy=2026, sha256="1" * 64, format="json", ignored=0, warnings=[],
                             items=[{"category": "dividend", "part": "tds", "amount": "918273",
                                     "source": "MARKER-AIS-WOMBAT"}]))  # fmt: skip
        s.add(WealthAsset(kind="fd", name="MARKER-WEALTH-NUMBAT", principal=D(4455667)))
        s.add(WealthGoal(name="MARKER-GOAL-BILBY", target_inr=D(100000), target_date=date(2031, 1, 1)))
        co = get_or_create_company(s, "priv-" + tmp_path.name[-8:], "Privacy Test Co")
        co.nse_symbol = "PRIVTEST"
        run = ResearchRun(company_id=co.id, kind="ipo_report", status="done",
                          manifest={"facts": {"issue_close": "2026-09-29"}})  # fmt: skip
        s.add(run)
        s.flush()
        for metric, value in (("price_band_upper", 272), ("lot_size", 55)):
            s.add(Claim(run_id=run.id, stream="facts", statement=metric, claim_type="numeric", metric=metric,
                        value=D(value), unit="x", period="offer", status="verified"))  # fmt: skip
        s.add(AgentStep(run_id=run.id, key="synthesis", stage="synthesis", role="synthesizer", status="done",
                        output={"report_markdown": "# Privacy Test Co\nNo figures.", "overall_verdict": "APPLY",
                                "confidence": "medium"}))  # fmt: skip
        out = {"run_id": run.id, "slug": co.slug}
    return out


def _assert_clean(texts: list[str]) -> None:
    blob = "\n".join(texts)
    leaked = [m for m in MARKERS if m in blob]
    assert not leaked, f"personal markers reached a model: {leaked}"


async def _offline(symbol):
    return None


async def test_no_agent_task_or_agent_tool_carries_personal_data(world):
    from finresearch.agents import ask
    from finresearch.agents.roles import ROLES
    from finresearch.agents.runner import RunContext, build_task
    from finresearch.mcp_server import server
    from finresearch.suggest.advisor import suggest

    texts: list[str] = []
    advisor = Capture(ADVICE)
    await suggest(world["run_id"], router=advisor, fetch=_offline)
    (task,) = advisor.tasks
    texts += [task.system_prompt or "", task.prompt]
    assert '"capital_per_ipo_inr": "15000"' in task.system_prompt  # the test sees the real prompt
    assert "fno_capital_inr" not in task.system_prompt and "household" not in task.system_prompt

    asker = Capture({"answer_markdown": "ok", "cited_claims": [], "needs_new_research": False})
    await ask.ask(world["run_id"], "What is the lot size?", router=asker)
    texts += [t.system_prompt or "" for t in asker.tasks] + [t.prompt for t in asker.tasks]

    docs = server.list_documents()
    ctx = RunContext(run_id=world["run_id"], company_slug=world["slug"], company_name="Privacy Test Co",
                     nse_symbol="PRIVTEST", facts={"issue_close": "2026-09-29"})  # fmt: skip
    for name in ROLES:  # every research role of every kind (IPO, stock, fund, bond, discovery)
        t = build_task(name, ctx, plan="(plan)", claims="(claims)")
        texts += [t.system_prompt or "", t.prompt]
    texts += [docs, server.list_claims(world["run_id"]), server.identity_checks(world["run_id"], only_failing=False)]
    assert len(texts) > 2 * len(ROLES)
    _assert_clean(texts)


async def test_local_suggestion_makes_no_model_call(world):
    from finresearch.db import session_scope
    from finresearch.suggest.advisor import LOCAL_MODEL, load_profile, save_profile, suggest

    with session_scope() as s:
        save_profile(s, load_profile(s).model_copy(update={"local_suggestion": True}))
    d = await suggest(world["run_id"], router=Refuse(), fetch=_offline)
    sug = d["suggestion"]
    assert (sug["model"], sug["tier"]) == (LOCAL_MODEL, "local-rules")
    # verdict APPLY, but the live QIB book is unknown offline: the skip rule turns it into a condition
    assert d["action"] == "APPLY-CONDITIONAL" and d["lots"] == 1
    assert "not by a model" in sug["agent"]["rationale_markdown"]


def test_llm_paths_never_import_personal_tables():
    """Static guard: the modules that build prompts or serve the agents' tools import no personal model or module."""
    personal = {"PortfolioHolding", "PortfolioTxn", "PortfolioLot", "PortfolioDisposal", "PortfolioImport",
                "PortfolioAis", "PortfolioSnapshot", "PortfolioSetting", "TradeNote", "WealthAsset", "WealthGoal",
                "WealthLoan", "WealthPolicy", "WealthValuation", "BrokerConnection", "Decision"}  # fmt: skip
    bad = []
    for pkg in ("agents", "orchestrator", "mcp_server", "bridge", "verify", "ingest", "render"):
        for f in (SRC / pkg).rglob("*.py"):
            for node in ast.walk(ast.parse(f.read_text())):
                if isinstance(node, ast.ImportFrom) and node.module:
                    if node.module.startswith(("finresearch.portfolio", "finresearch.wealth")):
                        bad.append(f"{f.name}: {node.module}")
                    if node.module == "finresearch.db.models":
                        bad += [f"{f.name}: {a.name}" for a in node.names if a.name in personal]
    assert not bad, bad

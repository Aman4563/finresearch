"""The independent second verifier (#243), through the fake `claude` CLI: it runs on another model tier with its own
prompt, is never shown the claimed figure, the status or the first verifier's reasoning, and the orchestrator decides
agreement from the figure it re-derived."""

from __future__ import annotations

import json
import stat
import sys
from decimal import Decimal
from pathlib import Path

from finresearch.agents.runner import RunContext, run_role
from finresearch.bridge.claude_code import ClaudeCodeEngine
from finresearch.bridge.limits import LimitTracker
from finresearch.bridge.router import BridgeRouter
from finresearch.bridge.types import ModelClass, Tier

FIX = Path(__file__).parent / "fixtures"
MODELS = {ModelClass.DEEP: "fake-deep", ModelClass.STANDARD: "fake-standard", ModelClass.FAST: "fake-fast"}
FIRST_NOTE = "first verifier: printed at L1650 of the RHP restated P&L"


def _router(tmp_path):
    wrapper = tmp_path / "claude"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FIX / "fake_claude.py"}" "$@"\n')
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
    eng = ClaudeCodeEngine(
        Tier.CLAUDE_MAX, claude_bin=str(wrapper), models=MODELS, runs_dir=tmp_path / "runs"
    )
    return BridgeRouter(
        {Tier.CLAUDE_MAX: eng}, [Tier.CLAUDE_MAX], LimitTracker(tmp_path / "st"), tmp_path / "l.jsonl"
    )


def _claims(tmp_path):
    from finresearch.db import session_scope
    from finresearch.db.models import Citation, Claim, ResearchRun
    from finresearch.ingest.documents import get_or_create_company

    with session_scope() as s:
        co = get_or_create_company(s, "blind-" + tmp_path.name[-8:], "Acme Cables Ltd")
        run = ResearchRun(company_id=co.id, kind="ipo_report", manifest={})
        s.add(run)
        s.flush()
        ids = []
        for value in ("1234.56", "987.65"):
            c = Claim(run_id=run.id, stream="financials", claim_type="numeric", importance="high", status="verified",
                      statement=f"Revenue from operations was ₹{value} million in FY2026", metric="revenue",
                      value=Decimal(value), unit="INR million", period="FY2026", verifier_note=FIRST_NOTE,
                      checks={"verifiers": {"first": {"role": "verifier", "verdict": "verified"}}})  # fmt: skip
            c.citations = [Citation(line_start=1650, line_end=1651, quote=f"Revenue from operations {value}",
                                    quote_found=True)]  # fmt: skip
            s.add(c)
            s.flush()
            ids.append(c.id)
        return run.id, ids


async def test_blind_second_verifier_runs_blind_on_another_tier_and_decides_agreement(
    env, tmp_path, monkeypatch
):
    from finresearch.db import session_scope
    from finresearch.db.models import Claim
    from finresearch.orchestrator.ipo import IpoPipeline

    run_id, (same, other) = _claims(tmp_path)
    pipe = IpoPipeline(run_id, runner=lambda *a, **k: None, tracker=LimitTracker(tmp_path / "lim"))
    items = pipe._blind_claims_text([same, other])
    assert "1234" not in items and "987" not in items  # neither the value nor the quote that carries it
    assert "verified" not in items and "L1650 of the RHP" not in items  # no status, no first verifier's note
    assert "Revenue from operations was ₹[N] million in FY[N]" in items

    # the blind verifier reads ₹123.456 crore (= ₹1,234.56 million) for the first claim and ₹1,300 million for the
    # second (the claim says ₹987.65 million: 32 % apart)
    log = tmp_path / "argv.jsonl"
    monkeypatch.setenv("FAKE_CLAUDE_ARGV_LOG", str(log))
    monkeypatch.setenv("FAKE_CLAUDE_OUTPUT", json.dumps({"summary": "read L1650", "findings": [
        {"claim_id": same, "derived_value": "123.456", "derived_unit": "INR crore", "supports_statement": "yes",
         "evidence": "RHP L1650: Revenue from operations 1,234.56 (₹ million)"},
        {"claim_id": other, "derived_value": "1,300.00", "derived_unit": "INR million", "supports_statement": "yes",
         "evidence": "RHP L1702: 1,300.00"}]}))  # fmt: skip
    ctx = RunContext(run_id=run_id, company_slug="acme", company_name="Acme Cables Ltd")
    rep, _ = await run_role(
        "verifier_blind", ctx, router=_router(tmp_path), target_stream="financials", claims=items
    )

    call = json.loads(log.read_text().splitlines()[-1])
    argv = call["argv"]
    assert argv[argv.index("--model") + 1] == "fake-standard"  # the first verifier runs on DEEP
    system = argv[argv.index("--append-system-prompt") + 1]
    assert "independent checker" in system and "adversarial verifier" not in system
    sent = system + call["stdin"]
    assert "1234.56" not in sent and "987.65" not in sent and FIRST_NOTE not in sent
    assert "mcp__finresearch__list_claims" not in argv[argv.index("--allowedTools") + 1]

    pipe._apply_blind(rep, items)
    with session_scope() as s:
        a, b = s.get(Claim, same), s.get(Claim, other)
        assert a.status == "verified" and a.checks["verifiers"]["agreed"] == ["verifier", "verifier_blind"]
        assert b.status == "needs_review" and b.checks["verifiers"]["agreed"] == ["verifier"]
        assert "independent second verifier disagrees: re-derived 1,300.00" in b.verifier_note


def test_agreement_rules():
    from types import SimpleNamespace as NS

    from finresearch.agents.schemas import BlindFinding
    from finresearch.verify.second_opinion import agrees, parse_figure

    assert parse_figure("(512.40)") == Decimal("-512.40") and parse_figure("₹1,23,456.7") == Decimal(
        "123456.7"
    )
    assert parse_figure("n/a") is None
    num = NS(claim_type="numeric", value=Decimal("12.5"), unit="%")
    f = lambda v, sup="yes", u=None: BlindFinding(
        claim_id=1,
        derived_value=v,
        derived_unit=u,  # noqa: E731
        supports_statement=sup,
        evidence="e",
    )
    assert (
        agrees(num, f("12.5"))[0] and agrees(num, f("12.53"))[0]
    )  # 0.24 % apart: within the 0.5 % tolerance
    assert not agrees(num, f("12.6"))[0]  # 0.8 % apart
    assert not agrees(num, f(None))[0] and not agrees(num, None)[0]
    assert not agrees(num, f("12.5", sup="no"))[0]
    fact = NS(claim_type="factual", value=None, unit=None)
    assert agrees(fact, f(None, "yes"))[0] and not agrees(fact, f(None, "cannot_tell"))[0]

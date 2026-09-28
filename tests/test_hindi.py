"""Hindi news: Hindi-quoted web citations are detected, and the report gate warns about unmarked translations."""

from __future__ import annotations

from finresearch.agents.roles import ROLES
from finresearch.agents.runner import RunContext, render

HINDI_QUOTE = "ओरिएंट केबल्स के आईपीओ को दूसरे दिन 5.84 गुना सब्सक्रिप्शन मिला"


def _run(env, tmp_path):
    from finresearch.db import session_scope
    from finresearch.db.models import ResearchRun
    from finresearch.ingest.documents import get_or_create_company

    with session_scope() as s:
        co = get_or_create_company(s, "hi-" + tmp_path.name[-8:], "Hindi Co")
        r = ResearchRun(company_id=co.id, kind="ipo_report", manifest={})
        s.add(r)
        s.flush()
        return r.id


def test_hindi_quotes_are_flagged_and_unmarked_translations_warned(env, tmp_path):
    from finresearch.db import session_scope
    from finresearch.db.models import Claim
    from finresearch.mcp_server.claims import save_claim
    from finresearch.verify.gate import check_report

    run_id = _run(env, tmp_path)
    cite = [
        {
            "url": "https://hindi.example/orient-ipo",
            "accessed_at": "2026-09-28T18:00:00+05:30",
            "quote": HINDI_QUOTE,
        }
    ]
    with session_scope() as s:
        plain = save_claim(s, run_id=run_id, stream="news30", statement="Day-2 subscription 5.84x per a Hindi daily",
                           claim_type="numeric", metric="total_subscription", value="5.84", unit="x",
                           period="2026-09-28 day 2", citations=cite)  # fmt: skip
        marked = save_claim(s, run_id=run_id, stream="news30", claim_type="numeric", metric="total_subscription",
                            statement="Day-2 subscription was 5.84x (translated from Hindi)", value="5.84", unit="x",
                            period="2026-09-28 day 2", citations=cite)  # fmt: skip
        english = save_claim(s, run_id=run_id, stream="news30", statement="Anchor book closed", claim_type="factual",
                             citations=[{"url": "https://en.example/x", "quote": "anchor book closed"}])  # fmt: skip
        assert "translated from Hindi" in plain["note"] and "note" not in marked and "note" not in english
        auto = s.get(Claim, plain["claim_id"])
        assert auto.statement == "Day-2 subscription 5.84x per a Hindi daily (translated from Hindi)"
        assert auto.checks == {
            "source_language": "hi",
            "translation_marked": True,
            "translation_mark_added": True,
        }
        assert s.get(Claim, marked["claim_id"]).checks["translation_mark_added"] is False
        assert s.get(Claim, english["claim_id"]).checks == {}
        # a claim saved before the marker existed (live run 7) still gets a gate warning when the report cites it
        legacy = s.get(Claim, english["claim_id"])
        legacy.checks = {"source_language": "hi", "translation_marked": False}
        s.flush()
        report = f"Subscription 5.84x [C{plain['claim_id']}]; anchor [C{legacy.id}]."
        gate = check_report(s, run_id, report)
        legacy_id = legacy.id
    hindi_warnings = [w for w in gate.warnings if "Hindi" in w]
    assert gate.ok and hindi_warnings == [f"[C{legacy_id}] quotes a Hindi source: say in the report that the figure "
                                          "is translated"]  # fmt: skip


def test_news_prompt_requires_hindi_coverage():
    ctx = RunContext(
        run_id=1, company_slug="x", company_name="Orient Cables", nse_symbol="ORIENTCABL", documents=[]
    )
    system, _ = render(ROLES["news30"], ctx)
    assert "HINDI COVERAGE (required)" in system and "आईपीओ" in system and "(translated from Hindi)" in system

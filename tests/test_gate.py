"""Deterministic verification gate, seeded with mistakes the manual and live runs actually made."""

from __future__ import annotations

import hashlib
import shutil
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

FIX = Path(__file__).parent / "fixtures"
FACTS = {"issue_open": "2026-09-25", "issue_close": "2026-09-29"}
NOW = datetime(2026, 9, 28, 14, 0, tzinfo=UTC)


@pytest.fixture
def ledger(env, tmp_path):
    """A run over a one-page document whose text is the Orient P&L layout fixture."""
    from finresearch.db import session_scope
    from finresearch.db.models import Document, DocumentPage, ResearchRun
    from finresearch.ingest.documents import get_or_create_company

    txt = tmp_path / "text.txt"
    shutil.copy(FIX / "orient_rhp_pnl_layout.txt", txt)
    lines = txt.read_text().split("\n")
    with session_scope() as s:
        co = get_or_create_company(
            s, "gate-" + hashlib.sha1(str(tmp_path).encode()).hexdigest()[:8], "Gate Co"
        )
        d = Document(company_id=co.id, kind="RHP", title="t", sha256=hashlib.sha256(str(tmp_path).encode()).hexdigest(),
                     local_path=str(txt), text_path=str(txt), bytes=1, pages=1)  # fmt: skip
        s.add(d)
        s.flush()
        s.add(DocumentPage(document_id=d.id, page_no=1, line_start=1, line_end=len(lines), text="", char_count=0,
                           text_source="pdftotext"))  # fmt: skip
        run = ResearchRun(company_id=co.id, kind="test", manifest={})
        s.add(run)
        s.flush()
        return {"run": run.id, "doc": d.id, "lines": lines}


def line_of(lines, text):
    return next(i for i, x in enumerate(lines, 1) if text in x)


def add(led, *, stream="financials", statement="x", claim_type="numeric", metric=None, value=None, unit=None,
        period=None, importance="normal", status="unverified", cite_line=None, url=None, accessed_at=None):  # fmt: skip
    from finresearch.db import session_scope
    from finresearch.db.models import Citation, Claim

    with session_scope() as s:
        c = Claim(run_id=led["run"], stream=stream, statement=statement, claim_type=claim_type, metric=metric,
                  value=Decimal(str(value)) if value is not None else None, unit=unit, period=period,
                  importance=importance, status=status)  # fmt: skip
        s.add(c)
        s.flush()
        if cite_line:
            s.add(Citation(claim_id=c.id, document_id=led["doc"], line_start=cite_line, line_end=cite_line,
                           quote=led["lines"][cite_line - 1].strip(), quote_found=True))  # fmt: skip
        if url:
            s.add(Citation(claim_id=c.id, url=url, accessed_at=accessed_at, quote="q"))
        return c.id


def gate(led, **kw):
    from finresearch.db import session_scope
    from finresearch.verify.gate import run_gate

    with session_scope() as s:
        return run_gate(s, led["run"], facts=FACTS, now=NOW, **kw)


def claim(cid):
    from finresearch.db import session_scope
    from finresearch.db.models import Claim

    with session_scope() as s:
        c = s.get(Claim, cid)
        return c.status, dict(c.checks or {}), c.verifier_note or ""


def test_value_found_through_unit_conversion_and_derived_values_flagged(ledger):
    ln = line_of(ledger["lines"], "Revenue from Operations")
    ok = add(ledger, metric="revenue", value="1171.654", unit="INR crore", period="FY2026", cite_line=ln)
    rounding = add(ledger, metric="cogs_ratio", value="80.36", unit="%", period="FY2026", cite_line=ln)
    wrong_scale = add(
        ledger, metric="revenue2", value="117165.4", unit="INR crore", period="FY2026", cite_line=ln
    )
    r = gate(ledger)
    assert claim(ok)[1]["value_in_source"] is True and claim(ok)[0] == "unverified"
    assert rounding in r.derived and claim(rounding)[0] == "needs_review"
    assert wrong_scale in r.derived and claim(wrong_scale)[0] == "needs_review"


def test_conflicting_promoter_holding_across_streams(ledger):
    a = add(ledger, stream="valuation", metric="promoter_holding_post_issue", value="15.55", unit="%",
            period="post-offer", status="verified")  # fmt: skip
    b = add(
        ledger,
        stream="risks",
        metric="Promoter holding (post issue)",
        value="20.33",
        unit="%",
        period="Post-Offer",
    )
    same = add(
        ledger,
        stream="demand",
        metric="promoter_holding_post_issue",
        value="15.55",
        unit="%",
        period="post offer",
    )
    r = gate(ledger)
    assert (a, b) in r.conflicts or (b, a) in r.conflicts
    assert claim(a)[0] == "needs_review" and claim(b)[0] == "needs_review"  # verified claim is downgraded too
    assert b in claim(a)[1]["conflict_with"] and "conflicts with claim" in claim(b)[2]
    assert b in claim(same)[1]["conflict_with"]


def test_live_figures_need_timestamps_and_fresh_sources(ledger):
    no_time = add(ledger, stream="demand", claim_type="factual", statement="GMP is Rs 90 per share (unofficial)",
                  url="https://gmp.example", accessed_at=NOW - timedelta(hours=1))  # fmt: skip
    timed = add(ledger, stream="demand", claim_type="factual",
                statement="Total subscription 6.56x as of 28-Sep-2026 14:39 IST (INTERIM)",
                url="https://www.nseindia.com", accessed_at=NOW - timedelta(minutes=10))  # fmt: skip
    stale = add(ledger, stream="demand", claim_type="factual", statement="GMP Rs 113 at 11:00 IST (INTERIM)",
                url="https://gmp.example", accessed_at=NOW - timedelta(days=3))  # fmt: skip
    r = gate(ledger)
    assert no_time in r.live_flags and "no time/INTERIM" in claim(no_time)[2]
    assert timed not in r.live_flags and claim(timed)[1]["live_ok"] is True
    assert stale in r.live_flags and "older than 24h" in claim(stale)[2]


def test_weekend_day_label_is_a_deterministic_contradiction(ledger):
    wrong = add(ledger, stream="demand", claim_type="factual",
                statement="Day 3 (28-Sep-2026 14:27 IST, INTERIM): 6.48x subscribed")  # fmt: skip
    right = add(ledger, stream="demand", claim_type="factual",
                statement="Day 2 (28 Sep 2026, 14:39 IST, INTERIM): 6.56x subscribed")  # fmt: skip
    r = gate(ledger)
    assert wrong in r.day_label_errors and claim(wrong)[0] == "contradicted"
    assert "is bidding Day 2, not Day 3" in claim(wrong)[2]
    assert claim(right)[1]["day_label_ok"] is True and claim(right)[0] != "contradicted"


def test_corrections_are_rechecked_against_the_source(ledger):
    from finresearch.db import session_scope
    from finresearch.db.models import Claim
    from finresearch.verify.gate import apply_correction

    ln = line_of(ledger["lines"], "Profit / (Loss) for the year")
    bad = add(ledger, metric="pat", value="536.61", unit="INR million", period="FY2026", cite_line=ln,
              status="contradicted", importance="high")  # fmt: skip
    bad2 = add(
        ledger,
        metric="pat_growth",
        value="1.2",
        unit="%",
        period="FY2026",
        cite_line=ln,
        status="contradicted",
    )
    with session_scope() as s:
        c1 = apply_correction(s, s.get(Claim, bad), "535.61", "RHP line shows 535.61")
        c2 = apply_correction(s, s.get(Claim, bad2), "0.45%", "fincalc pct_change")
        assert (
            c1.status == "verified" and c1.corrects_claim_id == bad and c1.checks["value_in_source"] is True
        )
        assert c2.status == "needs_review" and c2.value == Decimal("0.45")
        assert len(c1.citations) == 1 and c1.importance == "high"


def test_publish_gate_blocks_bad_citations_and_warns_on_uncited_figures(ledger):
    from finresearch.db import session_scope
    from finresearch.verify.gate import apply_correction, check_report

    ln = line_of(ledger["lines"], "Profit / (Loss) for the year")
    good = add(
        ledger,
        metric="pat",
        value="535.61",
        unit="INR million",
        period="FY2026",
        cite_line=ln,
        status="verified",
    )
    bad = add(ledger, metric="pat_x", value="536.61", unit="INR million", period="FY2026", cite_line=ln,
              status="contradicted")  # fmt: skip
    high = add(ledger, metric="mcap", value="3095.35", unit="INR crore", period="post-issue", importance="high",
               status="unverified")  # fmt: skip
    with session_scope() as s:
        from finresearch.db.models import Claim

        fix = apply_correction(s, s.get(Claim, bad), "535.61", "line")
        fix_id = fix.id
    clean = f"PAT was ₹535.61 mn [C{good}].\n"
    with session_scope() as s:
        assert check_report(s, ledger["run"], clean).ok
        g = check_report(s, ledger["run"], clean + f"PAT ₹536.61 mn [C{bad}]. Mcap ₹3,095 cr [C{high}]. "
                         "See [RHP L4960]. Margin 8.23% with no cite.\n")  # fmt: skip
    joined = " ".join(g.blocking)
    assert not g.ok and f"[C{bad}] is contradicted" in joined and f"use the correction [C{fix_id}]" in joined
    assert f"[C{high}] is high-importance" in joined and "raw document citations" in joined
    with session_scope() as s:
        g2 = check_report(s, ledger["run"], clean + "Margin 8.23% with no cite.\n")
    assert g2.ok and any("without a [C<id>]" in w for w in g2.warnings)
    with session_scope() as s:
        assert "does not exist" in " ".join(check_report(s, ledger["run"], "x [C999999]").blocking)


def test_live_check_ignores_statements_without_a_live_figure(ledger):
    bm = add(ledger, stream="demand", claim_type="factual",
             statement="Book Running Lead Managers are IIFL and JM Financial; subscription opens 25-Sep-2026")  # fmt: skip
    gate(ledger)
    assert claim(bm)[0] == "unverified" and "live_ok" not in claim(bm)[1]

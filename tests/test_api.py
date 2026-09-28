"""Local API: endpoints against the test database, run workers (never launched for real) and live SSE events."""

from __future__ import annotations

import hashlib
import json
import os
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from finresearch.api.workers import Spawner, worker_command


@pytest.fixture
def seeded(env, tmp_path):
    from finresearch.db import session_scope
    from finresearch.db.models import AgentStep, Citation, Claim, Document, ResearchRun
    from finresearch.ingest.documents import get_or_create_company

    tag = hashlib.sha1(str(tmp_path).encode()).hexdigest()[:8]
    docs_dir = env.docs_dir
    docs_dir.mkdir(parents=True, exist_ok=True)
    pdf = docs_dir / f"{tag}.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    txt = docs_dir / f"{tag}.txt"
    txt.write_text("line one\nProfit for the year 535.61\fpage two line\nlast")
    with session_scope() as s:
        co = get_or_create_company(s, f"api-{tag}", "Api Co")
        co.nse_symbol = "APICO"
        d = Document(company_id=co.id, kind="RHP", title="Api RHP", sha256=hashlib.sha256(tag.encode()).hexdigest(),
                     local_path=str(pdf), text_path=str(txt), bytes=1, pages=2)  # fmt: skip
        s.add(d)
        s.flush()
        run = ResearchRun(
            company_id=co.id, kind="ipo_report", status="done", manifest={"final_gate": {"ok": True}}
        )
        s.add(run)
        s.flush()
        ok = Claim(run_id=run.id, stream="financials", statement="PAT FY26 ₹535.61 mn", claim_type="numeric",
                   metric="pat", value=Decimal("535.61"), unit="INR million", period="FY26", status="verified")  # fmt: skip
        bad = Claim(run_id=run.id, stream="demand", statement="GMP ₹90", claim_type="numeric", status="contradicted",
                    verifier_note="stale")  # fmt: skip
        s.add_all([ok, bad])
        s.flush()
        s.add(Citation(claim_id=ok.id, document_id=d.id, page_no=1, line_start=2, line_end=2,
                       quote="Profit for the year 535.61", quote_found=True))  # fmt: skip
        s.add(Citation(claim_id=bad.id, url="https://gmp.example/x"))
        report = f"# Api Co\n\nPAT rose to ₹535.61 mn [C{ok.id}].\n"
        s.add(AgentStep(run_id=run.id, key="planner", stage="plan", role="planner", status="done", num_turns=3,
                        duration_s=60, five_hour_before=0.1, five_hour_after=0.15))  # fmt: skip
        s.add(AgentStep(run_id=run.id, key="synthesis", stage="synthesis", role="synthesizer", status="done",
                        output={"report_markdown": report}))  # fmt: skip
        pack = env.reports_dir / co.slug / f"run-{run.id}" / "06_Final_Report"
        pack.mkdir(parents=True)
        (pack / "report.md").write_text(report)
        return {"slug": co.slug, "run_id": run.id, "doc_id": d.id, "ok": ok.id, "bad": bad.id}


class FakeSpawner(Spawner):
    def __init__(self):
        self.calls: list[list[str]] = []
        super().__init__(popen=self._popen)

    def _popen(self, argv, log):
        self.calls.append(argv)
        return 999_999_999  # never a live pid


class FakeRouter:
    """Stands in for the bridge: records tasks and returns queued structured answers."""

    def __init__(self):
        self.tasks = []
        self.answers: list[dict] = []

    async def run(self, task):
        from finresearch.bridge.types import AgentResult, Tier

        self.tasks.append(task)
        return AgentResult(task_name=task.name, tier=Tier.CLAUDE_MAX, model="sonnet", ok=True,
                           structured_output=self.answers.pop(0), session_id=f"sess-{len(self.tasks)}",
                           num_turns=3, duration_s=4.0)  # fmt: skip


@pytest.fixture
def client(env):
    from finresearch.api import create_app

    spawner, router = FakeSpawner(), FakeRouter()
    with TestClient(create_app(spawner=spawner, poll_s=0.01, router=router)) as c:
        c.spawner, c.router = spawner, router
        yield c


def test_companies_documents_and_lines(client, seeded):
    rows = {c["slug"]: c for c in client.get("/api/companies").json()}
    assert rows[seeded["slug"]]["documents"] == 1 and rows[seeded["slug"]]["latest_run"] == seeded["run_id"]
    co = client.get(f"/api/companies/{seeded['slug']}").json()
    assert co["documents"][0]["kind"] == "RHP" and co["runs"][0]["id"] == seeded["run_id"]
    lines = client.get(f"/api/documents/{seeded['doc_id']}/lines", params={"start": 2, "end": 3}).json()
    # grep-compatible: the \f page break does not start a new line
    assert lines["lines"] == ["Profit for the year 535.61\fpage two line", "last"]
    assert client.get(f"/api/documents/{seeded['doc_id']}/file").content == b"%PDF-1.4 fake"
    assert client.get("/api/companies/nope").status_code == 404


def test_run_detail_claims_and_report_with_gate(client, seeded):
    rid = seeded["run_id"]
    d = client.get(f"/api/runs/{rid}").json()
    assert d["status"] == "done" and [s["key"] for s in d["steps"]] == ["planner", "synthesis"]
    assert (
        d["claims"] == {"verified": 1, "contradicted": 1} and abs(d["usage"]["five_hour_used"] - 0.05) < 1e-9
    )
    claims = client.get(f"/api/runs/{rid}/claims", params={"status": "verified"}).json()
    assert [c["id"] for c in claims] == [seeded["ok"]] and claims[0]["citations"][0][
        "document_title"
    ] == "Api RHP"
    assert [c["id"] for c in client.get(f"/api/runs/{rid}/claims", params={"cited": True}).json()] == [
        seeded["ok"]
    ]
    rep = client.get(f"/api/runs/{rid}/report").json()
    assert rep["published"] and list(rep["claims"]) == [str(seeded["ok"])]
    assert rep["claims"][str(seeded["ok"])]["citations"][0]["quote_found"] is True
    assert client.get(f"/api/claims/{seeded['bad']}").json()["status"] == "contradicted"


def test_report_that_cites_a_contradicted_claim_is_not_published(client, seeded):
    from finresearch.db import session_scope
    from finresearch.db.models import AgentStep

    with session_scope() as s:
        st = s.query(AgentStep).filter_by(run_id=seeded["run_id"], key="synthesis").one()
        st.output = {"report_markdown": f"GMP ₹90 [C{seeded['bad']}]"}
    rep = client.get(f"/api/runs/{seeded['run_id']}/report").json()
    assert not rep["published"] and "contradicted" in rep["gate"]["blocking"][0]


def test_pack_listing_and_path_traversal(client, seeded):
    rid = seeded["run_id"]
    assert client.get(f"/api/runs/{rid}/pack").json()["files"] == ["06_Final_Report/report.md"]
    assert client.get(f"/api/runs/{rid}/pack/06_Final_Report/report.md").text.startswith("# Api Co")
    assert client.get(f"/api/runs/{rid}/pack/..%2F..%2F..%2Fetc%2Fpasswd").status_code == 404


def test_start_and_resume_spawn_the_cli_worker(client, seeded):
    r = client.post(
        "/api/runs", json={"company": seeded["slug"], "streams": ["financials"], "concurrency": 2}
    )
    assert r.status_code == 201
    run_id = r.json()["run_id"]
    argv = client.spawner.calls[-1]
    assert argv == worker_command(run_id, streams=["financials"], concurrency=2)
    assert argv[1:] == ["-m", "finresearch.cli", "ipo", "resume", str(run_id), "--wait", "--concurrency", "2",
                        "--streams", "financials"]  # fmt: skip
    detail = client.get(f"/api/runs/{run_id}").json()
    assert detail["worker"]["pid"] == 999_999_999 and detail["worker"]["alive"] is False
    assert client.post(f"/api/runs/{run_id}/resume").status_code == 200
    assert client.post("/api/runs", json={"company": "nope"}).status_code == 404
    assert client.post("/api/runs", json={"company": seeded["slug"], "streams": ["bogus"]}).status_code == 422


def test_resume_refuses_a_second_live_worker(client, seeded):
    from finresearch.db import session_scope
    from finresearch.db.models import ResearchRun

    with session_scope() as s:
        run = s.get(ResearchRun, seeded["run_id"])
        run.manifest = {**run.manifest, "worker": {"pid": os.getpid()}}
    assert client.post(f"/api/runs/{seeded['run_id']}/resume").status_code == 409


def _sse(text: str) -> list[tuple[str, dict]]:
    out = []
    for block in text.split("\n\n"):
        ev = dict(
            line.split(": ", 1) for line in block.split("\n") if ": " in line and not line.startswith(":")
        )
        if "event" in ev:
            out.append((ev["event"], json.loads(ev["data"])))
    return out


def test_events_stream_step_changes_until_the_run_finishes(client, seeded):
    import threading
    import time

    from finresearch.db import session_scope
    from finresearch.db.models import AgentStep, ResearchRun

    rid = seeded["run_id"]
    with session_scope() as s:
        s.get(ResearchRun, rid).status = "running"

    def worker():  # stands in for the pipeline process: finish a step, then the run
        time.sleep(0.3)
        with session_scope() as s:
            s.add(AgentStep(run_id=rid, key="critic", stage="critic", role="critic", status="done"))
        time.sleep(0.3)
        with session_scope() as s:
            s.get(ResearchRun, rid).status = "done"

    t = threading.Thread(target=worker)
    t.start()
    with client.stream("GET", f"/api/runs/{rid}/events") as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        events = _sse(r.read().decode())
    t.join()
    kinds = [e for e, _ in events]
    assert kinds[0] == "snapshot" and kinds[-1] == "end" and events[-1][1]["status"] == "done"
    assert ("step", "critic") in [(e, d.get("key")) for e, d in events]
    assert ("run", "done") in [(e, d.get("status")) for e, d in events]


def test_only_localhost_hosts_are_accepted(client):
    assert client.get("/api/health", headers={"host": "evil.example"}).status_code == 400
    assert client.get("/api/health").json() == {"ok": True}


def test_limits_reads_the_isolated_state(client, env):
    assert "tiers" in client.get("/api/limits").json()
    assert Path(env.state_dir).is_relative_to(Path(env.docs_dir).parent)


def test_ipo_radar_links_known_companies_and_reports_source_errors(client, seeded, monkeypatch):
    from datetime import date

    from finresearch.adapters import nse
    from finresearch.adapters.nse import IpoIssue

    class FakeNse:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def current_issues(self):
            return [
                IpoIssue(
                    symbol="APICO",
                    company="Api Co",
                    issue_start=date(2026, 9, 25),
                    price_band="Rs.258 to Rs.272",
                )
            ]

        async def upcoming_issues(self):
            raise nse.NseError("NSE refused")

    monkeypatch.setattr(nse, "NseClient", FakeNse)
    data = client.get("/api/ipos", params={"refresh": True}).json()
    row = data["issues"][0]
    assert (
        row["phase"] == "current" and row["slug"] == seeded["slug"] and row["latest_run"] == seeded["run_id"]
    )
    assert row["issue_start"] == "2026-09-25" and data["errors"][0].startswith("upcoming")


def test_ipo_radar_dedupes_symbols_and_derives_the_phase_from_dates(client, monkeypatch):
    from datetime import date, datetime

    from finresearch.adapters import nse
    from finresearch.adapters.nse import IpoIssue

    def issue(sym, start, end):
        return IpoIssue(symbol=sym, company=sym, issue_start=start, issue_end=end)

    class FakeNse:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def current_issues(self):
            return [
                issue("OPEN", date(2026, 9, 25), date(2026, 9, 29)),
                issue("DONE", date(2026, 9, 20), date(2026, 9, 24)),
            ]

        async def upcoming_issues(self):
            return [
                issue("OPEN", date(2026, 9, 25), date(2026, 9, 29)),
                issue("NEXT", date(2026, 10, 1), date(2026, 10, 5)),
            ]

    monkeypatch.setattr(nse, "NseClient", FakeNse)
    monkeypatch.setattr("finresearch.fincalc.dates.now_ist", lambda: datetime(2026, 9, 28, 12))
    rows = client.get("/api/ipos", params={"refresh": True}).json()["issues"]
    assert [(r["symbol"], r["phase"]) for r in rows] == [
        ("OPEN", "open"),
        ("NEXT", "upcoming"),
        ("DONE", "closed"),
    ]


def test_ask_answers_are_checked_and_conversations_resume_the_session(client, seeded):
    rid, ok, bad = seeded["run_id"], seeded["ok"], seeded["bad"]
    client.router.answers = [
        {"answer_markdown": f"FY26 PAT was ₹535.61 mn [C{ok}], see [D{seeded['doc_id']}:L2-2].", "cited_claims": [ok]},
        {"answer_markdown": f"GMP was ₹90 [C{bad}] and the P/E is 51.6x.\nAlso [C999999] and [D{seeded['doc_id']}:L7-9].",
         "cited_claims": []},
    ]  # fmt: skip
    r1 = client.post(f"/api/runs/{rid}/ask", json={"question": "What was FY26 PAT?"}).json()
    conv = r1["conversation_id"]
    assert r1["message"]["checks"]["ok"] is True and r1["message"]["checks"]["cited_claims"] == [ok]
    first = client.router.tasks[0]
    assert first.resume_session_id is None and not any(
        t in first.allowed_tools for t in ("mcp__finresearch__save_claim", "WebSearch")
    )
    assert "PAT rose to" in first.system_prompt  # the report is in context

    r2 = client.post(f"/api/runs/{rid}/ask", json={"question": "And GMP?", "conversation_id": conv}).json()
    assert client.router.tasks[1].resume_session_id == "sess-1"
    c = r2["message"]["checks"]
    assert c["unusable_claims"] == [bad] and c["unknown_claims"] == [999999] and c["ok"] is False
    assert c["bad_line_citations"] == [f"D{seeded['doc_id']}:L7-9"]  # the document has only 4 lines
    assert any("51.6x" in line for line in c["uncited_figure_lines"])

    thread = client.get(f"/api/conversations/{conv}").json()
    assert [m["role"] for m in thread["messages"]] == ["user", "assistant", "user", "assistant"]
    assert client.get(f"/api/runs/{rid}/conversations").json()[0]["messages"] == 4


def test_ask_rejects_unknown_runs_and_foreign_conversations(client, seeded):
    assert client.post("/api/runs/999999/ask", json={"question": "x"}).status_code == 404
    client.router.answers = [{"answer_markdown": "ok"}]
    conv = client.post(f"/api/runs/{seeded['run_id']}/ask", json={"question": "hi"}).json()["conversation_id"]
    from finresearch.db import session_scope
    from finresearch.db.models import ResearchRun

    with session_scope() as s:
        other = ResearchRun(kind="ipo_report", status="done", manifest={})
        s.add(other)
        s.flush()
        other_id = other.id
    r = client.post(f"/api/runs/{other_id}/ask", json={"question": "x", "conversation_id": conv})
    assert r.status_code == 404
    assert client.post(f"/api/runs/{seeded['run_id']}/ask", json={"question": ""}).status_code == 422

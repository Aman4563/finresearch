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


def test_usage_by_run_sums_plan_window_turns_and_minutes(client, seeded):
    rows = client.get("/api/usage/runs").json()
    row = next(r for r in rows if r["run_id"] == seeded["run_id"])
    assert row["kind"] == "ipo_report" and row["company_name"] == "Api Co" and row["steps"] == 2
    assert abs(row["five_hour_used"] - 0.05) < 1e-9 and row["turns"] == 3 and abs(row["minutes"] - 1) < 1e-9
    assert len(client.get("/api/usage/runs", params={"limit": 1}).json()) == 1
    assert client.get("/api/usage/runs", params={"limit": 0}).status_code == 422


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
    assert "--concurrency" not in client.spawner.calls[-1]  # resume keeps the run's saved concurrency
    client.post(f"/api/runs/{run_id}/resume", json={"concurrency": 3})
    assert client.spawner.calls[-1][-2:] == ["--concurrency", "3"]
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
        run = s.get(ResearchRun, rid)
        run.status, run.manifest = (
            "running",
            {**run.manifest, "worker": {"pid": os.getpid()}},
        )  # a live worker

    def worker():  # stands in for the pipeline process: finish a step, then the run, then exit
        time.sleep(0.3)
        with session_scope() as s:
            s.add(AgentStep(run_id=rid, key="critic", stage="critic", role="critic", status="done"))
        time.sleep(0.3)
        with session_scope() as s:
            run = s.get(ResearchRun, rid)
            run.status, run.manifest = "done", {**run.manifest, "worker": {"pid": 999_999_999}}

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
    monkeypatch.setattr("finresearch.adapters.bse.sme_radar", no_bse)
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
    monkeypatch.setattr("finresearch.adapters.bse.sme_radar", no_bse)
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


# --------------------------------------------------------------------------- BSE SME in the radar
async def no_bse():
    return [], []


class FakeNseList:
    def __init__(self, issues):
        self.issues = issues

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None

    async def current_issues(self):
        return self.issues

    async def upcoming_issues(self):
        return []


def test_radar_lists_bse_sme_issues_with_lot_and_skips_ones_nse_lists(client, monkeypatch):
    from datetime import date, datetime

    from finresearch.adapters import bse, nse
    from finresearch.adapters.bse import BseIssue, BseIssueDetail
    from finresearch.adapters.http import IST
    from finresearch.adapters.nse import IpoIssue

    fix = Path(__file__).parent / "fixtures" / "bse"

    def load(name):
        return json.loads((fix / name).read_text())

    def shivchem():
        return BseIssueDetail.parse(load("issue_detail_8008_SHIVCHEM_20260929_1034.json"))

    rows = [BseIssue.parse(r) for r in load("public_issues_20260929.json")["Table"]]
    sme = [i for i in rows if i.is_sme_ipo]
    tna = BseIssueDetail.parse(load("issue_detail_8023_TNA_20260929.json"))

    async def fake_radar():
        return [(i, {8008: shivchem(), 8023: tna}.get(i.ipo_no)) for i in sme], [
            "BSE IPO 8007 details: HTTP 500"
        ]

    both = IpoIssue(symbol="EVEREST", company="Everestims Technologies Ltd", series="SME",
                    issue_start=date(2026, 9, 29), issue_end=date(2026, 10, 5))  # fmt: skip
    monkeypatch.setattr(nse, "NseClient", lambda: FakeNseList([both]))
    monkeypatch.setattr(bse, "sme_radar", fake_radar)
    monkeypatch.setattr("finresearch.fincalc.dates.now_ist", lambda: datetime(2026, 9, 29, 12, tzinfo=IST))
    data = client.get("/api/ipos", params={"refresh": True}).json()
    by = {(r["exchange"], r["symbol"]): r for r in data["issues"]}
    assert set(by) == {
        ("NSE", "EVEREST"),
        ("BSE", "SHIVCHEM"),
        ("BSE", "TNA"),
    }  # Everestims only once, from NSE
    s = by[("BSE", "SHIVCHEM")]
    assert (s["phase"], s["series"], s["lot_size"], s["min_lots"], s["bse_ipo_no"]) == (
        "open",
        "SME",
        2000,
        2,
        8008,
    )
    assert by[("BSE", "TNA")]["phase"] == "upcoming" and data["errors"] == [
        "bse: BSE IPO 8007 details: HTTP 500"
    ]


# --------------------------------------------------------------------------- review fixes
def test_finished_worker_is_reaped_and_not_reported_alive(tmp_path):
    import subprocess
    import sys
    import time

    from finresearch.api import workers

    pid = workers._popen([sys.executable, "-c", "pass"], tmp_path / "w.log")
    stray = subprocess.Popen([sys.executable, "-c", "pass"])  # a child the registry does not know
    workers._children.pop(stray.pid, None)
    time.sleep(1.0)  # both have exited: zombies until someone waits for them
    assert workers.pid_alive(pid) is False and workers.pid_alive(stray.pid) is False
    assert workers.pid_alive(os.getpid()) is True  # not our child: kill(pid, 0) decides


def test_resume_clicked_twice_starts_one_worker(env, seeded):
    import threading

    from finresearch.api.workers import WorkerBusy

    first_spawning, release, pids = threading.Event(), threading.Event(), []

    def popen(argv, log):
        pids.append(argv)
        first_spawning.set()
        release.wait(5)
        return os.getpid()  # a live pid

    sp = Spawner(popen=popen)
    results: list[object] = []

    def click():
        try:
            results.append(sp.start(seeded["run_id"]))
        except WorkerBusy as e:
            results.append(e)

    a = threading.Thread(target=click)
    a.start()
    assert first_spawning.wait(5)
    b = threading.Thread(target=click)
    b.start()
    b.join(0.5)  # blocked on the row lock while the first click spawns
    release.set()
    a.join(5)
    b.join(5)
    assert len(pids) == 1 and sum(isinstance(r, WorkerBusy) for r in results) == 1


def test_unsafe_requests_from_other_origins_are_refused(client, seeded):
    rid, dash = seeded["run_id"], "http://127.0.0.1:3100"
    evil = client.post(f"/api/runs/{rid}/resume", headers={"origin": "https://evil.example"})
    assert evil.status_code == 403 and not client.spawner.calls
    assert client.post(f"/api/runs/{rid}/resume", headers={"origin": "null"}).status_code == 403
    no_header = client.post(f"/api/runs/{rid}/resume", headers={"origin": dash})
    assert no_header.status_code == 403 and no_header.headers["access-control-allow-origin"] == dash
    cross = client.post("/api/alerts/1/read", headers={"sec-fetch-site": "cross-site"})
    assert cross.status_code == 403
    ok = client.post(f"/api/runs/{rid}/resume", headers={"origin": dash, "x-finresearch": "1"})
    assert ok.status_code == 200 and len(client.spawner.calls) == 1
    assert client.post(f"/api/runs/{rid}/resume").status_code == 200  # CLI/curl: no Origin, not a browser
    pre = client.options(f"/api/runs/{rid}/resume", headers={"origin": dash, "access-control-request-method": "POST",
                                                            "access-control-request-headers": "content-type,x-finresearch"})  # fmt: skip
    assert pre.status_code == 200 and "x-finresearch" in pre.headers["access-control-allow-headers"].lower()
    assert client.get("/api/health", headers={"origin": "https://evil.example"}).status_code == 200


def test_start_run_errors_and_suggest_only_for_ipo_reports(client, seeded, monkeypatch):
    from finresearch.db import session_scope
    from finresearch.db.models import ResearchRun

    assert client.post("/api/runs", json={"company": "nope"}).status_code == 404
    assert client.post("/api/runs", json={"company": seeded["slug"], "kind": "bogus"}).status_code == 422
    wrong = client.post("/api/runs", json={"company": seeded["slug"], "kind": "fund_report"})  # KindMismatch
    assert wrong.status_code == 422 and "fund" in wrong.json()["detail"]
    with session_scope() as s:
        s.get(ResearchRun, seeded["run_id"]).kind = "stock_report"
    r = client.post(f"/api/runs/{seeded['run_id']}/suggest")
    assert r.status_code == 422 and "IPO" in r.json()["detail"] and not client.router.tasks
    with session_scope() as s:
        s.get(ResearchRun, seeded["run_id"]).kind = "ipo_report"

    from pydantic import BaseModel

    class M(BaseModel):
        lots: int

    async def bad_suggest(run_id, **kw):
        M.model_validate({"lots": "many"})

    monkeypatch.setattr("finresearch.suggest.advisor.suggest", bad_suggest)
    r = client.post(f"/api/runs/{seeded['run_id']}/suggest")
    assert r.status_code == 422 and "ValidationError" in r.json()["detail"]
    assert client.post("/api/runs/999999/suggest").status_code == 404


def test_blank_question_is_rejected_before_claude(client, seeded):
    r = client.post(f"/api/runs/{seeded['run_id']}/ask", json={"question": "   \n "})
    assert r.status_code == 422 and not client.router.tasks


def test_server_errors_are_json_with_cors_and_upstream_errors_are_502(env, monkeypatch):
    from finresearch.adapters import nse
    from finresearch.adapters.nse import NseError
    from finresearch.api import create_app

    dash = {"origin": "http://localhost:3100"}

    async def broken_list():
        raise RuntimeError("boom")

    class DownFno:
        async def __aenter__(self):
            raise NseError("NSE HTTP 403")

        async def __aexit__(self, *a):
            return None

    async def down_detail(symbol):
        raise NseError("NSE refused")

    class DownNse:
        async def __aenter__(self):
            raise NseError("NSE warm-up failed")

        async def __aexit__(self, *a):
            return None

    monkeypatch.setattr(nse, "NseClient", DownNse)
    monkeypatch.setattr("finresearch.adapters.bse.sme_radar", no_bse)
    app = create_app(equity_list=broken_list, fno_client=DownFno, nse_detail=down_detail)
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.get("/api/stocks/search", params={"q": "infy"}, headers=dash)
        assert r.status_code == 500 and "boom" in r.json()["detail"]
        assert r.headers["access-control-allow-origin"] == dash["origin"]
        r = c.get("/api/fno/NIFTY/expiries", headers=dash)
        assert r.status_code == 502 and r.headers["access-control-allow-origin"] == dash["origin"]
        radar = c.get("/api/ipos", params={"refresh": True})
        assert radar.status_code == 200 and "warm-up" in radar.json()["errors"][0]
        from finresearch.db import session_scope
        from finresearch.ingest.documents import get_or_create_company

        with session_scope() as s:
            get_or_create_company(s, "watch-down", "Watch Down", nse_symbol="WDOWN")
        assert c.post("/api/watches", json={"company": "watch-down"}).status_code == 502


def test_add_company_never_maps_a_symbol_onto_another_company(env):
    from finresearch.api import create_app
    from finresearch.db import session_scope
    from finresearch.db.models import Company
    from finresearch.ingest.documents import get_or_create_company

    with session_scope() as s:
        for slug in ("zeta-widgets", "zeta-widgets-zetb"):
            if (co := s.query(Company).filter_by(slug=slug).one_or_none()) is not None:
                co.slug = f"{slug}-old{co.id}"
        s.query(Company).filter(Company.nse_symbol.in_(["ZETA", "ZETB"])).update({"nse_symbol": None})
        get_or_create_company(s, "zeta-widgets", "Zeta Widgets Ltd")  # added from documents, no symbol yet
    with TestClient(create_app()) as c:
        r = c.post("/api/companies", json={"nse_symbol": "zeta", "name": "Zeta Widgets Limited"}).json()
        assert (r["slug"], r["created"], r["nse_symbol"]) == ("zeta-widgets", False, "ZETA")
        r = c.post("/api/companies", json={"nse_symbol": "ZETB", "name": "Zeta Widgets Limited"}).json()
        assert (r["slug"], r["created"]) == ("zeta-widgets-zetb", True)
    with session_scope() as s:
        assert s.query(Company).filter_by(slug="zeta-widgets").one().nse_symbol == "ZETA"
        assert s.query(Company).filter_by(slug="zeta-widgets-zetb").one().nse_symbol == "ZETB"


def test_stale_running_run_ends_the_stream_and_runs_list_research_kinds_only(client, seeded):
    from finresearch.db import session_scope
    from finresearch.db.models import ResearchRun

    with session_scope() as s:
        s.get(ResearchRun, seeded["run_id"]).status = "running"  # no worker: e.g. a hard-killed one
        stale = ResearchRun(kind="smoke_mcp", status="running", manifest={})
        s.add(stale)
        s.flush()
        stale_id = stale.id
    with client.stream("GET", f"/api/runs/{seeded['run_id']}/events") as r:
        events = _sse(r.read().decode())
    assert [e for e, _ in events] == ["snapshot", "end"] and events[-1][1]["status"] == "running"
    rows = client.get("/api/runs", params={"limit": 500}).json()
    assert stale_id not in [r["id"] for r in rows]
    mine = next(r for r in rows if r["id"] == seeded["run_id"])
    assert mine["has_report"] is True
    assert client.post(f"/api/runs/{seeded['run_id']}/resume").status_code == 200


def test_companies_report_the_research_kind(client, seeded):
    from finresearch.db import session_scope
    from finresearch.ingest.documents import get_or_create_company

    tag = seeded["slug"][4:]
    with session_scope() as s:
        get_or_create_company(s, f"mf-{tag}", "Some Fund")
        get_or_create_company(s, f"bond-{tag}", "Some Bond")
        get_or_create_company(s, f"stk-{tag}", "Some Stock").meta = {"kind": "stock_report"}
        get_or_create_company(s, f"ipo-{tag}", "Some IPO")
    kinds = {c["slug"]: c["kind"] for c in client.get("/api/companies").json()}
    assert kinds[f"mf-{tag}"] == "fund_report" and kinds[f"bond-{tag}"] == "bond_report"
    assert kinds[f"stk-{tag}"] == "stock_report" and kinds[f"ipo-{tag}"] == "ipo_report"
    assert kinds[seeded["slug"]] == "ipo_report"  # from its latest run


def test_bonds_search_and_add(env):
    from finresearch.adapters.nse_bonds import parse_live_bonds
    from finresearch.api import create_app

    fixture = Path(__file__).parent / "fixtures" / "nse" / "bonds_live_trimmed.json"

    async def rows():
        return parse_live_bonds(json.loads(fixture.read_text()))

    with TestClient(create_app(bonds=rows)) as c:
        hits = c.get("/api/bonds", params={"q": "INE906B07DF8"}).json()
        assert hits[0]["symbol"] == "875NHAI29" and hits[0]["slug"] in (None, "bond-ine906b07df8")
        assert len(c.get("/api/bonds").json()) > 0
        made = c.post("/api/bonds", json={"isin": "INE906B07DF8"})
        assert made.status_code == 201 and made.json()["slug"] == "bond-ine906b07df8"
        assert c.get("/api/bonds", params={"q": "875NHAI29"}).json()[0]["slug"] == "bond-ine906b07df8"
        assert c.post("/api/bonds", json={"isin": "INE000000000"}).status_code == 404
        kinds = {x["slug"]: x["kind"] for x in c.get("/api/companies").json()}
        assert kinds["bond-ine906b07df8"] == "bond_report"

"""A run-bound MCP server (#393 follow-up): the server launched for one agent task is bound to its research run
(mcp_server.config.RUN_ENV), and every tool that takes a run_id refuses any other run with a clear error, reading and
writing nothing of it. An unbound server (tests, scripts, the advisor, Ask) is not restricted. Synthetic data only."""

from __future__ import annotations

import asyncio
import json

import pytest


def _run(tmp_path, name: str) -> int:
    from finresearch.db import session_scope
    from finresearch.db.models import Claim, ResearchRun
    from finresearch.ingest.documents import get_or_create_company

    with session_scope() as s:
        co = get_or_create_company(s, f"br-{name}-" + tmp_path.name[-8:], f"Bound Run {name} Co")
        r = ResearchRun(company_id=co.id, kind="stock_report", manifest={})
        s.add(r)
        s.flush()
        s.add(
            Claim(
                run_id=r.id, stream="stock_technical", statement=f"MARKER-{name}-CLAIM", claim_type="opinion"
            )
        )
        return r.id


@pytest.fixture
def bound(env, tmp_path, monkeypatch):
    """(my run, another run), with the server bound to mine; fetching a page fails the test."""
    from finresearch.mcp_server.config import RUN_ENV
    from finresearch.verify import web

    async def no_fetch(url):
        raise AssertionError(f"a page was fetched for another run: {url}")

    monkeypatch.setattr(web, "FETCHER", no_fetch)
    mine, theirs = _run(tmp_path, "MINE"), _run(tmp_path, "THEIRS")
    monkeypatch.setenv(RUN_ENV, str(mine))
    return mine, theirs


def _refused(out: str, mine: int, theirs: int) -> None:
    err = json.loads(out)["error"]
    assert f"bound to run {mine}" in err and f"run {theirs}" in err
    assert "MARKER" not in out


def test_list_claims_refuses_another_run(bound):
    from finresearch.mcp_server import server

    mine, theirs = bound
    _refused(server.list_claims(theirs), mine, theirs)
    assert "MARKER-MINE-CLAIM" in server.list_claims(mine)


def test_identity_checks_refuses_another_run(bound):
    from finresearch.mcp_server import server

    mine, theirs = bound
    _refused(server.identity_checks(theirs, only_failing=False), mine, theirs)
    assert "error" not in json.loads(server.identity_checks(mine, only_failing=False))


def test_fetch_page_refuses_another_run_before_fetching_or_storing(bound):
    from sqlalchemy import func, select

    from finresearch.db import session_scope
    from finresearch.db.models import WebSnapshot
    from finresearch.mcp_server import server

    mine, theirs = bound
    _refused(asyncio.run(server.fetch_page("https://example.com/page", theirs)), mine, theirs)
    with session_scope() as s:
        assert (
            s.scalar(select(func.count()).select_from(WebSnapshot).where(WebSnapshot.run_id == theirs)) == 0
        )


def test_every_tool_taking_a_run_id_is_guarded():
    """A new tool with a run_id parameter must join the guarded set (and get a test above)."""
    from finresearch.mcp_server import server

    tools = asyncio.run(server.server.list_tools())
    with_run = {t.name for t in tools if "run_id" in (t.input_schema.get("properties") or {})}
    assert (
        with_run
        == set(server.RUN_BOUND_TOOLS)
        == {"save_claim", "list_claims", "identity_checks", "fetch_page"}
    )


def test_an_unbound_server_is_not_restricted(env, tmp_path, monkeypatch):
    from finresearch.mcp_server import server
    from finresearch.mcp_server.config import RUN_ENV

    monkeypatch.delenv(RUN_ENV, raising=False)
    theirs = _run(tmp_path, "OPEN")
    assert "MARKER-OPEN-CLAIM" in server.list_claims(theirs)


def test_a_bound_server_refuses_to_create_runs(bound):
    """Agents never create runs: a run-bound server's start_run is refused with a clear error and writes nothing."""
    from sqlalchemy import func, select

    from finresearch.db import session_scope
    from finresearch.db.models import ResearchRun
    from finresearch.mcp_server import server

    mine, _ = bound
    with session_scope() as s:
        before = s.scalar(select(func.count()).select_from(ResearchRun))
    err = json.loads(server.start_run("anything", note="x"))["error"]
    assert f"bound to run {mine}" in err and "cannot create runs" in err
    with session_scope() as s:
        assert s.scalar(select(func.count()).select_from(ResearchRun)) == before


def test_an_unbound_server_may_still_create_a_run(env, monkeypatch):
    from finresearch.mcp_server import server
    from finresearch.mcp_server.config import RUN_ENV

    monkeypatch.delenv(RUN_ENV, raising=False)
    assert "run_id" in json.loads(server.start_run("no-such-company"))

"""#505: the `fund_sip_outcome` MCP tool and its citation. The fund prompt asked for "fincalc funds.sip_outcome",
which needs a NAV series no agent can pass; the tool reads AMFI's NAVs itself, stores the series it used, and
save_claim re-runs it on that stored series (grade D).

Synthetic scheme and NAVs only; AMFI is replaced, nothing touches the network. Expected values come from the rule that
generates the NAVs (a fund growing at exactly r a year has an XIRR of r on any schedule), never from fincalc.
"""

from __future__ import annotations

import ast
import json
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

CODE = "999001"
RATE = 0.12


def _navs(first: date, last: date, rate: float = RATE) -> list[tuple[date, Decimal]]:
    """A NAV on every weekday from `first` to `last`: 100 growing at exactly `rate` a year, on actual days / 365."""
    out, d = [], first
    while d <= last:
        if d.weekday() < 5:
            out.append((d, Decimal(str(round(100 * (1 + rate) ** ((d - first).days / 365), 10)))))
        d += timedelta(days=1)
    return out


def _weekday_on_or_after(d: date) -> date:
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


FULL = _navs(date(2022, 12, 1), date(2025, 12, 31))


def _series(navs, start: date, end: date) -> tuple[dict, str]:
    """A series as the tool stores it (the test database)."""
    from finresearch.db import session_scope
    from finresearch.mcp_server import fund_facts

    scheme = {"scheme_code": CODE, "name": "Synthetic Flexi Cap Fund", "plan": "Direct Plan",
              "option": "Growth Option"}  # fmt: skip
    with session_scope() as s:
        return fund_facts.store_series(s, scheme, start, end, navs, run_id=None)


def _sip(navs, start: date, end: date, amount="10000", day: int = 5) -> dict:
    from finresearch.mcp_server import fund_facts

    series, sha = _series([(d, v) for d, v in navs if start - timedelta(days=10) <= d <= end], start, end)
    return fund_facts.sip(series, sha, amount, day)


# --------------------------------------------------------------------------- the outcome, by hand
def test_three_year_sip_on_a_fund_growing_twelve_percent_a_year(env):
    """36 instalments of 10,000 on the 5th (the next weekday when the 5th is a weekend), Jan-2023 to Dec-2025.
    Invested 36 x 10,000 = 3,60,000. Each instalment grows at 12 % a year from its NAV date to 31-Dec-2025, so the
    value is the sum of 10000 x 1.12^(days / 365) and the XIRR is 12 % whatever the dates."""
    start, end = date(2023, 1, 1), date(2025, 12, 31)
    out = _sip(FULL, start, end)
    fills = [_weekday_on_or_after(date(y, m, 5)) for y in (2023, 2024, 2025) for m in range(1, 13)]
    want = sum(10000 * (1 + RATE) ** ((end - d).days / 365) for d in fills)
    assert out["known"] is True and out["instalments"] == 36 and out["invested"] == "360000.00"
    assert abs(Decimal(out["value"]) - Decimal(str(want))) < Decimal("0.05")
    assert Decimal(out["gain"]) == Decimal(out["value"]) - 360000
    assert Decimal(out["xirr_pct"]) == Decimal("12.0000")
    # the NAV dates it used: 5-Jan-2023 is a Thursday; 5-Feb-2023 a Sunday, bought at Monday's NAV
    assert out["instalment_navs"][0][:2] == ["2023-01-05", "2023-01-05"]
    assert out["instalment_navs"][1][:2] == ["2023-02-05", "2023-02-06"]
    assert [x[1] for x in out["instalment_navs"]] == [d.isoformat() for d in fills]
    assert out["valuation"]["nav_date"] == "2025-12-31" and out["instalment_dates_without_a_nav_yet"] == []
    assert out["citable_keys"] == ["xirr_pct", "value", "invested", "gain"]


def test_one_instalment_is_plain_arithmetic(env):
    """10,000 at NAV 100 buys 100 units; 20 days later at NAV 101 they are worth 10,100: a gain of 100, and an
    annualised 1.01^(365 / 20) - 1 = 19.91 %."""
    navs = [(date(2025, 3, 3) + timedelta(days=i), Decimal("100") + Decimal(i) / 20) for i in range(21)]
    out = _sip(navs, date(2025, 3, 3), date(2025, 3, 23), day=3)
    assert (out["instalments"], out["invested"], out["units"]) == (1, "10000.00", "100.0000")
    assert (out["value"], out["gain"]) == ("10100.00", "100.00")
    assert abs(Decimal(out["xirr_pct"]) - Decimal(str((1.01 ** (365 / 20) - 1) * 100))) < Decimal("0.001")
    assert out["valuation"] == {"nav_date": "2025-03-23", "nav": "101"}


# --------------------------------------------------------------------------- unknown is never a number
def test_a_scheme_younger_than_the_period_is_unknown(env):
    """fincalc buys each instalment at the first NAV on or after its date: for a scheme launched in March 2024 every
    2023 instalment would be bought on launch day and the XIRR would look precise. The tool refuses instead."""
    young = [(d, v) for d, v in FULL if d >= date(2024, 3, 11)]
    out = _sip(young, date(2023, 1, 1), date(2025, 12, 31))
    assert set(out) == {"known", "reason"} and out["known"] is False
    assert "2024-03-11" in out["reason"] and "younger than the SIP period" in out["reason"]
    # from its first full month it answers
    assert _sip(young, date(2024, 4, 1), date(2025, 12, 31))["instalments"] == 21


def test_missing_navs_are_unknown_and_an_unpublished_last_nav_is_reported(env):
    start, end = date(2023, 1, 1), date(2025, 12, 31)
    holed = [(d, v) for d, v in FULL if not date(2024, 6, 3) <= d <= date(2024, 6, 25)]
    out = _sip(holed, start, end)
    assert set(out) == {"known", "reason"} and "2024-06-05" in out["reason"] and "missing" in out["reason"]
    stale = _sip([(d, v) for d, v in FULL if d <= date(2025, 11, 28)], start, end)
    assert set(stale) == {"known", "reason"} and "2025-11-28" in stale["reason"]
    # the December instalment's NAV is not out yet: it is not invested, and the answer says so
    partial = _sip([(d, v) for d, v in FULL if d <= date(2025, 12, 4)], start, date(2025, 12, 8))
    assert partial["instalments"] == 35 and partial["instalment_dates_without_a_nav_yet"] == ["2025-12-05"]
    assert partial["valuation"]["nav_date"] == "2025-12-04"
    for bad in ({"amount": "0"}, {"amount": "ten"}, {"day": 0}, {"day": 32}):
        with pytest.raises(ValueError):
            _sip(FULL, start, end, **bad)


# --------------------------------------------------------------------------- the tool, with AMFI replaced
@pytest.fixture
def amfi(monkeypatch):
    """AMFI's NAV file and history report answered from `state["navs"]`; records the date ranges asked for."""
    from finresearch.adapters.amfi import AmfiClient, SchemeNav
    from finresearch.mcp_server import server

    state = {"navs": FULL, "asked": []}

    def row(d, v):
        return SchemeNav(code=CODE, name="Synthetic Flexi Cap Fund", plan="Direct Plan", option="Growth Option",
                         isin_growth="INF000X01ZZ9", isin_reinvest=None, nav=v, day=d,
                         category="Equity Scheme - Flexi Cap Fund", amc="Synthetic Mutual Fund")  # fmt: skip

    async def nav_all(client):
        return [row(*state["navs"][-1])]

    async def history(self, scheme, start, end, probe):
        state["asked"].append((start, end))
        return [row(d, v) for d, v in state["navs"] if start <= d <= end]

    monkeypatch.setattr(server, "_nav_all", nav_all)
    monkeypatch.setattr(AmfiClient, "scheme_history", history)
    return state


async def _tool(**args) -> dict:
    from finresearch.mcp_server import server

    return json.loads(await server.fund_sip_outcome(CODE, "10000", **args))


def _run(tmp_path) -> int:
    from finresearch.db import session_scope
    from finresearch.db.models import ResearchRun
    from finresearch.ingest.documents import get_or_create_company

    with session_scope() as s:
        co = get_or_create_company(s, "sip-" + tmp_path.name[-8:], "Synthetic Flexi Cap Fund")
        r = ResearchRun(company_id=co.id, kind="fund_report", manifest={})
        s.add(r)
        s.flush()
        return r.id


def _save(run_id: int, cite: dict, value: str, key: str = "xirr_pct", unit: str = "%", **kw) -> dict:
    """save_claim as the agent calls it (the MCP tool)."""
    from finresearch.mcp_server import server

    return json.loads(server.save_claim(run_id, "fund_performance", "A 10,000 monthly SIP over 3 years", "numeric",
                                        [{**cite, "result_key": key}], metric=f"sip_3y_{key}", value=value,
                                        unit=unit, period="2022-12-31 to 2025-12-31", **kw))  # fmt: skip


def _claim(claim_id: int):
    from finresearch.db import session_scope
    from finresearch.db.models import Claim
    from finresearch.verify.evidence import claim_grade

    with session_scope() as s:
        c = s.get(Claim, claim_id)
        return claim_grade(c), c.status, c.citations[0].computation, c.citations[0].url


async def test_the_tool_resolves_the_period_stores_the_series_and_returns_a_citation(env, amfi):
    from finresearch.db import session_scope
    from finresearch.db.models import WebSnapshot
    from finresearch.mcp_server import fund_facts

    out = await _tool(years=3)
    # the last three years to the latest NAV (31-Dec-2025), fetched with ten days of lead-in
    assert amfi["asked"] == [(date(2022, 12, 21), date(2025, 12, 31))]
    args = out["citation"]["fund_fact"]["args"]
    assert out["known"] is True and out["scheme_code"] == CODE and out["scheme"] == "Synthetic Flexi Cap Fund"
    assert {k: args[k] for k in ("scheme_code", "amount", "start", "end", "day_of_month")} == {
        "scheme_code": CODE, "amount": "10000", "start": "2022-12-31", "end": "2025-12-31", "day_of_month": 1}  # fmt: skip
    # 1-Jan-2023 to 1-Dec-2025 is 36 instalments; at exactly 12 % a year the XIRR is 12 %
    assert out["instalments"] == 36 and Decimal(out["xirr_pct"]) == Decimal("12.0000")
    prov = out["provenance"]
    assert prov["source_url"].startswith("https://portal.amfiindia.com/")
    assert prov["method_version"] == "fund_facts/1"
    assert (prov["nav_first"], prov["nav_last"]) == ("2022-12-21", "2025-12-31")
    with session_scope() as s:
        snap = s.query(WebSnapshot).filter_by(sha256=args["series_sha256"]).first()
        key = fund_facts.series_key(CODE, date(2022, 12, 31), date(2025, 12, 31))
        assert snap.url == key == prov["series_key"]
        assert json.loads(snap.text)["navs"][-1] == ["2025-12-31", str(FULL[-1][1])]
    again = await _tool(start="2022-12-31", end="2025-12-31")  # the same period, spelt out: the same series
    assert again["citation"] == out["citation"] and again["value"] == out["value"]


async def test_refused_periods_unknown_schemes_and_a_failed_fetch_carry_no_number(env, amfi, monkeypatch):
    from finresearch.adapters.amfi import AmfiClient, AmfiError
    from finresearch.mcp_server import server

    for args, why in (({}, "either years"), ({"years": 3, "start": "2023-01-01"}, "either years"),
                      ({"start": "2026-01-01"}, "must be before end"), ({"years": 0}, "between 1 and 10"),
                      ({"years": 11}, "between 1 and 10"), ({"start": "2015-12-30"}, "at most 10 years"),
                      ({"years": 3, "day_of_month": 40}, "day_of_month")):  # fmt: skip
        out = await _tool(**args)
        assert set(out) == {"known", "reason"} and why in out["reason"], (args, out)
    free = json.loads(await server.fund_sip_outcome(CODE, "0", years=3))
    assert set(free) == {"known", "reason"} and "amount must be > 0" in free["reason"]
    other = json.loads(await server.fund_sip_outcome("123456", "10000", years=3))
    assert other == {"known": False, "reason": "scheme 123456 is not in AMFI's NAV file"}
    amfi["navs"] = [(d, v) for d, v in FULL if d >= date(2024, 3, 11)]
    young = await _tool(years=3)
    assert set(young) == {"known", "reason"} and "younger than the SIP period" in young["reason"]

    async def down(self, scheme, start, end, probe):
        raise AmfiError("AMFI HTTP 503 for the history report")

    monkeypatch.setattr(AmfiClient, "scheme_history", down)
    out = await _tool(years=1)
    assert set(out) == {"known", "reason"} and "could not be read" in out["reason"] and "503" in out["reason"]
    # nothing was fetched for a refused amount, day, period or scheme: only the young scheme's three years were
    assert amfi["asked"] == [(date(2022, 12, 21), date(2025, 12, 31))]


async def test_under_a_run_bound_server_the_series_belongs_to_the_run_and_the_claim_is_graded_d(
    env, tmp_path, amfi, monkeypatch
):
    """An agent's MCP server is always bound to its run (mcp_server.config.RUN_ENV): the series is stored with that
    run, save_claim under the binding re-runs it, and the bound server still refuses to write into another run."""
    from finresearch.db import session_scope
    from finresearch.db.models import WebSnapshot
    from finresearch.mcp_server.config import RUN_ENV

    mine, theirs = _run(tmp_path), _run(tmp_path / "theirs")
    monkeypatch.setenv(RUN_ENV, str(mine))
    out = await _tool(years=3)
    sha = out["provenance"]["series_sha256"]
    with session_scope() as s:
        assert [x.run_id for x in s.query(WebSnapshot).filter_by(sha256=sha, run_id=mine)] == [mine]
    res = _save(mine, out["citation"], out["xirr_pct"])
    assert _claim(res["claim_id"])[0]["grade"] == "D"
    other = _save(theirs, out["citation"], out["xirr_pct"])
    assert f"bound to run {mine}" in other["error"]


# --------------------------------------------------------------------------- citing it: save_claim re-runs it (#242)
async def test_a_claim_citing_the_tool_is_rerun_on_its_series_and_graded_d(env, tmp_path, amfi, monkeypatch):
    from finresearch.adapters.amfi import AmfiClient

    run_id = _run(tmp_path)
    out = await _tool(years=3)

    async def no_network(self, scheme, start, end, probe):
        raise AssertionError("save_claim must not fetch")

    monkeypatch.setattr(AmfiClient, "scheme_history", no_network)
    res = _save(run_id, out["citation"], "12.00")
    g, status, comp, url = _claim(res["claim_id"])
    assert g["grade"] == "D" and "stored AMFI NAV series" in g["label"] and status == "unverified"
    assert comp["kind"] == "fund_fact" and comp["function"] == "fund_fact:fund_sip_outcome"
    assert comp["matches"] is True and comp["inputs"] == [] and comp["result"] == "12.0000"
    assert comp["provenance"]["series_sha256"] == out["provenance"]["series_sha256"]
    assert url.startswith("fund_fact:fund_sip_outcome(") and comp["method_version"] == "fund_facts/1"
    # the value in rupees, rounded as a report states it
    rupees = _save(run_id, out["citation"], str(round(Decimal(out["value"]))), key="value", unit="INR")
    assert _claim(rupees["claim_id"])[0]["grade"] == "D"
    # a misstated figure is unsupported, with what the tool gives
    wrong = _save(run_id, out["citation"], "14.50")
    g, status, comp, _ = _claim(wrong["claim_id"])
    assert (g["grade"], status, comp["matches"]) == ("U", "unsupported", False)
    assert "12.0000" in comp["detail"]


async def test_only_the_series_the_tool_stored_can_be_cited(env, tmp_path, amfi):
    """The series is found by its key AND sha256. A made-up sha, another period, or the same NAVs saved as a web page
    (the only snapshots an agent can write, through fetch_page, sit under http(s) URLs) are refused."""
    from finresearch.adapters.http import UnsafeURLError, check_public_url
    from finresearch.db import session_scope
    from finresearch.db.models import Claim, WebSnapshot
    from finresearch.mcp_server import fund_facts
    from finresearch.verify import web

    run_id = _run(tmp_path)
    out = await _tool(years=3)
    args = out["citation"]["fund_fact"]["args"]
    key = fund_facts.series_key(CODE, date(2022, 12, 31), date(2025, 12, 31))
    with pytest.raises(UnsafeURLError):  # what fetch_page checks before it fetches or stores anything
        check_public_url(key)

    def cite(**changed):
        return {"fund_fact": {"tool": "fund_sip_outcome", "args": {**args, **changed}}}

    with session_scope() as s:  # a faster-growing series planted as a page: same period, its own sha
        rich = json.loads(s.query(WebSnapshot).filter_by(sha256=args["series_sha256"]).first().text)
        rich["navs"] = [[d, str(Decimal(v) * 3)] if d > "2025-06-30" else [d, v] for d, v in rich["navs"]]
        page = web.store(s, "https://evil.example/navs.json", json.dumps(rich, sort_keys=True), run_id=run_id)
        planted = page.sha256
    for bad, why in ((cite(series_sha256="0" * 64), "no stored NAV series"),
                     (cite(series_sha256=planted), "no stored NAV series"),
                     (cite(start="2023-06-30"), "no stored NAV series"),
                     (cite(scheme_code="123456"), "no stored NAV series"),
                     ({"fund_fact": {"tool": "fund_sip_outcome",
                                     "args": {k: v for k, v in args.items() if k != "series_sha256"}}}, "series_sha256"),
                     (cite(run_id=run_id), "its args are"),
                     ({"fund_fact": {"tool": "fund_nav_guess", "args": args}}, "unknown fund fact tool")):  # fmt: skip
        res = _save(run_id, bad, "12.00")
        assert why in res["error"], (bad, res)
    assert "result_key" in _save(run_id, cite(), "12.00", key="units")["error"]  # not a citable key
    with session_scope() as s:
        assert s.query(Claim).filter_by(run_id=run_id).count() == 0  # nothing of a refused claim is kept


async def test_an_unknown_answer_cannot_be_cited(env, tmp_path, amfi):
    amfi["navs"] = [(d, v) for d, v in FULL if d >= date(2024, 3, 11)]
    run_id = _run(tmp_path)
    assert (await _tool(years=3))["known"] is False
    # the series of that call is stored; citing it by hand still gets the tool's answer: unknown
    from finresearch.db import session_scope
    from finresearch.db.models import WebSnapshot
    from finresearch.mcp_server import fund_facts

    key = fund_facts.series_key(CODE, date(2022, 12, 31), date(2025, 12, 31))
    with session_scope() as s:  # the young scheme's series (the full history sits under the same key)
        (sha,) = {x.sha256 for x in s.query(WebSnapshot).filter_by(url=key)
                  if json.loads(x.text)["navs"][0][0] == "2024-03-11"}  # fmt: skip
    cite = {"fund_fact": {"tool": "fund_sip_outcome",
                          "args": {"scheme_code": CODE, "amount": "10000", "start": "2022-12-31", "end": "2025-12-31",
                                   "day_of_month": 1, "series_sha256": sha}}}  # fmt: skip
    res = _save(run_id, cite, "31.00")
    assert "the fund fact is unknown, so it cannot be cited" in res["error"] and "younger" in res["error"]


async def test_regrade_reruns_the_stored_series_and_fails_closed_when_it_is_gone(env, tmp_path, amfi):
    """`evidence regrade --all` must send a fund fact to its own re-run (run_fincalc would call it an unknown function
    and turn a sound citation U), find the series whatever its age, and say so when the series no longer exists."""
    from datetime import UTC, datetime

    from finresearch.db import session_scope
    from finresearch.db.models import Claim, WebSnapshot
    from finresearch.verify.regrade import regrade

    run_id = _run(tmp_path)
    out = await _tool(years=3)
    res = _save(run_id, out["citation"], "12.00")
    sha = out["provenance"]["series_sha256"]
    with session_scope() as s:  # months later: a tool's snapshot counts for a web quote for one day only
        s.query(WebSnapshot).filter_by(sha256=sha).update({"fetched_at": datetime(2026, 1, 2, tzinfo=UTC)})
    with session_scope() as s:
        mine = [c for c in regrade(s, all_d=True).citations if c.claim_id == res["claim_id"]]
        assert [c.grade for c in mine] == ["D"] and not mine[0].changed
        s.rollback()
    with session_scope() as s:
        s.query(WebSnapshot).filter_by(sha256=sha).delete()
    with session_scope() as s:
        mine = [c for c in regrade(s, all_d=True).citations if c.claim_id == res["claim_id"]]
        assert [c.grade for c in mine] == ["U"] and mine[0].changed
        assert "could not be re-checked" in mine[0].reason and "no stored NAV series" in mine[0].reason
        s.rollback()
    with session_scope() as s:  # one database per session: leave no citation that re-checks differently
        s.query(Claim).filter_by(id=res["claim_id"]).delete()


async def test_the_blind_verifier_sees_where_a_fund_fact_points_but_not_its_value(env, tmp_path, amfi):
    from finresearch.bridge.limits import LimitTracker
    from finresearch.orchestrator.fund import FundPipeline

    run_id = _run(tmp_path)
    out = await _tool(years=3)
    res = _save(run_id, out["citation"], out["value"], key="value", unit="INR")
    pipe = FundPipeline(run_id, runner=lambda *a, **k: None, tracker=LimitTracker(tmp_path / "lim"))
    (item,) = json.loads(pipe._blind_claims_text([res["claim_id"]]))
    assert item["citations"] == [{"fund_fact": "fund_sip_outcome", "result_key": "value",
                                  "args": out["citation"]["fund_fact"]["args"]}]  # fmt: skip
    assert out["value"] not in json.dumps(item) and out["value"].split(".")[0] not in json.dumps(item)


# --------------------------------------------------------------------------- least privilege and privacy
def test_the_fund_roles_hold_the_tool_and_the_prompt_names_it():
    import asyncio

    from finresearch.agents.roles import FUNDS, MCP, ROLES
    from finresearch.mcp_server import fund_facts, server

    holders = {name for name, r in ROLES.items() if f"{MCP}fund_sip_outcome" in r.tools}
    assert holders == {name for name, r in ROLES.items() if set(FUNDS) <= set(r.tools)}
    assert holders == {"fund_planner", "fund_performance", "fund_risk", "fund_costs", "fund_manager"}
    assert f"{MCP}save_claim" in ROLES["fund_performance"].tools  # it can cite what it reads
    text = ROLES["fund_performance"].template_text()
    assert "fund_sip_outcome" in text and "funds.sip_outcome" not in text
    registered = {t.name: t for t in asyncio.run(server.server.list_tools())}
    for name in fund_facts.TOOLS:  # the run is never an argument (tests/test_mcp_bound_run.py pins that set)
        assert "run_id" not in registered[name].input_schema["properties"], name
        assert "citation" in registered[name].description and "known=false" in registered[name].description


def test_the_fund_fact_module_reads_public_nav_data_only():
    """Static guard beyond test_privacy_llm's: fincalc, the snapshot table and the snapshot store, nothing else."""
    src = Path(__file__).parents[1] / "src" / "finresearch" / "mcp_server" / "fund_facts.py"
    allowed = ("finresearch.db.models", "finresearch.fincalc", "finresearch.verify", "sqlalchemy")
    std = {"__future__", "calendar", "datetime", "decimal", "json", "typing"}
    bad = []
    for node in ast.walk(ast.parse(src.read_text())):
        mods = ([a.name for a in node.names] if isinstance(node, ast.Import)
                else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])  # fmt: skip
        bad += [m for m in mods if m.split(".")[0] not in std and not m.startswith(allowed)]
        if isinstance(node, ast.ImportFrom) and node.module == "finresearch.db.models":
            bad += [a.name for a in node.names if a.name != "WebSnapshot"]
        if isinstance(node, ast.ImportFrom) and node.module == "finresearch.verify":
            bad += [a.name for a in node.names if a.name != "web"]
    assert not bad, bad

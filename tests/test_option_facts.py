"""Citable option-implied facts as read-only MCP tools (#393): the implied move, the IV30 percentile and the 25-delta
skew read from the stored F&O bhavcopy (fno_bhav) and its derived series (fno_iv_eod), each returned with its source,
trade date, expiry, strikes and method version, and re-run by save_claim (grade D) when a claim cites it. Synthetic
chains only: prices come from the Black-76 engine at a known volatility, so the expected values are independent."""

from __future__ import annotations

import ast
import json
import math
from datetime import date, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest

DAY = date(2026, 3, 2)
NEAR = date(2026, 3, 30)  # 28 days
WEEKLY = date(2026, 3, 5)  # 3 days: below MIN_DTE, never the default expiry
FWD = 22000.0
SIGMA = 0.15
RATE = 0.065  # fincalc.fno_history.RATE, the convention fno_iv_eod is computed with
URL = "https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_20260302_F_0000.csv.zip"


@pytest.fixture(autouse=True)
def _clean(request):
    """Each test starts with no stored F&O rows and leaves no option-fact claims behind (the test database is shared
    by the session, and a later regrade over every grade-D citation must not meet ours)."""
    if "env" not in request.fixturenames:
        yield
        return
    request.getfixturevalue("env")
    from sqlalchemy import text

    from finresearch.db import session_scope

    with session_scope() as s:
        s.execute(text("TRUNCATE fno_bhav, fno_bhav_day, fno_iv_eod"))
    yield
    with session_scope() as s:
        s.execute(text("DELETE FROM claim WHERE id IN (SELECT claim_id FROM citation "
                       "WHERE computation->>'kind' = 'option_fact')"))  # fmt: skip


def _smile(k: float, skew: float) -> float:
    return SIGMA + skew * (FWD - k) / FWD  # puts (low strikes) richer when skew > 0


def _price(right: str, k: float, expiry: date, skew: float = 0.0) -> D:
    from finresearch.fincalc import black76

    t = (expiry - DAY).days / 365
    p = black76.price(right, FWD, k, t, math.exp(-RATE * t), _smile(k, skew))
    return D(str(round(p, 2)))


def _load(symbol: str = "NIFTY", skew: float = 0.0, day: date = DAY, instrument: str = "IDO") -> None:
    """One trading day of a synthetic chain: a future and options on NEAR, a few options on WEEKLY."""
    from finresearch.db import session_scope
    from finresearch.db.models import FnoBhav, FnoBhavDay

    fut = "IDF" if instrument == "IDO" else "STF"
    rows = [FnoBhav(day=day, symbol=symbol, instrument=fut, expiry=NEAR, strike=D(0), option_type="XX",
                    settle=D(str(FWD)), close=D(str(FWD)), contracts=5000, oi=10**6, underlying=D("21900"))]  # fmt: skip
    for expiry, strikes in ((NEAR, range(19000, 25001, 100)), (WEEKLY, range(21800, 22201, 100))):
        for k in strikes:
            for right, ot in (("call", "CE"), ("put", "PE")):
                px = _price(right, k, expiry, skew)
                if px < D("0.05"):
                    continue
                rows.append(FnoBhav(day=day, symbol=symbol, instrument=instrument, expiry=expiry, strike=D(k),
                                    option_type=ot, settle=px, close=px, contracts=100, oi=10**5,
                                    underlying=D("21900")))  # fmt: skip
    with session_scope() as s:
        s.merge(FnoBhavDay(day=day, status="loaded", format="udiff", url=URL, stock_symbols=["ZZSECRET"]))
        s.add_all(rows)


def _run(tmp_path, symbol: str | None = None) -> int:
    from finresearch.db import session_scope
    from finresearch.db.models import ResearchRun
    from finresearch.ingest.documents import get_or_create_company

    with session_scope() as s:
        co = get_or_create_company(s, "of-" + tmp_path.name[-8:], "Synthetic Options Co")
        co.nse_symbol = symbol
        r = ResearchRun(company_id=co.id, kind="stock_report", manifest={})
        s.add(r)
        s.flush()
        return r.id


def _call(tool: str, run_id: int | None = None, **args):
    """A tool call as the server makes it: the run comes from the server's trusted binding, never from the args."""
    from finresearch.db import session_scope
    from finresearch.mcp_server import option_facts

    with session_scope() as s:
        return option_facts.run(s, tool, args, run_id=run_id)


# --------------------------------------------------------------------------- the implied move (golden)
def test_implied_move_golden_from_a_chain_fixture(env, tmp_path):
    _load()
    run_id = _run(tmp_path)
    out = _call("option_implied_move", run_id=run_id, symbol="NIFTY", trade_date="2026-03-02")
    assert out["known"] is True, out
    t = 28 / 365
    # independent: the ATM 1-sigma move is sigma sqrt(T); the ATM straddle on the forward is
    # DF F (2 N(sigma sqrt(T) / 2) - 1) x 2, i.e. as a share of F: 2 DF erf(sigma sqrt(T) / (2 sqrt 2))
    assert float(out["move_1sd_pct"]) == pytest.approx(SIGMA * math.sqrt(t) * 100, abs=0.005)
    straddle = 2 * math.exp(-RATE * t) * math.erf(SIGMA * math.sqrt(t) / (2 * math.sqrt(2))) * 100
    assert float(out["atm_straddle_pct"]) == pytest.approx(straddle, abs=0.005)
    assert float(out["atm_iv_pct"]) == pytest.approx(15.0, abs=0.02)
    p = out["provenance"]
    assert p["trade_date"] == "2026-03-02" and p["expiry"] == "2026-03-30" and p["days_to_expiry"] == 28
    assert p["dataset"].startswith("fno_bhav") and p["source_url"] == URL
    assert p["forward_source"] == "future" and p["strikes_used"] == [22000.0]
    assert p["method_version"] and "fno_history" in p["computation"]
    assert "ZZSECRET" not in str(out)  # never which stock underlyings are stored
    cite = out["citation"]["option_fact"]
    assert cite["tool"] == "option_implied_move"
    assert cite["args"] == {"symbol": "NIFTY", "trade_date": "2026-03-02", "expiry": "2026-03-30"}


def test_explicit_expiry_is_honoured_and_missing_data_is_unknown_with_a_reason(env, tmp_path):
    _load()
    run_id = _run(tmp_path)
    out = _call(
        "option_implied_move", run_id=run_id, symbol="NIFTY", trade_date="2026-03-02", expiry="2026-03-05"
    )
    assert out["provenance"]["expiry"] == "2026-03-05" and out["provenance"]["days_to_expiry"] == 3
    for args in ({"trade_date": "2026-03-03"}, {"trade_date": "2026-03-02", "expiry": "2026-04-27"}):
        out = _call("option_implied_move", run_id=run_id, symbol="NIFTY", **args)
        assert out["known"] is False and out["reason"]
        assert not any(k in out for k in ("move_1sd_pct", "atm_straddle_pct", "atm_iv_pct"))


def test_latest_trade_date_is_resolved_into_the_citation(env, tmp_path):
    _load()
    run_id = _run(tmp_path)
    out = _call("option_implied_move", run_id=run_id, symbol="NIFTY")
    assert out["citation"]["option_fact"]["args"]["trade_date"] == "2026-03-02"


# --------------------------------------------------------------------------- scope: indices and the run's own stock
def test_symbols_outside_the_run_are_refused_without_saying_why(env, tmp_path):
    _load(symbol="ZZSECRET", instrument="STO")
    other = _run(tmp_path, symbol="ACME")
    out = _call("option_implied_move", run_id=other, symbol="ZZSECRET", trade_date="2026-03-02")
    assert out["known"] is False and "not available to this run" in out["reason"]
    assert "track" not in out["reason"] and "hold" not in out["reason"] and "watch" not in out["reason"]
    own = _run(tmp_path / "own", symbol="ZZSECRET")
    assert (
        _call("option_implied_move", run_id=own, symbol="ZZSECRET", trade_date="2026-03-02")["known"] is True
    )


def test_a_run_id_in_the_tool_args_cannot_widen_the_scope(env, tmp_path):
    """The scope comes only from the trusted run (the server's binding, or the claim's own run in save_claim): an
    agent naming another run whose stock has stored options is refused."""
    _load(symbol="ZZSECRET", instrument="STO")
    mine = _run(tmp_path, symbol="ACME")
    theirs = _run(tmp_path / "theirs", symbol="ZZSECRET")
    with pytest.raises(ValueError, match="run_id"):
        _call_raw(
            "option_implied_move", mine, {"run_id": theirs, "symbol": "ZZSECRET", "trade_date": "2026-03-02"}
        )


def _call_raw(tool: str, run_id: int | None, args: dict):
    from finresearch.db import session_scope
    from finresearch.mcp_server import option_facts

    with session_scope() as s:
        return option_facts.run(s, tool, args, run_id=run_id)


def test_server_tools_take_the_run_from_their_binding_not_from_the_agent(env, tmp_path, monkeypatch):
    import asyncio

    from finresearch.mcp_server import option_facts, server

    _load(symbol="ZZSECRET", instrument="STO")
    mine = _run(tmp_path, symbol="ACME")
    _run(tmp_path / "theirs", symbol="ZZSECRET")
    registered = {t.name: t for t in asyncio.run(server.server.list_tools())}
    for name in option_facts.TOOLS:
        assert "run_id" not in registered[name].input_schema["properties"], name
        assert "citation" in registered[name].description and "unknown" in registered[name].description
    monkeypatch.setenv(option_facts.RUN_ENV, str(mine))
    out = json.loads(server.option_implied_move("ZZSECRET", "2026-03-02"))
    assert out["known"] is False and "not available to this run" in out["reason"]
    monkeypatch.delenv(option_facts.RUN_ENV)
    out = json.loads(server.option_implied_move("NIFTY", "2026-03-02"))  # unbound: indices only
    assert out["known"] is False and "no traded NIFTY options" in out["reason"]


def test_each_agent_task_binds_its_own_run_into_the_mcp_server(env, tmp_path):
    from finresearch.agents.runner import RunContext, build_task
    from finresearch.mcp_server.option_facts import RUN_ENV

    run_id = _run(tmp_path, symbol="ACME")
    ctx = RunContext(run_id=run_id, company_slug="of-x", company_name="Synthetic Options Co", nse_symbol="ACME",
                     facts={})  # fmt: skip
    task = build_task("stock_technical", ctx, plan="(plan)", claims="(claims)")
    cfg = json.loads(task.mcp_config.read_text())
    assert cfg["mcpServers"]["finresearch"]["env"][RUN_ENV] == str(run_id)
    other = build_task("stock_technical", RunContext(run_id=run_id + 1, company_slug="of-y", company_name="Other",
                                                     nse_symbol="ZZ", facts={}), plan="(plan)", claims="(c)")  # fmt: skip
    assert other.mcp_config != task.mcp_config  # one config per run: a later run never rebinds an earlier one
    assert json.loads(task.mcp_config.read_text())["mcpServers"]["finresearch"]["env"][RUN_ENV] == str(run_id)


def test_index_list_matches_the_stored_index_options():
    from finresearch.fnohist.store import INDEX_OPTIONS
    from finresearch.mcp_server.option_facts import INDEXES

    assert tuple(INDEXES) == tuple(INDEX_OPTIONS)


# --------------------------------------------------------------------------- IV30 percentile
def _iv_series(values: list[float | None], symbol: str = "NIFTY") -> list[date]:
    """One loaded session per value (weekdays ending DAY) with fno_iv_eod.iv_30d = value (no row for None)."""
    from finresearch.db import session_scope
    from finresearch.db.models import FnoBhavDay, FnoIvEod

    days, d = [], DAY
    while len(days) < len(values):
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    days.reverse()
    with session_scope() as s:
        for d, v in zip(days, values, strict=True):
            s.merge(FnoBhavDay(day=d, status="loaded", format="udiff", url=URL))
            if v is not None:
                s.add(
                    FnoIvEod(
                        symbol=symbol, day=d, iv_30d=D(str(v)), iv_30d_method="interpolated", rate=D("0.065")
                    )
                )
    return days


def test_iv30_percentile_over_252_sessions_with_n_and_source(env, tmp_path):
    vals = [10 + (i * 37 % 251) / 10 for i in range(251)] + [20.0]
    days = _iv_series(vals)
    out = _call("option_iv30_percentile", run_id=_run(tmp_path), symbol="NIFTY", trade_date="2026-03-02")
    assert out["known"] is True, out
    below = sum(1 for v in vals[:-1] if v < 20.0)
    assert float(out["iv30_percentile"]) == pytest.approx(below / 251 * 100, abs=0.005)
    assert out["iv30_pct"] == "20.000" or float(out["iv30_pct"]) == 20.0
    p = out["provenance"]
    assert p["n"] == 252 and p["sessions"] == 252 and p["window_first"] == days[0].isoformat()
    assert p["window_last"] == "2026-03-02" and p["dataset"].startswith("fno_iv_eod") and p["method_version"]
    assert out["citation"]["option_fact"] == {"tool": "option_iv30_percentile",
                                              "args": {"symbol": "NIFTY", "trade_date": "2026-03-02"}}  # fmt: skip


def test_iv30_percentile_below_coverage_is_unknown_without_a_number(env, tmp_path):
    _iv_series([None] * 102 + [15.0] * 150)  # 150 of 252 sessions carry an IV: below 80 %
    out = _call("option_iv30_percentile", run_id=_run(tmp_path), symbol="NIFTY", trade_date="2026-03-02")
    assert out["known"] is False and "coverage" in out["reason"] and "iv30_percentile" not in out


# --------------------------------------------------------------------------- 25-delta skew
def test_skew_25d_matches_the_stored_series_and_names_its_strikes(env, tmp_path):
    from finresearch.db import session_scope
    from finresearch.db.models import FnoIvEod
    from finresearch.fnohist.derive import derive_day

    _load(skew=0.10)
    with session_scope() as s:
        derive_day(s, DAY)
    with session_scope() as s:
        stored = s.scalar(select_skew(FnoIvEod))
    out = _call("option_skew_25d", run_id=_run(tmp_path), symbol="NIFTY", trade_date="2026-03-02")
    assert out["known"] is True, out
    # independent: the 25-delta strikes sit about 0.674 sigma sqrt(T) (~2.8 %) either side of F, so a smile of 0.10 vol
    # per unit of (F - K) / F puts the put about 0.28 points over and the call 0.28 under the ATM level
    assert float(out["skew_25d"]) == pytest.approx(0.56, abs=0.05)
    assert float(out["skew_25d"]) == pytest.approx(float(stored), abs=0.0006)
    p = out["provenance"]
    assert p["expiry"] == "2026-03-30" and p["stored_skew_25d"] == str(stored)
    (c0, c1), (p0, p1) = p["call_25d_strikes"], p["put_25d_strikes"]
    assert FWD < c0 < c1 and p0 < p1 < FWD
    assert sorted(p["strikes_used"]) == sorted({c0, c1, p0, p1})


def select_skew(model):
    from sqlalchemy import select

    return select(model.skew_25d).where(model.symbol == "NIFTY", model.day == DAY)


# --------------------------------------------------------------------------- citing a fact: save_claim re-runs it
def _save(run_id: int, value: str, cite: dict, metric: str = "nifty_implied_move", unit: str = "%"):
    from finresearch.db import session_scope
    from finresearch.mcp_server.claims import save_claim
    from finresearch.mcp_server.server import run_fincalc, run_option_fact

    with session_scope() as s:
        return save_claim(s, run_id=run_id, stream="stock_technical", statement="NIFTY implied move to 30-Mar",
                          claim_type="numeric", metric=metric, value=value, unit=unit, period="2026-03-02",
                          citations=[cite], fincalc=run_fincalc, option_fact=run_option_fact)  # fmt: skip


def _grade(claim_id: int):
    from finresearch.db import session_scope
    from finresearch.db.models import Claim
    from finresearch.verify.evidence import claim_grade

    with session_scope() as s:
        c = s.get(Claim, claim_id)
        return claim_grade(c), c.status, c.citations[0].computation


def test_a_claim_citing_the_tool_reproduces_its_value_and_is_grade_d(env, tmp_path):
    _load()
    run_id = _run(tmp_path)
    out = _call("option_implied_move", run_id=run_id, symbol="NIFTY", trade_date="2026-03-02")
    cite = {**out["citation"], "result_key": "move_1sd_pct"}
    res = _save(run_id, str(round(float(out["move_1sd_pct"]), 2)), cite)  # rounded as a report states it
    g, status, comp = _grade(res["claim_id"])
    assert g["grade"] == "D" and "stored market data" in g["label"] and status != "unsupported"
    assert comp["kind"] == "option_fact" and comp["matches"] is True and comp["inputs"] == []
    assert (
        comp["result"] == out["move_1sd_pct"]
        and comp["method_version"] == out["provenance"]["method_version"]
    )
    assert comp["provenance"]["expiry"] == "2026-03-30"


def test_a_claim_that_misstates_the_value_is_unsupported(env, tmp_path):
    _load()
    run_id = _run(tmp_path)
    cite = {**_call("option_implied_move", run_id=run_id, symbol="NIFTY", trade_date="2026-03-02")["citation"],
            "result_key": "move_1sd_pct"}  # fmt: skip
    res = _save(run_id, "5.20", cite)
    g, status, comp = _grade(res["claim_id"])
    assert g["grade"] == "U" and status == "unsupported" and comp["matches"] is False


def test_an_unknown_or_out_of_scope_fact_cannot_be_cited(env, tmp_path):
    _load()
    _load(symbol="ZZSECRET", instrument="STO")
    run_id = _run(tmp_path, symbol="ACME")
    cite = {"option_fact": {"tool": "option_implied_move", "args": {"symbol": "ZZSECRET", "trade_date": "2026-03-02"}},
            "result_key": "move_1sd_pct"}  # fmt: skip
    with pytest.raises(ValueError, match="not available to this run"):
        _save(run_id, "4.15", cite)
    cite["option_fact"]["args"] = {"symbol": "NIFTY", "trade_date": "2026-03-02", "run_id": run_id}
    with pytest.raises(ValueError, match="run_id"):
        _save(run_id, "4.15", cite)
    with pytest.raises(ValueError, match="result_key"):
        _save(run_id, "4.15", {"option_fact": {"tool": "option_skew_25d", "args": {"symbol": "NIFTY"}}})


def test_regrade_rechecks_a_cited_fact_and_a_changed_stored_series_fails_it(env, tmp_path):
    from sqlalchemy import update

    from finresearch.db import session_scope
    from finresearch.db.models import FnoBhav
    from finresearch.verify.regrade import regrade

    _load()
    run_id = _run(tmp_path)
    out = _call("option_implied_move", run_id=run_id, symbol="NIFTY", trade_date="2026-03-02")
    res = _save(run_id, out["move_1sd_pct"], {**out["citation"], "result_key": "move_1sd_pct"})
    with session_scope() as s:
        mine = [c for c in regrade(s, all_d=True).citations if c.claim_id == res["claim_id"]]
        assert [c.grade for c in mine] == ["D"] and not mine[0].changed
        s.rollback()
    with (
        session_scope() as s
    ):  # the stored chain was re-loaded with different prices: the fact no longer holds
        s.execute(update(FnoBhav).where(FnoBhav.instrument == "IDO").values(settle=FnoBhav.settle * D("1.2"),
                                                                            close=FnoBhav.close * D("1.2")))  # fmt: skip
    with session_scope() as s:
        mine = [c for c in regrade(s, all_d=True).citations if c.claim_id == res["claim_id"]]
        assert [c.grade for c in mine] == ["U"] and mine[0].changed
        s.rollback()


# --------------------------------------------------------------------------- least privilege and privacy
def test_only_the_stock_technical_stream_may_call_the_option_tools():
    from finresearch.agents.roles import MCP, ROLES
    from finresearch.mcp_server.option_facts import TOOLS

    holders = {name for name, r in ROLES.items() if any(f"{MCP}{t}" in r.tools for t in TOOLS)}
    assert holders == {"stock_technical"}
    assert all(f"{MCP}{t}" in ROLES["stock_technical"].tools for t in TOOLS)
    assert f"{MCP}save_claim" in ROLES["stock_technical"].tools  # it can cite what it reads
    assert "option_implied_move" in ROLES["stock_technical"].template_text()


def test_the_option_fact_module_imports_market_data_only():
    """Static guard beyond test_privacy_llm's: nothing outside the market-data models, fincalc and the server config
    (fnohist.store and disclosures would reach the tracked-stock list)."""
    src = Path(__file__).parents[1] / "src" / "finresearch" / "mcp_server" / "option_facts.py"
    allowed = ("finresearch.db.models", "finresearch.fincalc", "finresearch.mcp_server.config", "sqlalchemy")
    std = {"__future__", "collections", "datetime", "decimal", "itertools", "math", "os", "typing"}
    bad = []
    for node in ast.walk(ast.parse(src.read_text())):
        mods = ([a.name for a in node.names] if isinstance(node, ast.Import)
                else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])  # fmt: skip
        bad += [m for m in mods if m.split(".")[0] not in std and not m.startswith(allowed)]
        if isinstance(node, ast.ImportFrom) and node.module == "finresearch.db.models":
            bad += [a.name for a in node.names if a.name not in
                    {"Company", "FnoBhav", "FnoBhavDay", "FnoIvEod", "ResearchRun"}]  # fmt: skip
    assert not bad, bad


def test_the_blind_verifier_sees_where_an_option_fact_points_but_not_its_value(env, tmp_path):
    from finresearch.bridge.limits import LimitTracker
    from finresearch.orchestrator.ipo import IpoPipeline

    _load()
    run_id = _run(tmp_path)
    out = _call("option_implied_move", run_id=run_id, symbol="NIFTY", trade_date="2026-03-02")
    res = _save(run_id, out["move_1sd_pct"], {**out["citation"], "result_key": "move_1sd_pct"})
    pipe = IpoPipeline(run_id, runner=lambda *a, **k: None, tracker=LimitTracker(tmp_path / "lim"))
    (item,) = json.loads(pipe._blind_claims_text([res["claim_id"]]))
    assert item["citations"] == [{"option_fact": "option_implied_move", "result_key": "move_1sd_pct",
                                  "args": {"symbol": "NIFTY", "trade_date": "2026-03-02", "expiry": "2026-03-30"}}]  # fmt: skip
    assert out["move_1sd_pct"] not in json.dumps(item)


def test_save_claim_through_the_server_scopes_by_its_bound_run_not_the_run_id_argument(
    env, tmp_path, monkeypatch
):
    """An agent bound to run A cannot name run B (whose stock has stored options) in save_claim to read B's facts
    through the re-check's verdict."""
    from sqlalchemy import func, select

    from finresearch.db import session_scope
    from finresearch.db.models import Claim
    from finresearch.mcp_server import option_facts, server

    _load(symbol="ZZSECRET", instrument="STO")
    mine = _run(tmp_path, symbol="ACME")
    theirs = _run(tmp_path / "theirs", symbol="ZZSECRET")
    monkeypatch.setenv(option_facts.RUN_ENV, str(mine))
    cite = {"option_fact": {"tool": "option_implied_move", "args": {"symbol": "ZZSECRET", "trade_date": "2026-03-02"}},
            "result_key": "move_1sd_pct"}  # fmt: skip
    out = json.loads(server.save_claim(theirs, "stock_technical", "ZZSECRET implied move", "numeric", [cite],
                                       metric="implied_move", value="5.20", unit="%", period="2026-03-02"))  # fmt: skip
    assert "not available to this run" in out["error"] and not any(
        ch.isdigit() for ch in out["error"].split(":")[-1]
    )
    with session_scope() as s:
        assert s.scalar(select(func.count()).select_from(Claim).where(Claim.run_id == theirs)) == 0

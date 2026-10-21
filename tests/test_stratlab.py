"""F&O Strategy Lab (#318): spec validation, the locked split, the entry filters, the variants counter and its deflated
Sharpe, background runs and the paper hand-off. Synthetic history only (tests/fno_synth.py); expected values are
worked out here from the stated rules and formulas, not by calling the code under test."""

from __future__ import annotations

import math
import threading
import time
from datetime import UTC, date, datetime, timedelta
from statistics import NormalDist
from zoneinfo import ZoneInfo

import fno_synth as fs
import pytest
from pydantic import ValidationError

from finresearch.backtest.history import Day, MemoryHistory, Quote
from finresearch.fincalc.volatility import IvStats
from finresearch.stratlab import filters as flt
from finresearch.stratlab import split as sp
from finresearch.stratlab.spec import StrategySpec, spec_hash, to_lab_spec

IST = ZoneInfo("Asia/Kolkata")
FIRST, LAST = date(2023, 9, 1), date(2025, 8, 29)
BASE = dict(
    underlying="NIFTY", structure="put_credit", delta=0.2, profit_take_pct=50.0, stop_mult=2.0, exit_dte=7
)


def spec(**kw) -> StrategySpec:
    return StrategySpec(**{**BASE, **kw})


def at_ist(d: date, h: int, m: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, h, m, tzinfo=IST).astimezone(UTC)


@pytest.fixture(scope="module")
def mem() -> MemoryHistory:
    """Two years of a made-up index: weekly Thursday expiries, a monthly future, strikes every 100 within ±10 %."""
    px = fs.path(FIRST, LAST, vol=0.15, seed=7)
    exps, monthly = fs.expiries(FIRST, LAST)
    return MemoryHistory([fs.day("NIFTY", d, s, exps, monthly, 0.15, 50, step=100.0, width=0.10)
                          for d, s in sorted(px.items())])  # fmt: skip


# --------------------------------------------------------------------------- 1. spec validation
@pytest.mark.parametrize(
    ("kw", "msg"),
    [
        (dict(dte_min=40, dte_max=30), "minimum days to expiry is above its maximum"),
        (dict(delta=None), "give delta"),
        (dict(strike_by="otm_pct", otm_pct=3.0), "give otm_pct (and no delta)"),
        (dict(delta=0.55), "short delta at most 0.5"),
        (dict(iv_min=60, iv_max=40), "minimum must be below its maximum"),
        (dict(profit_take_pct=100.0), "profit-take below 100"),
        (dict(structure="debit_call", stop_mult=1.5), "stop at most 1"),
        (dict(exit_dte=30), "below the entry window's minimum"),
        (dict(max_per_expiry=3, max_concurrent=2), "cannot exceed"),
        (dict(exit_before_expiry_day=False, underlying="EXMPLSTOCK"), "settle physically"),
        (dict(exit_before_results=True), "an index has no results date"),
        (dict(weekdays=[0, 0]), "distinct"),
        (dict(weekdays=[5]), "distinct numbers 0 (Mon) to 4 (Fri)"),
        (dict(width_pct=0), "greater than 0"),
        (dict(structure="short_strangle"), "Input should be"),
        (dict(naked=True), "Extra inputs are not permitted"),
    ],
)
def test_spec_validation_refuses_inconsistent_or_unbounded_specs(kw, msg):
    with pytest.raises(ValidationError, match=msg.replace("(", r"\(").replace(")", r"\)")):
        spec(**kw)


def test_spec_hash_is_the_spec_not_its_name_and_the_lab_spec_keeps_the_registration():
    from finresearch.backtest import prereg, specs

    a, b = spec(), spec(underlying="nifty")
    assert a.underlying == "NIFTY" and spec_hash(a) == spec_hash(b)
    assert spec_hash(spec(delta=0.15)) != spec_hash(a)
    lab = to_lab_spec(spec(strike_by="otm_pct", delta=None, otm_pct=3.0, weekdays=[1, 3]), "k", "n")
    assert (lab.otm_pct, lab.weekdays, lab.iv_pct_min, lab.hold_to_expiry) == (3.0, (1, 3), None, False)
    assert specs.registration_hash() == prereg.HASH  # the subclass leaves the pre-registered specs untouched


# --------------------------------------------------------------------------- 2. the split rule
def test_split_library_windows_when_the_history_covers_them():
    got = sp.lock(date(2022, 1, 3), date(2025, 9, 30))
    assert (got.is_start, got.is_end, got.oos_start) == (
        date(2022, 1, 3),
        date(2024, 6, 30),
        date(2024, 7, 1),
    )


def test_split_70_30_by_calendar_days_otherwise():
    # 2023-09-01 → 2025-08-29 is 728 days; 70 % = 509.6 → 510 days → 2025-01-24; OOS from the 1st of the next month
    got = sp.lock(FIRST, LAST)
    assert (got.is_start, got.is_end, got.oos_start) == (FIRST, date(2025, 1, 31), date(2025, 2, 1))
    with pytest.raises(sp.SplitError, match="at least 540"):
        sp.lock(date(2024, 6, 1), date(2025, 8, 29))
    with pytest.raises(sp.SplitError, match="no stored F&O history"):
        sp.lock(None, None)


# --------------------------------------------------------------------------- 3. filters
def _stats(pct: float | None, rank: float | None) -> IvStats:
    return IvStats(250, 15.0, rank, pct, 10.0, 20.0, "ok")


def test_filters_weekday_iv_range_and_unknowns_refuse():
    f = flt.Filters(to_lab_spec(spec(weekdays=[0, 2], iv_min=30, iv_max=70), "k", "n"), 0.065)
    tue, wed = Day(date(2025, 3, 4), "NIFTY"), Day(date(2025, 3, 5), "NIFTY")
    assert f.gate(None, tue, _stats(0.5, 0.5)) == "weekday filter (Tue)"
    assert f.gate(None, wed, _stats(0.5, 0.1)) is None  # percentile 50 is inside 30-70; rank is not read
    assert f.gate(None, wed, _stats(0.29, 0.5)) == "IV percentile below the filter"
    assert f.gate(None, wed, _stats(0.71, 0.5)) == "IV percentile above the filter"
    assert f.gate(None, wed, _stats(None, 0.5)) == "IV percentile unknown"
    assert f.gate(None, wed, None) == "IV percentile unknown"
    g = flt.Filters(to_lab_spec(spec(iv_measure="rank", iv_min=60), "k", "n"), 0.065)
    assert g.gate(None, wed, _stats(0.9, 0.5)) == "IV rank below the filter"


def test_trend_filter_on_the_discounted_future_and_unknown_days():
    f = flt.Filters(to_lab_spec(spec(trend="above_ma50"), "k", "n"), 0.065)
    d0 = date(2025, 1, 1)
    for i in range(49):
        f.closes.append((d0 + timedelta(days=i), 100.0 + i))  # 100..148
    x = Day(d0 + timedelta(days=49), "NIFTY")
    e = x.day + timedelta(days=20)
    x.futures[e] = 200.0
    f.on_day(x)
    price = 200.0 * math.exp(-0.065 * 20 / 365)  # the future discounted to today
    assert f.closes[-1][1] == pytest.approx(price)
    ma = (sum(100.0 + i for i in range(49)) + price) / 50
    assert f.moving_average(50) == pytest.approx(ma)
    assert f.gate(None, x, None) is None  # price > its average
    below = flt.Filters(to_lab_spec(spec(trend="below_ma50"), "k", "n"), 0.065)
    below.closes = list(f.closes)
    assert below.gate(None, x, None) == "trend filter: price not below its 50-session average"
    f.closes[10] = (f.closes[10][0], None)  # one day without a price inside the window
    assert f.gate(None, x, None) == "trend unknown (fewer than 50 sessions with a price)"
    short = flt.Filters(to_lab_spec(spec(trend="above_ma200"), "k", "n"), 0.065)
    short.closes = list(f.closes)
    assert short.gate(None, x, None) == "trend unknown (fewer than 200 sessions with a price)"


def test_strike_by_percent_out_of_the_money():
    from finresearch.backtest import pricing

    x = Day(date(2025, 3, 3), "NIFTY")
    e = date(2025, 3, 27)
    for k in (19000, 19300, 19400, 19500, 20000, 20600, 20700):
        for r in ("put", "call"):
            x.options[(e, r, float(k))] = Quote(e, r, float(k), 10.0, 10.0, 50 if k != 19400 else 5, 100)
    # forward 20000: 3 % OTM put → 19400 (but it traded only 5 contracts < 10) → nearest traded 19300 / 19500 tie →
    # the farther from the money (19300); 3 % OTM call → 20600
    assert pricing.by_otm(x, e, "put", 3.0, 20000.0, 10).strike == 19300.0
    assert pricing.by_otm(x, e, "call", 3.0, 20000.0, 10).strike == 20600.0
    assert pricing.by_otm(x, e, "call", 0.0, 20000.0, 10).strike == 20000.0


# --------------------------------------------------------------------------- 4. OOS lock, variants counter (database)
LAB_TABLES = ("lab_paper_signal, lab_paper_rule, lab_variant, lab_run, lab_version, lab_strategy, fno_backtest_result, "
              "fno_backtest_run, fno_exit_rule, paper_leg, paper_strategy")  # fmt: skip


@pytest.fixture
def lab_db(env, monkeypatch):
    from sqlalchemy import text

    from finresearch.adapters import groww_market
    from finresearch.db import session_scope
    from finresearch.stratlab import paper

    monkeypatch.setattr(groww_market, "_holidays", lambda: set())
    paper._LAST.clear()
    with session_scope() as s:
        s.execute(text(f"TRUNCATE {LAB_TABLES} CASCADE"))
    yield env
    with session_scope() as s:
        s.execute(text(f"TRUNCATE {LAB_TABLES} CASCADE"))


def save_library(srs: list[float]) -> None:
    """A synthetic 'latest library evaluation' whose strategies have these out-of-sample daily Sharpe ratios."""
    from finresearch.backtest import store as bstore
    from finresearch.db import session_scope

    rep = {"version": "t", "registration_hash": "h", "registered_hash": "h", "oos_evaluated": True, "source": "synthetic",
           "variants_tried": 8, "stock_rule_symbol": None, "windows": {"is": ["2022-01-01", "2024-06-30"]},
           "strategies": [{"key": f"s{i}", "name": f"S{i}", "role": "candidate", "status": "fails", "spec_hash": "x",
                           "underlying": "NIFTY", "windows": {"oos": {"daily_sr": v, "trades": 40}}, "trades": []}
                          for i, v in enumerate(srs)]}  # fmt: skip
    with session_scope() as s:
        bstore.save(s, rep)


def run_now(version_id: int, oos: bool, history) -> dict:
    from finresearch.db import session_scope
    from finresearch.db.models import LabRun
    from finresearch.stratlab import jobs

    with session_scope() as s:
        rid = jobs.submit(s, version_id, oos).id
    assert jobs.execute(rid, history) == "done"
    with session_scope() as s:
        r = s.get(LabRun, rid)
        return {
            "id": rid,
            "result": r.result,
            "trades": r.trades,
            "n_trials": r.n_trials,
            "counted": r.counted,
        }


def dsr_by_hand(w: dict, n: int, var: float) -> float:
    """Bailey & López de Prado (2014) written out: SR0 = √V((1−γ)Φ⁻¹(1−1/N) + γΦ⁻¹(1−1/(Ne))), then
    DSR = Φ((SR − SR0)√(T−1) / √(1 − γ3·SR + (γ4−1)/4·SR²))."""
    nd, g = NormalDist(), 0.5772156649015329
    sr0 = math.sqrt(var) * ((1 - g) * nd.inv_cdf(1 - 1 / n) + g * nd.inv_cdf(1 - 1 / (n * math.e)))
    sr, t = w["daily_sr"], w["days"]
    return nd.cdf(
        (sr - sr0) * math.sqrt(t - 1) / math.sqrt(1 - w["skew"] * sr + (w["kurtosis"] - 1) / 4 * sr * sr)
    )


def test_oos_lock_in_sample_runs_are_free_and_every_new_oos_variant_raises_the_bar(lab_db, mem):
    from finresearch.db import session_scope
    from finresearch.db.models import LabStrategy
    from finresearch.stratlab import evaluate as ev
    from finresearch.stratlab import store

    lib = [0.01, -0.02, 0.03, 0.0, -0.01, 0.02, -0.03, 0.015]
    save_library(lib)
    with session_scope() as s:
        st = store.create_strategy(s, "Bull put", spec(), mem)
        sid, v1 = st.id, st.versions[0].id
        assert (st.is_end, st.oos_start) == (date(2025, 1, 31), date(2025, 2, 1))
        # a changed version keeps the split locked at creation; the same spec again is the same version
        v2 = store.add_version(s, st, spec(delta=0.15), "short leg farther from the money").id
        assert store.add_version(s, st, spec(delta=0.15)).id == v2
        with pytest.raises(store.LabError, match="locked on NIFTY"):
            store.add_version(s, st, spec(underlying="BANKNIFTY"))

    ins = run_now(v1, False, mem)  # in sample: never reads a day after the split, not counted
    assert ins["result"]["status"] == "in_sample_only" and ins["counted"] is False
    assert ins["result"]["max_day_read"] <= "2025-01-31" and ins["n_trials"] == 8
    assert all(t["entry_day"] <= "2025-01-31" for t in ins["trades"])

    a = run_now(v1, True, mem)
    assert a["counted"] is True and a["n_trials"] == 9
    again = run_now(v1, True, mem)  # the same spec again: not a new variant
    assert again["counted"] is False and again["n_trials"] == 9
    b = run_now(v2, True, mem)
    assert b["counted"] is True and b["n_trials"] == 10
    w_a, w_b = a["result"]["windows"]["oos"], b["result"]["windows"]["oos"]
    assert w_a["from"] == w_b["from"] == "2025-02-01"
    # V: the library's eight OOS daily Sharpe ratios and the lab's counted variants (this one included)
    trials = [*lib, w_a["daily_sr"], w_b["daily_sr"]]
    v_b = ev.trials_variance(trials)
    m = sum(trials) / 10
    assert v_b == pytest.approx(sum((x - m) ** 2 for x in trials) / 9)
    assert w_b["deflated_sharpe"] == pytest.approx(dsr_by_hand(w_b, 10, v_b), abs=1e-4)
    # the bar rises with N: the same window and V judged as one of 9 vs one of 10 (and of 30) variants
    assert dsr_by_hand(w_b, 9, v_b) > dsr_by_hand(w_b, 10, v_b) > dsr_by_hand(w_b, 30, v_b)
    dsr_rule = next(c for c in b["result"]["criteria"] if c["criterion"] == "OOS deflated Sharpe")["rule"]
    assert "N = 10 variants: 8 pre-registered + 2 from the lab" in dsr_rule
    assert any("2 versions tested out of sample" in x for x in b["result"]["warnings"])
    assert any("10 variants have now been tried" in x for x in b["result"]["warnings"])
    with session_scope() as s:
        assert store.n_trials(s) == 10 and store.oos_versions(s, sid) == 2
        assert s.get(LabStrategy, sid).oos_start == date(2025, 2, 1)


def test_deflated_sharpe_unknown_without_two_trials_never_passes(lab_db, mem):
    from finresearch.db import session_scope
    from finresearch.stratlab import store

    with session_scope() as s:
        v1 = store.create_strategy(s, "", spec(), mem).versions[0].id
    got = run_now(v1, True, mem)  # no library evaluation saved: one trial only, V unknown
    w = got["result"]["windows"]["oos"]
    crit = next(c for c in got["result"]["criteria"] if c["criterion"] == "OOS deflated Sharpe")
    assert w["deflated_sharpe"] is None and crit["pass"] is False and got["result"]["status"] == "fails"
    assert any("could not be computed" in x for x in got["result"]["warnings"])


# --------------------------------------------------------------------------- 5. background runs and the API
@pytest.fixture
def client(lab_db, mem, monkeypatch):
    from fastapi.testclient import TestClient

    from finresearch.api import fno_lab
    from finresearch.api.app import create_app
    from finresearch.stratlab import jobs

    launched: list[int] = []
    monkeypatch.setattr(fno_lab, "HISTORY", lambda: mem)
    monkeypatch.setattr(jobs, "LAUNCHER", lambda rid: launched.append(rid) or 4242)
    c = TestClient(create_app())
    c.launched = launched
    return c


def test_api_runs_in_a_background_job_with_progress(client, mem, monkeypatch):
    from finresearch.stratlab import jobs

    monkeypatch.setattr(
        jobs, "HEARTBEAT_S", 0.0
    )  # write every session's progress so the poll below sees it move

    r = client.post("/api/fno/lab/strategies", json={"name": "Bull put", "spec": BASE})
    assert r.status_code == 201, r.text
    st = r.json()
    assert st["split"] == {"is": ["2023-09-01", "2025-01-31"], "oos_from": "2025-02-01",
                           "data": ["2023-09-01", "2025-08-29"], "rule": st["split"]["rule"], "oos_label": "already published, not out of sample"}  # fmt: skip
    vid = st["versions"][0]["id"]
    q = client.post(f"/api/fno/lab/versions/{vid}/runs", json={"oos": True})
    assert q.status_code == 202 and q.json()["status"] == "queued"  # nothing replayed in the request
    rid = q.json()["id"]
    assert client.launched == [rid]
    assert client.post(f"/api/fno/lab/versions/{vid}/runs", json={"oos": True}).status_code == 409
    seen: list[float] = []
    th = threading.Thread(target=jobs.execute, args=(rid, mem))
    th.start()
    while th.is_alive():
        got = client.get(f"/api/fno/lab/runs/{rid}").json()
        if got["status"] == "running":
            seen.append(got["progress"])
        time.sleep(0.05)
    th.join()
    assert seen and seen == sorted(seen) and min(seen) >= 0 and max(seen) < 1
    done = client.get(f"/api/fno/lab/runs/{rid}").json()
    assert done["status"] == "done" and done["progress"] == 1 and done["n_trials"] == 9
    res = done["result"]
    assert res["key"] == f"lab{st['id']}v1" and "trades" not in res and res["equity"] and res["by_year"]
    assert set(res["windows"]) == {"is", "oos", "oos_stress"} and res["warnings"]
    assert done["bar_now"]["n_trials"] == 9 and done["library"] is None
    trades = client.get(f"/api/fno/lab/runs/{rid}/trades").json()["trades"]
    assert len(trades) == done["n_trades"] > 0 and {"entry_day", "legs", "net", "reason"} <= set(trades[0])
    lab = client.get("/api/fno/lab").json()
    assert lab["variants"] == {"library": 8, "lab": 1, "n": 9}
    assert lab["strategies"][0]["versions"][0]["counted_variant"] is True


def test_api_refuses_bad_specs_and_unknown_underlyings(client):
    bad = client.post("/api/fno/lab/strategies", json={"spec": {**BASE, "dte_min": 50, "dte_max": 40}})
    assert bad.status_code == 422 and "above its maximum" in bad.text
    none = client.post("/api/fno/lab/strategies", json={"spec": {**BASE, "underlying": "BANKNIFTY"}})
    assert none.status_code == 422 and "no stored F&O history" in none.json()["detail"]


def test_run_cap_stops_a_run_and_a_silent_worker_is_marked_failed(lab_db, mem):
    from finresearch.db import session_scope
    from finresearch.db.models import LabRun
    from finresearch.stratlab import jobs, store

    with session_scope() as s:
        vid = store.create_strategy(s, "", spec(), mem).versions[0].id
        rid = jobs.submit(s, vid, False).id
    t = [datetime(2025, 9, 1, tzinfo=UTC)]

    def clock():  # every read of the clock is a minute later
        t[0] += timedelta(minutes=1)
        return t[0]

    assert jobs.execute(rid, mem, cap=timedelta(minutes=5), clock=clock) == "timed_out"
    with session_scope() as s:
        r = s.get(LabRun, rid)
        assert r.status == "timed_out" and "5-minute cap" in r.error and r.counted is False
        stale = jobs.submit(s, vid, True)
        stale.status, stale.heartbeat_at = "running", datetime.now(UTC) - timedelta(minutes=10)
        s.flush()
        assert jobs.recover_stale(s) == 1
        assert stale.status == "failed" and "stopped reporting" in stale.error
        st = s.get(LabRun, rid)
        strat = store.create_strategy(s, "", spec(), mem)
        for i in range(jobs.MAX_ACTIVE):  # other versions queued: the lab is busy
            jobs.submit(s, store.add_version(s, strat, spec(delta=0.1 + i / 100)).id, False)
        assert st.status == "timed_out"
        with pytest.raises(store.LabError, match="already queued or running"):
            jobs.submit(s, vid, False)


# --------------------------------------------------------------------------- 6. paper hand-off
class NoGroww:
    """No Groww session: prices come from the option chain (the paper book's fallback)."""

    def token(self):
        return None

    async def instruments(self):
        return None


class ChainFrom:
    """Option chains whose last prices are one synthetic day's closes (made-up prices)."""

    def __init__(self, x: Day) -> None:
        self.x = x

    async def chain(self, symbol, expiry):
        from decimal import Decimal as D

        from finresearch.adapters.nse_fno import ChainRow, OptionChain, OptionQuote

        def q(right, k):
            got = self.x.options.get((expiry, right, k))
            return None if got is None else OptionQuote(last_price=D(str(got.close)), iv=None, oi=None,
                                                         change_in_oi=None, volume=None, bid=None, ask=None)  # fmt: skip

        ks = sorted({k for e, _, k in self.x.options if e == expiry})
        return OptionChain(symbol=symbol, expiry=expiry, underlying=D(str(round(self.x.spot, 2))), as_of=None,
                           rows=[ChainRow(strike=D(str(k)), call=q("call", k), put=q("put", k)) for k in ks])  # fmt: skip


def test_paper_hand_off_signals_match_the_engine_then_fill_live_and_exit_by_rule(client, mem, monkeypatch):
    from finresearch.backtest import engine
    from finresearch.db import session_scope
    from finresearch.db.models import LabPaperSignal, PaperStrategy
    from finresearch.fincalc import paper as pp
    from finresearch.stratlab import jobs, paper, store
    from finresearch.stratlab.spec import book_for

    sp_ = spec(max_concurrent=5, max_per_expiry=5)
    with session_scope() as s:
        vid = store.create_strategy(s, "Paper me", sp_, mem).versions[0].id
    assert run_async(paper.step(at_ist(LAST, 20))) == {}  # nothing to do: no rule set
    assert client.post(f"/api/fno/lab/versions/{vid}/paper").status_code == 422  # not tested yet
    with session_scope() as s:
        rid = jobs.submit(s, vid, False).id
    jobs.execute(rid, mem)
    made = client.post(f"/api/fno/lab/versions/{vid}/paper")
    assert made.status_code == 201 and made.json()["status"] == "active"
    assert client.post(f"/api/fno/lab/versions/{vid}/paper").status_code == 409
    assert client.post(f"/api/fno/lab/paper/{made.json()['id']}/stop").json()["status"] == "stopped"

    # a rule set made on the evening of 8-Aug: closes from 11-Aug on are checked
    monkeypatch.setattr(paper, "FILL_WITHIN_DAYS", 100)
    with session_scope() as s:
        rule = paper.create_rule(s, vid, at_ist(date(2025, 8, 8), 20)).id
    got = paper.signals_step(at_ist(LAST, 20), mem)
    lab, book = to_lab_spec(sp_, "e", "e"), book_for(sp_)
    f = flt.Filters(lab, book.rate)
    want = engine.run([lab], mem, date(2025, 8, 9), LAST, book, data_end=LAST, gate=f.gate, on_day=f.on_day)[
        "e"
    ]
    entries = sorted([t["entry_day"] for t in want["trades"]] + [o["entry_day"] for o in want["open_at_end"]])
    # fewer entries than max_concurrent, so the engine closing some early does not change later decisions
    assert 0 < len(entries) < 5
    by_day = {t["entry_day"]: t for t in want["trades"]}
    with session_scope() as s:
        sigs = list(s.scalars(paper.select(LabPaperSignal).where(LabPaperSignal.rule_id == rule)
                              .order_by(LabPaperSignal.day)))  # fmt: skip
        assert [g.day.isoformat() for g in sigs] == entries and {g.status for g in sigs} == {"pending"}
        assert got == {"lab_signals": len(entries), "lab_closes_checked": 15}
        assert all(g.legs[0]["side"] == "sell" and g.legs[1]["side"] == "buy" for g in sigs)
        for g in sigs:  # the same legs and lots as the backtest's entry that close
            t = by_day.get(g.day.isoformat())
            if t is not None:
                assert g.lots == t["lots"] and [x["strike"] for x in g.legs] == [
                    x["strike"] for x in t["legs"]
                ]
                assert [x["fill_assumed"] for x in g.legs] == [x["entry"] for x in t["legs"]]
        first = sigs[0]
        ids, legs0, lots0 = [g.id for g in sigs], first.legs, first.lots
    assert paper.signals_step(at_ist(LAST, 21), mem) == {
        "lab_signals": 0,
        "lab_closes_checked": 0,
    }  # idempotent

    # Monday 1-Sep, 10:00 IST: filled at the chain's last price ∓ the paper book's 0.5 % slippage
    src, mon = ChainFrom(mem.day("NIFTY", LAST)), date(2025, 9, 1)
    assert run_async(paper.fill_step(at_ist(mon, 10), market=NoGroww(), sources=src)) == {
        "lab_filled": len(ids),
        "lab_missed": 0,
    }
    with session_scope() as s:
        g = s.get(LabPaperSignal, ids[0])
        st = s.get(PaperStrategy, g.paper_id)
        assert g.status == "filled" and st.label.startswith("Lab #") and st.status == "open"
        for leg, want_leg in zip(st.legs, legs0, strict=True):
            close = mem.day("NIFTY", LAST).options[(st.expiry, want_leg["right"], want_leg["strike"])].close
            f_ = close * (1.005 if want_leg["side"] == "buy" else 0.995)
            assert float(leg.entry_price) == pytest.approx(round(f_, 2), abs=0.006)
            assert leg.lots == lots0
        credit = abs(pp.net_premium(pp_legs(st)))
        st.mtm_gross = round(credit * 0.6, 2)  # marked at 60 % of the credit: the 50 % profit-take fires
        pid0 = st.id
    out = run_async(paper.manage_step(at_ist(mon, 11), market=NoGroww(), sources=src))
    assert out == {"lab_closed": 1}  # the others have no mark yet: an unknown mark never fires a P&L rule
    with session_scope() as s:
        st = s.get(PaperStrategy, pid0)
        g = s.get(LabPaperSignal, ids[0])
        assert st.status == "closed" and st.close_reason == "square_off" and g.exit_reason == "profit_take"
    view = client.get("/api/fno/lab/paper").json()["rules"]
    mine = next(r for r in view if r["id"] == rule)
    assert mine["results"]["closed"] == 1 and mine["signals"][0]["exit_reason"] == "profit_take"


def run_async(coro):
    import asyncio

    return asyncio.run(coro)


def pp_legs(st):
    from finresearch.fnobook import book

    return book.legs_of(st)


def test_paper_signals_checked_too_late_are_kept_as_missed(lab_db, mem):
    from finresearch.db import session_scope
    from finresearch.db.models import LabPaperSignal
    from finresearch.stratlab import jobs, paper, store

    with session_scope() as s:
        vid = store.create_strategy(s, "", spec(), mem).versions[0].id
        rid = jobs.submit(s, vid, False).id
    jobs.execute(rid, mem)
    with session_scope() as s:
        rule = paper.create_rule(s, vid, at_ist(date(2025, 8, 8), 20)).id
    # the app was off: the closes of 11-29 Aug are checked on 10-Sep, more than FILL_WITHIN_DAYS later
    got = paper.signals_step(at_ist(date(2025, 9, 10), 20), mem)
    assert got["lab_signals"] > 0
    with session_scope() as s:
        sigs = list(s.scalars(paper.select(LabPaperSignal).where(LabPaperSignal.rule_id == rule)))
        assert {g.status for g in sigs} == {"missed"} and all("checked late" in g.note for g in sigs)
    assert run_async(paper.fill_step(at_ist(date(2025, 9, 11), 10), market=NoGroww(), sources=None)) == {
        "lab_filled": 0,
        "lab_missed": 0,
    }

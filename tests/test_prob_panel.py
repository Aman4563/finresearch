"""The fixed probability panel (#376 (b), evals/experiments/card_ledger/ADDENDUM.md B1.3) on synthetic F&O history
(tests/fno_synth.py: a made-up index, Black-76 at one flat volatility). Expected values are computed by hand or with
Black-76 prices, independently of the module's own formulas."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from statistics import NormalDist

import pytest
from fno_synth import history as synth
from sqlalchemy import select, text

from finresearch.backtest.history import Day, MemoryHistory, Quote
from finresearch.backtest.specs import SPECS
from finresearch.fincalc import black76 as b76
from finresearch.signals import prob_panel as pp

N = NormalDist()
PCS = next(s for s in SPECS if s.key == "nifty_pcs_m")
START = date(2022, 3, 28)  # the synthetic panel's first session: 28..31 Mar 2022 (31 Mar is a monthly expiry)
NOW = datetime(2022, 4, 1, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(("right", "delta"), [("call", 0.10), ("call", 0.25), ("put", 0.16), ("put", 0.25)])
def test_delta_strike_has_the_forward_delta_asked_for(right, delta):
    f, sigma, t = 20000.0, 0.15, 30 / 365
    k = pp.delta_strike(right, f, sigma * math.sqrt(t), delta)
    got = b76.forward_delta(right, f, k, t, 1.0, sigma)  # undiscounted (DF = 1)
    assert abs(got) == pytest.approx(delta, abs=1e-12)
    assert (k > f) == (right == "call")


def test_p_above_is_minus_the_strike_slope_of_the_undiscounted_call():
    f, k, sigma, t, h = 20000.0, 20500.0, 0.18, 21 / 365, 0.01
    slope = (b76.price("call", f, k + h, t, 1.0, sigma) - b76.price("call", f, k - h, t, 1.0, sigma)) / (
        2 * h
    )
    assert pp.p_above(f, k, sigma, t) == pytest.approx(-slope, abs=1e-7)
    assert pp.p_above(f, k, None, t) is None and pp.p_above(f, k, 0.0, t) is None  # unknown, never 0.5


def test_the_band_and_put_events_under_the_atm_iv_by_hand():
    f, sigma, t = 20000.0, 0.15, 28 / 365
    s = sigma * math.sqrt(t)
    inp = {"forward": f, "t": t, "band": [f * math.exp(-s), f * math.exp(s)]}
    # d2 at F e^-s is 1 - s/2 and at F e^s is -1 - s/2
    assert pp.event_probability("band1s", inp, sigma) == pytest.approx(N.cdf(1 - s / 2) - N.cdf(-1 - s / 2))
    put = {"forward": f, "t": t, "strike": pp.delta_strike("put", f, s, 0.25)}
    # the 25-delta put has d1 = N^-1(0.75), so P(S_T < K) = N(-d2) = N(s - N^-1(0.75))
    assert pp.event_probability("put25", put, sigma) == pytest.approx(N.cdf(s - N.inv_cdf(0.75)))
    assert pp.happened("put25", put, put["strike"] - 1) == 1 and pp.happened("put25", put, put["strike"]) == 0
    assert pp.happened("band1s", inp, f) == 1 and pp.happened("band1s", inp, inp["band"][1] + 1) == 0


def test_near_and_monthly_expiries():
    x = Day(date(2022, 3, 3), "NIFTY", futures={date(2022, 3, 31): 1.0, date(2022, 4, 28): 1.0})
    for e in (date(2022, 3, 3), date(2022, 3, 10), date(2022, 3, 31)):
        x.options[(e, "call", 100.0)] = Quote(e, "call", 100.0, 1.0, 1.0, 1, 1)
    assert pp.expiries(x) == {date(2022, 3, 10): "near", date(2022, 3, 31): "monthly"}
    del x.options[(date(2022, 3, 10), "call", 100.0)]
    assert pp.expiries(x) == {date(2022, 3, 31): "near, monthly"}


def test_an_unknown_atm_iv_voids_the_strike_events_with_the_reason():
    e = date(2022, 3, 31)
    x = Day(date(2022, 3, 24), "NIFTY", futures={e: 20000.0}, spot=19990.0)
    x.options[(e, "call", 20000.0)] = Quote(e, "call", 20000.0, None, None, 0, 0)  # no price: no IV
    rows = pp.tail_rows(x, [0.01] * 70, 0.065)
    assert len(rows) == 7 and {r["status"] for r in rows} == {"void"}
    assert all("no ATM IV" in r["note"] and set(r["probabilities"].values()) == {None} for r in rows)


# --------------------------------------------------------------------------- recording and resolution (database)
@pytest.fixture(scope="module")
def hist() -> MemoryHistory:
    return synth(date(2021, 11, 1), date(2022, 3, 31), vol=0.15, seed=5)


@pytest.fixture
def clean(env):
    from finresearch.db import session_scope

    with session_scope() as s:
        s.execute(text("TRUNCATE prob_panel"))
    yield


def _rows():
    from finresearch.db import session_scope
    from finresearch.db.models import ProbPanel

    with session_scope() as s:
        return [{c.name: getattr(r, c.name) for c in ProbPanel.__table__.columns}
                for r in s.scalars(select(ProbPanel).order_by(ProbPanel.id))]  # fmt: skip


def _record(h, **kw) -> int:
    from finresearch.db import session_scope

    with session_scope() as s:
        return pp.record_pending(s, h, start=START, underlyings=("NIFTY",), spec_list=kw.get("specs", [PCS]))


def test_every_session_is_logged_once_and_recipes_match_the_replay(clean, hist):
    from finresearch.horserace import replay

    n = _record(hist)
    assert n > 0 and _record(hist) == 0  # idempotent
    rows = _rows()
    sessions = sorted({r["session"] for r in rows})
    assert sessions == [date(2022, 3, 28), date(2022, 3, 29), date(2022, 3, 30), date(2022, 3, 31)]
    for d in sessions[:3]:  # 31 Mar is the near and the monthly expiry: one set of 7 rows
        tail = [r for r in rows if r["session"] == d and not r["event"].startswith("spec:")]
        assert [(r["expiry"], r["expiry_kind"]) for r in tail] == [(date(2022, 3, 31), "near, monthly")] * 7
    last = [r for r in rows if r["session"] == date(2022, 3, 31) and r["event"] == "put25"]
    assert [(r["expiry"], r["expiry_kind"]) for r in last] == [
        (date(2022, 4, 7), "near"),
        (date(2022, 4, 28), "monthly"),
    ]
    assert all(r["status"] == "open" and None not in r["probabilities"].values()
               for r in rows if not r["event"].startswith("spec:"))  # fmt: skip
    cards, _ = replay.make_cards(hist, START, date(2022, 3, 31), [PCS])
    recipes = {(r["session"], r["expiry"]): r for r in rows if r["event"] == "spec:nifty_pcs_m"}
    assert len(recipes) == len(cards) > 0
    for c in cards:
        r = recipes[(c.origin, c.expiry)]
        assert r["expiry_kind"] == "recipe"
        assert r["probabilities"] == {m: c.forecasts[m].p for m in pp.RECIPE_MODELS}
        assert {k: v for k, v in r["inputs"].items() if k != "gate_pass"} == c.inputs


def test_tail_events_resolve_at_the_expiry_settlement_and_later_ones_wait(clean, hist):
    from finresearch.db import session_scope

    _record(hist, specs=[])
    with session_scope() as s:
        got = pp.resolve_due(s, hist, NOW)
    s_t = hist.day(
        "NIFTY", date(2022, 3, 31)
    ).spot  # expiry-day option settles are intrinsic: the future/underlying
    rows = _rows()
    done = [r for r in rows if r["expiry"] == date(2022, 3, 31)]
    assert (
        got == {"resolved": 21, "void": 0, "open": 0} and len(done) == 21
    )  # later expiries are not due: untried
    for r in done:
        assert r["status"] == "resolved" and r["value"] == pytest.approx(s_t)
        k = r["inputs"].get("strike")
        want = (s_t < k if r["event"].startswith("put") else s_t > k) if k else (
            r["inputs"]["band"][0] <= s_t <= r["inputs"]["band"][1])  # fmt: skip
        assert r["outcome"] == int(want)
    assert {r["status"] for r in rows if r["expiry"] > date(2022, 3, 31)} == {"open"}


def test_an_unknown_settlement_voids_never_zero(clean, hist):
    from finresearch.db import session_scope

    _record(hist, specs=[])
    gone = MemoryHistory([x for d, x in hist.days["NIFTY"].items() if d != date(2022, 3, 30)]
                         + [Day(date(2022, 4, 1), "NIFTY")])  # fmt: skip
    del gone.days["NIFTY"][
        date(2022, 3, 31)
    ]  # expiry day missing: 29 Mar's settles differ, so it is not the last
    with session_scope() as s:
        got = pp.resolve_due(s, gone, NOW)
    assert got["void"] == 21 and got["resolved"] == 0
    assert all(r["outcome"] is None and r["value"] is None and r["note"].startswith("void:")
               for r in _rows() if r["expiry"] == date(2022, 3, 31))  # fmt: skip


@dataclass
class R:
    id: int
    session: date
    underlying: str
    expiry: date
    expiry_kind: str
    event: str
    status: str = "resolved"
    outcome: int | None = 1
    value: float | None = None
    probabilities: dict = field(default_factory=dict)
    inputs: dict = field(default_factory=dict)


def test_independence_keeps_the_first_logged_row_per_expiry_even_when_void():
    e1, e2 = date(2022, 3, 31), date(2022, 4, 28)
    rows = [R(1, date(2022, 3, 1), "NIFTY", e1, "monthly", "put25"),
            R(2, date(2022, 3, 2), "NIFTY", e1, "monthly", "put25"),
            R(3, date(2022, 3, 25), "NIFTY", e1, "near, monthly", "put25"),
            R(4, date(2022, 3, 31), "NIFTY", e2, "monthly", "put25", status="void", outcome=None),
            R(5, date(2022, 4, 1), "NIFTY", e2, "monthly", "put25")]  # fmt: skip
    got = pp.first_events(rows)
    assert [r.id for r in got[("NIFTY", "monthly", "put25")]] == [
        1,
        4,
    ]  # 4 is void: counted, never replaced by 5
    assert [r.id for r in got[("NIFTY", "near", "put25")]] == [3]


def test_score_by_hand_and_no_claim_below_100(clean):
    from finresearch.db import session_scope
    from finresearch.db.models import ProbPanel

    f, sig, t = 100.0, 0.2, 0.25
    with session_scope() as s:
        for i, (p, o, st) in enumerate([(0.2, 0, 90.0), (0.6, 1, 110.0), (None, 1, 100.0)]):
            s.add(ProbPanel(session=date(2022, 1, 3 + i), underlying="NIFTY", expiry=date(2022, 2, 3 + i * 7),
                            expiry_kind="monthly", event="put25", status="resolved", outcome=o, value=st,
                            inputs={"forward": f, "t": t, "sigma": {"rn_atm": sig, "rw20": None, "rw60": sig}},
                            probabilities={"rn_atm": p, "rw20": None, "rw60": p}))  # fmt: skip
        s.flush()
        rep = pp.score(s)
    g = next(x for x in rep["groups"] if x["model"] == "rn_atm")
    assert (g["n"], g["events"], g["unknown_p"], g["claim_allowed"]) == (2, 3, 1, False)
    assert g["brier"] == pytest.approx((0.2**2 + 0.4**2) / 2)
    assert g["log"] == pytest.approx(-(math.log(0.8) + math.log(0.6)) / 2)
    pit = next(x for x in rep["pit"] if x["model"] == "rn_atm")
    sd = sig * math.sqrt(t)
    us = [N.cdf((math.log(v / f) + sd * sd / 2) / sd) for v in (90.0, 110.0, 100.0)]
    want = [0] * 10
    for u in us:
        want[int(u * 10)] += 1
    assert pit["bins"] == want and pit["unknown"] == 0
    assert (
        next(x for x in rep["pit"] if x["model"] == "rw20")["unknown"] == 3
    )  # unknown sigma: counted, not binned
    assert next(x for x in rep["groups"] if x["model"] == "rw20")["n"] == 0


def test_a_holiday_expiry_settles_on_the_last_session_with_one_uniform_settle(clean, hist):
    from dataclasses import replace

    from finresearch.db import session_scope

    _record(hist, specs=[])
    e, last = date(2022, 3, 31), date(2022, 3, 30)
    x = hist.day("NIFTY", last)
    fsp = 17500.0  # a made-up final settlement price on every expiring option (and the future, as NSE's)
    moved = Day(last, "NIFTY", spot=fsp, iv30=x.iv30, futures={**x.futures, e: fsp},
                options={k: (replace(q, settle=fsp) if q.expiry == e else q) for k, q in x.options.items()})  # fmt: skip
    hol = MemoryHistory(
        [*(v for d, v in hist.days["NIFTY"].items() if d < last), moved, Day(date(2022, 4, 1), "NIFTY")]
    )
    with session_scope() as s:
        got = pp.resolve_due(s, hol, NOW)
    assert got["resolved"] == 21
    assert {(r["value"], r["note"]) for r in _rows() if r["expiry"] == e} == {
        (fsp, "settled at uniform option settle 17,500.00 on 2022-03-30")
    }


async def test_the_monitor_forecast_step_logs_and_resolves_the_panel(clean, hist, monkeypatch):
    from finresearch.fnobook import card_forecasts as cf
    from finresearch.horserace import replay
    from finresearch.monitor import scheduler

    monkeypatch.setattr(cf, "history", lambda: hist)
    monkeypatch.setattr(pp, "PANEL_START", START)
    monkeypatch.setattr(replay, "replay_specs", lambda: [PCS])
    monkeypatch.setattr(scheduler, "_FORECASTS_CHECKED", {})
    got = await scheduler.forecast_step(None, NOW)
    assert got["panel_logged"] == len(_rows()) > 0 and got["panel_resolved"] == 21


def test_the_cli_logs_nothing_without_stored_history(clean):
    from typer.testing import CliRunner

    from finresearch.cli import app

    r = CliRunner().invoke(app, ["forecasts", "panel"])
    assert r.exit_code == 0, r.output
    assert "0 panel row(s) logged; resolved 0, void 0, waiting 0" in r.output

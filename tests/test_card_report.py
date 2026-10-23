"""The card ledger's scoring report (#376, evals/experiments/card_ledger/ADDENDUM.md B1.1, B1.2) and its API.
Synthetic events and cards; expected values worked by hand in the comments."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import func, select, text

from finresearch.fnobook import card_report as cr
from finresearch.horserace.race import Event, Forecast

E1, E2, E3 = date(2026, 11, 3), date(2026, 11, 10), date(2026, 11, 17)
LINE = [float(i) for i in range(20)]  # dot i = i at u = (i + 0.5) / 20, so q(0.10) = 1.5 and q(0.5) = 9.5


def ev(pnl, *, rw=None, rn=None, first=True, gate=True, expiry=E1, origin=date(2026, 10, 27), und="NIFTY",
       dots=None, outcome=None) -> Event:  # fmt: skip
    return Event(underlying=und, expiry=expiry, structure="put_credit", origin=origin, resolved_on=expiry,
                 outcome=int(pnl > 0) if outcome is None else outcome, pnl=pnl, gate_pass=gate, first=first,
                 forecasts={"p_rw": Forecast(rw, None, dots), "p_rn_atm": Forecast(rn)})  # fmt: skip


def test_bss_pairs_the_first_events_where_both_models_are_known():
    xs = [ev(10, rw=0.8, rn=0.5), ev(-5, rw=0.3, rn=0.5, expiry=E2), ev(-1, rw=0.6, rn=0.5, expiry=E3),
          ev(10, rw=0.1, rn=0.9, first=False),  # a later card in a cluster: never scored
          ev(10, rw=0.9, rn=None, expiry=E2, und="BANKNIFTY")]  # no benchmark probability: unpaired  # fmt: skip
    got = cr.bss(xs)
    # Brier rw = (0.04 + 0.09 + 0.36) / 3, rn = 0.75 / 3: BSS = 1 - 0.49 / 0.75
    assert got["n"] == 3 and got["bss"] == pytest.approx(1 - 0.49 / 0.75)
    assert cr.bss([])["bss"] is None  # unknown, never 0


def test_cohort_delta_is_the_difference_of_means_clustered_by_expiry():
    xs = [ev(100, expiry=E1), ev(300, expiry=E2), ev(-50, gate=False, expiry=E1), ev(50, gate=False, expiry=E3),
          ev(999, gate=None), ev(999, first=False)]  # fmt: skip
    got = cr.cohort(xs)
    assert (got["mean_pass"], got["mean_fail"], got["delta"]) == (200, 0, pytest.approx(200))
    assert (got["n_pass"], got["n_fail"], got["unknown_class"], got["clusters"]) == (2, 2, 1, 3)
    assert got["se"] is not None and got["p"] is not None
    one = cr.cohort([ev(100), ev(-50, gate=False)])  # one expiry: the clustered variance is undefined
    assert one["delta"] is None and one["se"] is None and one["mean_pass"] == 100


def test_pit_and_p10_coverage_on_the_logged_dots():
    xs = [ev(1.0, rw=0.5, dots=LINE), ev(2.0, rw=0.5, dots=LINE), ev(9.5, rw=0.5, dots=LINE),
          ev(-100.0, rw=0.5, dots=LINE), ev(5.0, rw=0.5)]  # the last has no dots: counted, not scored  # fmt: skip
    m = cr.model_scores(xs, "p_rw")
    # u = 0.075, 0.125, 0.5 and clipped 0.001: bins 0, 1, 5, 0
    assert m["pit"] == {"n": 4, "bins": [2, 1, 0, 0, 0, 1, 0, 0, 0, 0], "clipped": 1, "no_dots": 1}
    # below q(0.10) = 1.5: 1.0 and -100; exact two-sided binomial(4, 0.1) at k = 2: 0.0486 + 0.0036 + 0.0001
    assert (m["p10"]["below"], m["p10"]["share"]) == (2, 0.5) and m["p10"]["p"] == pytest.approx(0.0523)
    assert m["n"] == 5 and m["brier"] == pytest.approx(0.25)
    assert cr.model_scores([], "p_rw")["p10"]["share"] is None


def test_claims_need_100_independent_events_per_underlying():
    xs = [ev(1, rw=0.5, expiry=date(2027, 1, 1 + i % 28), origin=date(2026, 10, 27)) for i in range(100)]
    xs += [ev(1, rw=0.5, und="BANKNIFTY"), ev(1, rw=None, und="BANKNIFTY", expiry=E2)]
    assert cr.claims(xs) == [{"underlying": "BANKNIFTY", "n_independent": 1, "claim_allowed": False},
                             {"underlying": "NIFTY", "n_independent": 100, "claim_allowed": True}]  # fmt: skip


def test_split_by_the_registration_date():
    xs = [ev(1, rw=0.5, origin=date(2026, 10, 21)), ev(1, rw=0.5, origin=date(2026, 10, 22), expiry=E2)]
    got = cr.split(xs)
    assert (got["before"]["events"], got["after"]["events"], got["pooled"]["events"]) == (1, 1, 2)
    assert got["registered"] == "2026-10-22"


def test_override_pnl_groups_use_the_cards_simulated_pnl():
    d = lambda i, dec, ovr: {"card_log_id": i, "card_index": 0, "decision": dec, "override": ovr}  # noqa: E731
    decisions = [d(1, "traded", True), d(2, "traded", True), d(3, "traded", False), d(4, "skipped", True),
                 d(5, "skipped", False), d(6, "traded", True)]  # fmt: skip
    outcomes = {(1, 0): {"status": "resolved", "value": -100.0, "cluster": "a"},
                (2, 0): {"status": "resolved", "value": 300.0, "cluster": "a"},
                (3, 0): {"status": "open", "value": None, "cluster": "b"},
                (4, 0): {"status": "void", "value": None, "cluster": "c"}}  # fmt: skip
    got = cr.override_pnl(decisions, outcomes)
    assert got["traded_against"] == {"decisions": 3, "resolved": 2, "mean_pnl": 100.0, "open": 0, "void": 0,
                                     "no_forecast": 1, "clusters": 1}  # fmt: skip
    assert got["traded_with"]["mean_pnl"] is None and got["traded_with"]["open"] == 1
    assert got["skipped_enter"]["void"] == 1 and got["skipped_other"]["no_forecast"] == 1


# --------------------------------------------------------------------------- the API (database)
@pytest.fixture
def client(env):
    from fastapi.testclient import TestClient

    from finresearch.api import create_app
    from finresearch.db import session_scope

    with session_scope() as s:
        s.execute(text("TRUNCATE forecast, card_log, card_override, prob_panel"))
    with TestClient(create_app()) as c:
        yield c


def _log_card() -> int:
    from finresearch.db import session_scope
    from finresearch.fnobook import card_log

    resp = {"symbol": "NIFTY", "cards": [{"title": "made-up", "action": "WAIT", "legs": []}]}
    with session_scope() as s:
        r = card_log.row("suggest", symbol="NIFTY", expiry=None, inputs={}, cards=card_log.summarise("suggest", resp),
                         response=resp, created_at=datetime(2026, 10, 23, 5, 0, tzinfo=UTC))  # fmt: skip
        s.add(r)
        s.flush()
        return r.id


def _counts() -> tuple[int, ...]:
    from finresearch.db import session_scope
    from finresearch.db.models import CardOverride, Forecast, ProbPanel

    with session_scope() as s:
        return tuple(
            s.scalar(select(func.count()).select_from(m)) for m in (Forecast, CardOverride, ProbPanel)
        )


def test_the_report_on_an_empty_ledger_is_unknown_and_the_get_writes_nothing(client):
    _log_card()  # a logged waiting card: no forecast, and the GET must not record one
    before = _counts()
    rep = client.get("/api/fno/card-ledger").json()
    assert _counts() == before == (0, 0, 0)
    after = rep["after"]
    assert after["events"] == 0 and after["bss"]["bss"] is None and after["cohort"]["delta"] is None
    assert [m["brier"] for m in after["models"]] == [None, None] and after["claims"] == []
    assert rep["overrides"]["rate"] is None and rep["overrides"]["rows"] == 0
    assert rep["panel"]["rows"] == 0 and rep["claim_min"] == 100


def test_post_records_an_override_and_get_lists_it(client):
    i = _log_card()
    r = client.post("/api/fno/overrides", json={"card_log_id": i, "card_index": 0, "decision": "traded",
                                                "note": "made-up note"})  # fmt: skip
    assert r.status_code == 200, r.text
    assert (r.json()["verdict"], r.json()["override"]) == ("WAIT", True)
    assert client.post("/api/fno/overrides", json={"card_log_id": i, "card_index": 3, "decision": "traded"}
                       ).status_code == 422  # fmt: skip
    assert client.post("/api/fno/overrides", json={"card_log_id": i, "card_index": 0, "decision": "maybe"}
                       ).status_code == 422  # fmt: skip
    got = client.get("/api/fno/overrides").json()
    assert got["total"] == 1 and got["overrides"][0]["note"] == "made-up note"
    rep = client.get("/api/fno/card-ledger").json()["overrides"]
    assert (rep["decisions"], rep["overrides"], rep["rate"]) == (1, 1, 1.0)
    assert rep["pnl"]["traded_against"]["no_forecast"] == 1  # a waiting card has no forecast: counted


def test_the_report_reads_resolved_card_forecasts_and_their_overrides(client):
    from finresearch.db import session_scope
    from finresearch.db.models import Forecast
    from finresearch.fnobook import card_forecasts as cf
    from finresearch.signals.tradeplan import PLAN_METHOD

    i = _log_card()
    client.post("/api/fno/overrides", json={"card_log_id": i, "card_index": 0, "decision": "traded"})
    inp = {"card_log_id": i, "card_index": 0, "symbol": "NIFTY", "expiry": "2026-11-03", "structure": "put_credit",
           "entry_day": "2026-10-23", "verdict": "WAIT", "ev": {}, "pnl_dots": None}  # fmt: skip
    with session_scope() as s:
        for tag, method, p in (("rw", PLAN_METHOD, 0.7), ("rn", cf.RN_METHOD, 0.6)):
            s.add(Forecast(asset="fno", instrument="NIFTY", source=f"{cf.SOURCE}{i}", event_kind=cf.EVENT_KIND,
                           event=cf.EVENT, horizon="h", resolve_on=date(2026, 11, 3), probability=p, action="WAIT",
                           method=method, validation_status="rule_based", inputs=inp, status="resolved", outcome=0,
                           resolution_value=-250.0, resolved_at=datetime(2026, 11, 4, 12, 0, tzinfo=UTC),
                           dedupe_key=f"card:{i}:0:{tag}"))  # fmt: skip
    rep = client.get("/api/fno/card-ledger").json()
    assert (rep["before"]["events"], rep["after"]["events"], rep["after"]["independent"]) == (0, 1, 1)
    assert rep["after"]["bss"]["bss"] == pytest.approx(1 - 0.49 / 0.36)  # one event: (0.7)^2 vs (0.6)^2
    assert rep["after"]["cohort"]["mean_fail"] == -250.0 and rep["cards"]["resolved"] == 1
    assert rep["overrides"]["pnl"]["traded_against"]["mean_pnl"] == -250.0

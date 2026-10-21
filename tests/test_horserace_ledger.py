"""Card-ledger events for the horse race (#377): resolved card_pnl forecasts become paired race events."""

from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest

from finresearch.fnobook.card_forecasts import RN_METHOD
from finresearch.horserace import ledger_events as le
from finresearch.signals.tradeplan import PLAN_METHOD

DOTS = [float(-500 + 50 * i) for i in range(20)]


def row(i, *, card=1, method=PLAN_METHOD, p=0.7, outcome=1, value=120.0, status="resolved", verdict="ENTER",
        expiry="2026-11-25", structure="put_credit", logged=datetime(2026, 10, 23, 5, 0, tzinfo=UTC),
        dots_model="real-world 20-day scenario"):  # fmt: skip
    inputs = {"card_log_id": card, "card_index": 0, "symbol": "NIFTY", "expiry": expiry, "structure": structure,
              "entry_day": "2026-10-23", "verdict": verdict, "probability": {"real_world": 0.7, "risk_neutral": 0.6},
              "ev": {"real_world": 40.0, "risk_neutral": -30.0},
              "pnl_dots": {"model": dots_model, "values": DOTS, "payoff_ratio": 0.5}}  # fmt: skip
    return SimpleNamespace(id=i, asset="fno", instrument="NIFTY", event_kind="card_pnl", method=method, probability=p,
                           status=status, outcome=outcome, resolution_value=value, inputs=inputs, created_at=logged,
                           resolved_at=datetime(2026, 11, 20, 11, 0, tzinfo=UTC), source=f"card_log:{card}",
                           event="e")  # fmt: skip


def test_a_card_becomes_one_event_with_both_models() -> None:
    [e] = le.events_from_rows([row(1), row(2, method=RN_METHOD, p=0.6)])
    assert (e.underlying, e.expiry, e.structure, e.origin) == ("NIFTY", date(2026, 11, 25), "put_credit",
                                                               date(2026, 10, 23))  # fmt: skip
    assert e.resolved_on == date(2026, 11, 20) and e.outcome == 1 and e.pnl == 120.0
    assert e.gate_pass is True and e.first is True
    rw, rn = e.forecasts["p_rw"], e.forecasts["p_rn_atm"]
    assert (rw.p, rw.ev, list(rw.dots or [])) == (0.7, 40.0, DOTS)
    assert (rn.p, rn.ev, rn.dots) == (
        0.6,
        -30.0,
        None,
    )  # the dots are the real-world scenario's, not the RN model's


def test_risk_neutral_dots_go_to_the_risk_neutral_forecaster() -> None:
    [e] = le.events_from_rows(
        [row(1, p=None, dots_model="risk-neutral (ATM IV)"), row(2, method=RN_METHOD, p=0.6)]
    )
    assert e.forecasts["p_rw"].p is None and e.forecasts["p_rw"].dots is None
    assert list(e.forecasts["p_rn_atm"].dots or []) == DOTS


def test_void_and_open_cards_are_not_events() -> None:
    assert le.events_from_rows([row(1, status="void", outcome=None, value=None)]) == []
    assert le.events_from_rows([row(1, status="open", outcome=None, value=None)]) == []


@pytest.mark.parametrize(("verdict", "gate"), [("ENTER", True), ("PAPER", True), ("WAIT", False),
                                               ("AVOID", False), (None, None)])  # fmt: skip
def test_gate_class(verdict, gate) -> None:
    [e] = le.events_from_rows([row(1, verdict=verdict)])
    assert e.gate_pass is gate


def test_only_the_first_card_per_cluster_is_independent() -> None:
    later = row(3, card=2, logged=datetime(2026, 10, 24, 5, 0, tzinfo=UTC))
    other = row(5, card=3, structure="call_credit", logged=datetime(2026, 10, 24, 6, 0, tzinfo=UTC))
    evs = {(e.structure, e.origin, e.first) for e in le.events_from_rows([row(1), later, other])}
    firsts = sorted(e.first for e in le.events_from_rows([row(1), later, other]))
    assert firsts == [False, True, True] and len(evs) >= 2


def test_unsorted_or_missing_dots_are_unknown() -> None:
    r = row(1)
    r.inputs["pnl_dots"]["values"] = [*DOTS[:-1], None]
    [e] = le.events_from_rows([r])
    assert e.forecasts["p_rw"].dots is None and e.forecasts["p_rw"].p == 0.7


def test_load_reads_resolved_card_forecasts_only(env) -> None:
    from sqlalchemy import text

    from finresearch.db import session_scope
    from finresearch.db.models import Forecast

    cols = {c.name for c in Forecast.__table__.columns} - {"id"}
    with session_scope() as s:
        s.execute(text("TRUNCATE forecast"))
        for i, (kind, status) in enumerate(
            [("card_pnl", "resolved"), ("card_pnl", "open"), ("listing_gain", "resolved")]
        ):
            r = row(i, status=status, card=10 + i)
            vals = {k: getattr(r, k) for k in cols if hasattr(r, k)}
            s.add(Forecast(**{**vals, "event_kind": kind, "horizon": "h", "resolve_on": date(2026, 11, 25),
                              "action": "ENTER", "validation_status": "rule_based", "dedupe_key": f"t{i}"}))  # fmt: skip
    with session_scope() as s:
        evs = le.load(s)
    assert [e.underlying for e in evs] == ["NIFTY"] and evs[0].pnl == 120.0


def test_cli_horse_race_scores_the_ledger_and_writes_json(env, tmp_path) -> None:
    import json

    from typer.testing import CliRunner

    from finresearch.cli import app

    test_load_reads_resolved_card_forecasts_only(env)  # one resolved card in the ledger
    out = tmp_path / "race.json"
    r = CliRunner().invoke(app, ["forecasts", "horse-race", "--window", "forward", "--json", str(out)])
    assert r.exit_code == 0, r.output
    assert "1 event" in r.output and "p_rw" in r.output and "adopted: none" in r.output
    rep = json.loads(out.read_text())
    assert rep["window"] == "forward" and rep["n_events"] == 1


def test_cli_horse_race_refuses_a_changed_spec(env, monkeypatch) -> None:
    from typer.testing import CliRunner

    from finresearch.cli import app
    from finresearch.horserace import prereg

    monkeypatch.setattr(prereg, "HASH", "0" * 64)
    r = CliRunner().invoke(app, ["forecasts", "horse-race"])
    assert r.exit_code != 0 and "refused" in r.output

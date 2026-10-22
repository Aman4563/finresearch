"""The 2022-26 card replay of the horse race (#377, addendum A1), on synthetic F&O history (tests/fno_synth.py: a
made-up index priced by Black-76 at one flat volatility; settle == close)."""

import math
from dataclasses import replace
from datetime import date

import pytest
from fno_synth import history as synth

from finresearch.backtest import engine
from finresearch.backtest import pricing as px
from finresearch.backtest.history import Day, MemoryHistory
from finresearch.backtest.specs import BOOK, SPECS
from finresearch.horserace import prereg, replay

PCS = next(s for s in SPECS if s.key == "nifty_pcs_m")
START, END = date(2022, 4, 1), date(2022, 5, 31)


@pytest.fixture(scope="module")
def hist() -> MemoryHistory:
    return synth(date(2021, 12, 1), date(2022, 8, 31), vol=0.15, seed=11)


@pytest.fixture(scope="module")
def made(hist):
    return replay.make_cards(hist, START, END, [PCS])


def test_require_registered_checks_the_addendum_too(monkeypatch) -> None:
    replay.require_registered()
    monkeypatch.setattr(prereg, "ADDENDUM_HASH", "0" * 64)
    with pytest.raises(replay.race.PreregistrationError):
        replay.require_registered()


def test_one_card_per_session_with_the_engines_expiry_and_legs(hist, made) -> None:
    cards, skips = made
    days = hist.sessions("NIFTY", START, END)
    assert cards and len({c.origin for c in cards}) == len(cards)  # one per session
    assert len(cards) + sum(skips.values()) == len(days)  # every session is a card or a counted skip
    for c in cards[:5]:
        x = hist.day("NIFTY", c.origin)
        assert 30 <= (c.expiry - c.origin).days <= 45 and c.expiry in x.futures
        fwd = px.forward(x, c.expiry, BOOK.rate)
        picked = engine.choose_legs(PCS, x, c.expiry, fwd, BOOK)
        assert [(q.right, q.strike, s) for q, s in picked] == [(lg["right"], lg["strike"], lg["side"])
                                                              for lg in c.inputs["legs"]]  # fmt: skip
        # A1.1: the settle moved by the tiered slippage
        q, side = picked[0]
        assert c.inputs["legs"][0]["entry"] == px.fill(q.settle, side, q.contracts, BOOK)
        assert c.inputs["exit_plan"] == {"profit_pct": 50.0, "stop_pct": 200.0, "exit_dte": 7,
                                         "hold_to_expiry": False}  # fmt: skip


def test_every_model_gives_a_pop_an_ev_and_20_dots(made) -> None:
    c = made[0][0]
    for k in ("p_rw20", "p_rw60", "p_rn_atm", "p_rw"):
        f = c.forecasts[k]
        assert 0 < f.p < 1 and math.isfinite(f.ev)
        assert len(f.dots) == 20 and all(a <= b for a, b in zip(f.dots, f.dots[1:], strict=False))
    lower = min((c.forecasts["p_rw20"], c.forecasts["p_rw60"]), key=lambda f: f.ev)
    assert c.forecasts["p_rw"] == lower


def test_atm_iv_recovers_the_flat_volatility(hist, made) -> None:
    c = made[0][0]
    x = hist.day("NIFTY", c.origin)
    fwd = px.forward(x, c.expiry, BOOK.rate)
    assert replay.atm_iv(x, c.expiry, fwd, (c.expiry - c.origin).days / 365, BOOK.rate) == pytest.approx(
        0.15, abs=2e-3
    )


def _day(d: date, spot: float | None, futs: dict[date, float]) -> Day:
    return Day(d, "NIFTY", futures=futs, spot=spot)


def test_returns_stay_on_one_instrument() -> None:
    e1, e2 = date(2022, 1, 27), date(2022, 2, 24)
    a = _day(date(2022, 1, 3), 100.0, {e1: 101.0})
    b = _day(date(2022, 1, 4), 110.0, {e1: 112.0})
    assert replay.same_instrument_return(a, b) == pytest.approx(math.log(1.1))  # the underlying value on both
    c = _day(date(2022, 1, 5), None, {e1: 120.0, e2: 121.0})
    assert replay.same_instrument_return(b, c) == pytest.approx(
        math.log(120 / 112)
    )  # no spot: the front future
    d = _day(date(2022, 1, 28), None, {e2: 130.0})  # e1 expired: e2 is on both days
    assert replay.same_instrument_return(c, d) == pytest.approx(math.log(130 / 121))
    assert (
        replay.same_instrument_return(
            _day(date(2022, 1, 3), 100.0, {}), _day(date(2022, 1, 4), None, {e2: 1.0})
        )
        is None
    )


def test_realised_vol_needs_n_returns() -> None:
    r = [0.01, -0.01] * 10
    assert replay.rv(r, 20) == pytest.approx(math.sqrt(sum((x - 0) ** 2 for x in r) / 19 * 252))
    assert replay.rv(r[:19], 20) is None


def _truncated(h: MemoryHistory, last: date) -> MemoryHistory:
    return MemoryHistory([x for x in h.days["NIFTY"].values() if x.day <= last])


def test_a_cards_forecasts_never_read_past_its_origin(hist, made) -> None:
    c = made[0][3]
    cards, _ = replay.make_cards(_truncated(hist, c.origin), START, c.origin, [PCS])
    assert cards[-1].origin == c.origin

    # the later sessions changed nothing; p_rw_bus is the one forecast that reads the stored session calendar past
    # the origin (addendum A2.5), tested in test_horserace_bustime.py
    def known(f: dict) -> dict:
        return {k: v for k, v in f.items() if k != "p_rw_bus"}

    assert known(cards[-1].forecasts) == known(c.forecasts) and cards[-1].inputs == c.inputs


def test_a_leg_without_a_settlement_price_is_a_counted_skip(hist, made) -> None:
    c = made[0][0]
    x = hist.day("NIFTY", c.origin)
    bare = Day(
        x.day, x.symbol, {k: replace(q, settle=None) for k, q in x.options.items()}, x.futures, x.spot, x.iv30
    )
    h2 = MemoryHistory([bare if y.day == c.origin else y for y in hist.days["NIFTY"].values()])
    cards, skips = replay.make_cards(h2, c.origin, c.origin, [PCS])
    assert cards == [] and skips == {"nifty_pcs_m: no settlement price for a leg": 1}


def test_an_unknown_lot_size_is_a_counted_skip(made) -> None:
    h = synth(date(2021, 12, 1), date(2022, 6, 30), vol=0.15, seed=11, lot=None)
    d = made[0][0].origin  # a session with an expiry in the window
    cards, skips = replay.make_cards(h, d, d, [PCS])
    assert cards == [] and set(skips) == {"nifty_pcs_m: lot size unknown"}


def test_gate_proxy_is_the_specs_iv_filter_and_unknown_for_controls(made, hist) -> None:
    # the synthetic iv30 is flat, so its percentile is 0 < 0.30: the seller's filter fails
    assert {c.gate_pass for c in made[0]} == {False}
    dcs = next(s for s in SPECS if s.key == "nifty_dcs_m")
    cards, _ = replay.make_cards(hist, START, END, [dcs])
    assert cards and {c.gate_pass for c in cards} == {None}


def test_resolution_matches_the_engines_trade_for_the_same_entry(hist, made) -> None:
    # the engine's recipe with its IV filter off and one lot: its first trade enters on a session we also replay
    spec = replace(PCS, iv_pct_min=None)
    run = engine.run([spec], hist, START, END, replace(BOOK, max_lots=1))[PCS.key]
    tr = run["trades"][0]
    events, _ = replay.resolve(made[0], hist)
    ev = next(e for e in events if e.origin.isoformat() == tr["entry_day"])
    assert ev.pnl == pytest.approx(tr["net"], abs=0.01) and ev.outcome == int(tr["net"] > 0)
    assert ev.resolved_on == ev.expiry  # A1.11: conservative


def test_unfinished_cards_are_counted_and_first_marks_one_per_cluster(made) -> None:
    short = synth(date(2021, 12, 1), date(2022, 5, 20), vol=0.15, seed=11)  # data ends before May's expiry
    events, counts = replay.resolve([c for c in made[0] if c.origin <= date(2022, 5, 20)], short)
    assert counts["not yet"] > 0
    firsts: dict[tuple, int] = {}
    for e in events:
        firsts[(e.underlying, e.expiry, e.structure)] = (
            firsts.get((e.underlying, e.expiry, e.structure), 0) + e.first
        )
    assert firsts and set(firsts.values()) == {1}
    for e in events:  # the first is the earliest resolved session of its cluster
        if e.first:
            assert e.origin == min(x.origin for x in events if x.expiry == e.expiry)


def test_run_window_scores_every_replay_forecaster(hist) -> None:
    rep = replay.run_window(hist, "design", spec_list=[PCS])
    assert rep["n_events"] > 0 and rep["window"] == "design"
    assert {"p_rw", "p_rw20", "p_rw60", "p_rn_atm", "p_base", "p_gate"} <= set(rep["forecasters"])
    assert rep["replay"]["counts"]["events"] >= rep["n_events"] and "skips" in rep["replay"]
    assert rep["replay"]["addendum_hash"] == prereg.ADDENDUM_HASH
    assert rep["adopted"] == []  # design never adopts


def test_run_window_refuses_forward_and_unknown_windows(hist) -> None:
    with pytest.raises(ValueError, match="card-ledger"):
        replay.run_window(hist, "forward", spec_list=[PCS])
    with pytest.raises(ValueError, match="unknown window"):
        replay.run_window(hist, "vault", spec_list=[PCS])


def test_cli_replay_source_on_an_empty_database(env) -> None:
    from typer.testing import CliRunner

    from finresearch.cli import app

    r = CliRunner().invoke(app, ["forecasts", "horse-race", "--source", "replay", "--window", "seen"])
    assert r.exit_code == 0, r.output
    assert "0 event(s)" in r.output and "replay" in r.output
    flat = " ".join(r.output.split())
    assert "challenger p_rw_bus" in flat and "T_bus unknown on 0 card(s)" in flat
    assert "challenger p_rn_b76" in flat and "unknown on 0 card(s); reasons {}" in flat
    r = CliRunner().invoke(app, ["forecasts", "horse-race", "--source", "replay", "--window", "forward"])
    assert r.exit_code == 2 and "card-ledger" in r.output

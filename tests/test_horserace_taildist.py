"""The tail-aware challengers p_rw_emp and p_rw_fhs in the card replay (#387, horse-race addendum A5), on synthetic
F&O history (tests/fno_synth.py: Black-76 at one flat volatility on a seeded GBM path, so z is standard normal and
kappa is about 1)."""

import math
from datetime import date

import numpy as np
import pytest
from fno_synth import history as synth
from fno_synth import path

from finresearch.backtest.specs import SPECS
from finresearch.fincalc import options as o
from finresearch.fincalc import taildist as td
from finresearch.horserace import addendum_a5, prereg, race, replay

ICW = next(s for s in SPECS if s.key == "nifty_ic_w")
PCS = next(s for s in SPECS if s.key == "nifty_pcs_m")
# one shared synthetic history and as few replay runs as possible (each card costs about 0.4 s in the replay)
FIRST, LAST = date(2021, 1, 4), date(2022, 5, 20)
START, END, CUT = date(2022, 4, 1), date(2022, 5, 6), date(2022, 4, 22)
VOL = 0.15


@pytest.fixture(scope="module")
def prices() -> dict[date, float]:
    return path(FIRST, LAST, vol=VOL, seed=21)


@pytest.fixture(scope="module")
def hist(prices):
    return synth(FIRST, LAST, vol=VOL, prices=prices)


@pytest.fixture(scope="module")
def made(hist):
    return replay.make_cards(hist, START, END, [ICW, PCS])


@pytest.fixture(scope="module")
def weekly_alone(hist):
    return replay.make_cards(hist, START, END, [ICW])[0]


def test_weekly_cards_carry_both_challengers_near_the_market_lognormal(made) -> None:
    cards = [c for c in made[0] if c.spec == "nifty_ic_w"]
    assert len(cards) > 10
    for c in cards:
        emp, fhs, ref = c.forecasts["p_rw_emp"], c.forecasts["p_rw_fhs"], c.forecasts["p_rn_atm"]
        assert c.tail["bucket"] == "short" and c.tail["n_z"] >= td.MIN_Z and c.tail["reason_emp"] is None
        assert c.tail["kappa"] == pytest.approx(1.0, abs=0.35)  # about 60 standard-normal z
        assert 0 < emp.p < 1 and len(emp.dots) == 20 and emp.p == pytest.approx(ref.p, abs=0.12)
        assert 0 < fhs.p < 1 and len(fhs.dots) == 20 and fhs.p == pytest.approx(ref.p, abs=0.12)
        assert c.tail["es_emp"] > 0 and c.tail["es_fhs"] > 0


def test_the_z_sample_holds_only_expiries_settled_before_the_origin(made) -> None:
    for c in made[0]:
        if c.tail["last_settled"] is not None:
            assert c.tail["last_settled"] < c.origin


def test_monthly_cards_are_unknown_until_thirty_monthly_expiries_have_settled(made) -> None:
    cards = [c for c in made[0] if c.spec == "nifty_pcs_m"]
    assert cards
    for c in cards:
        assert c.tail["bucket"] == "long" and c.tail["n_z"] < td.MIN_Z
        assert c.forecasts["p_rw_emp"].p is None and "z values" in c.tail["reason_emp"]
        assert c.forecasts["p_rw_fhs"].p is None and "kappa" in c.tail["reason_fhs"]


def test_no_look_ahead_a_different_future_leaves_earlier_cards_unchanged(prices, weekly_alone) -> None:
    other = path(FIRST, LAST, vol=0.45, seed=99)  # a different, wilder path after CUT
    mixed = {d: (s if d <= CUT else other[d] * prices[CUT] / other[CUT]) for d, s in prices.items()}
    a = weekly_alone
    b, _ = replay.make_cards(synth(FIRST, LAST, vol=VOL, prices=mixed), START, END, [ICW])
    early = [(x, y) for x, y in zip(a, b, strict=True) if x.origin <= CUT]
    assert early and any(x.origin == CUT for x, _ in early)
    for x, y in early:
        for k in ("p_rw_emp", "p_rw_fhs"):
            assert x.forecasts[k] == y.forecasts[k], (x.origin, k)
    late = [(x, y) for x, y in zip(a, b, strict=True) if x.origin > date(2022, 4, 28)]
    assert late and any(
        x.forecasts["p_rw_emp"] != y.forecasts["p_rw_emp"] for x, y in late
    )  # the test can see a change


def test_card_seeds_do_not_depend_on_which_specs_run(made, weekly_alone) -> None:
    pick = [c for c in made[0] if c.spec == "nifty_ic_w"]  # run with the monthly spec alongside
    assert pick and [c.forecasts["p_rw_fhs"] for c in weekly_alone] == [c.forecasts["p_rw_fhs"] for c in pick]


def test_vectorised_exit_cost_equals_the_card_exit_cost() -> None:
    from finresearch.fincalc import charges as ch

    legs = [o.Leg("call", 24500, 110.0, 75), o.Leg("call", 25000, 30.0, -75), o.Leg("put", 23000, 20.0, 75)]
    expiry = date(2025, 1, 30)
    xc = replay.exercise_stt_cost(legs, expiry)
    for p in (22000.0, 24000.0, 24800.0, 26000.0):
        ref = ch.exercise_stt(sum(x.qty * (max(0.0, p - x.strike) if x.right == "call" else max(0.0, x.strike - p))
                                  for x in legs if x.qty > 0), expiry)  # fmt: skip
        assert xc(p) == pytest.approx(ref) and float(xc(np.array([p]))[0]) == pytest.approx(ref)


def test_the_replay_refuses_a_changed_addendum_a5(monkeypatch) -> None:
    monkeypatch.setattr(prereg, "ADDENDUM_A5_HASH", "0" * 64)
    with pytest.raises(race.PreregistrationError, match="addendum_a5"):
        replay.require_registered()


@pytest.fixture(scope="module")
def design(hist):
    return replay.run_window(hist, "design", [ICW], n_paths=50)  # few paths: p_path (A6) is tested elsewhere


def test_the_race_lists_both_tail_challengers(design) -> None:
    # addendum A6 (#388) appended p_path after A5's challengers
    assert design["challengers"] == [*addendum_a5.CHALLENGERS, "p_path"]
    assert design["replay"]["addendum_a5_hash"] == prereg.ADDENDUM_A5_HASH
    assert math.isfinite(design["replay"]["p_rw_emp_unknown"]) and "p_rw_fhs_reasons" in design["replay"]


def test_card_seed_is_the_registered_string() -> None:
    import hashlib

    want = np.random.default_rng(
        int.from_bytes(hashlib.sha256(b"387|NIFTY|nifty_ic_w|2022-04-01|2022-04-07").digest()[:8], "big")
    )
    got = replay.card_rng("NIFTY", "nifty_ic_w", date(2022, 4, 1), date(2022, 4, 7))
    assert (got.random(5) == want.random(5)).all()


def test_an_expiry_without_a_settlement_price_is_counted_not_lost(prices) -> None:
    from dataclasses import replace

    h = synth(
        FIRST, date(2022, 4, 8), vol=VOL, prices={d: v for d, v in prices.items() if d <= date(2022, 4, 8)}
    )
    e = date(
        2022, 3, 3
    )  # a weekly expiry: no future, so its settlement needs a uniform settle or the underlying
    h.days["NIFTY"][e] = replace(h.days["NIFTY"][e], spot=None)
    _, skips = replay.make_cards(h, date(2022, 4, 8), date(2022, 4, 8), [ICW])  # one session of cards
    assert skips["A5 z not formed (NIFTY): settlement price unknown"] >= 1


def test_the_design_window_scores_both_challengers_on_resolved_events(design, tmp_path) -> None:
    import json

    for k in ("p_rw_emp", "p_rw_fhs"):
        f = design["forecasters"][k]["NIFTY"]
        assert f["n"] > 0 and math.isfinite(f["brier"]) and math.isfinite(f["crps"]), (k, f)
    (tmp_path / "rep.json").write_text(json.dumps(design, indent=1, default=str))  # the CLI's --json path


def test_models_with_the_vectorised_exit_cost_are_bit_identical_to_the_per_price_stt() -> None:
    # replay.models charges A1.6's exercise STT through ExerciseSttCost (one dated-rate lookup per card instead of
    # one per grid price); every PoP, EV and dot must equal the per-price charges.exercise_stt closure exactly
    from finresearch.fincalc import charges as ch

    legs = [o.Leg("put", 17000, 95.0, -50), o.Leg("put", 16500, 30.0, 50), o.Leg("call", 18500, 12.0, 50)]
    expiry, spot, t, rate = date(2022, 4, 28), 17400.0, 27 / 365, 0.065

    def old(p: float) -> float:
        return ch.exercise_stt(sum(x.qty * (max(0.0, p - x.strike) if x.right == "call" else max(0.0, x.strike - p))
                                   for x in legs if x.qty > 0), expiry)  # fmt: skip

    got = replay.models(legs, expiry, spot, t, rate, 90.0, {"p_rw20": 0.14, "p_rw60": None, "p_rn_atm": 0.18})
    for key, sigma in (("p_rw20", 0.14), ("p_rn_atm", 0.18)):
        oc = o.outcome(legs, spot, t, sigma, rate, rate, 90.0, old, steps=4000)
        dist = o.pnl_distribution(legs, spot, t, sigma, rate, 90.0, old, 2000)
        ref = (oc.pop, oc.ev, [round(v, 2) for v in o.weighted_quantiles(dist, replay.DOTS)])
        assert (got[key].p, got[key].ev, list(got[key].dots)) == ref, key
        assert type(got[key].ev) is float

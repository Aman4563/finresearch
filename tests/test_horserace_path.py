"""The exit-rule Monte Carlo challenger p_path in the card replay (#388, horse-race addendum A6), on synthetic F&O
history (tests/fno_synth.py: Black-76 at one flat volatility, a flat 6.5 % rate, futures on monthly expiries)."""

import math
from datetime import date

import pytest
from fno_synth import history as synth

from finresearch.backtest.history import MemoryHistory
from finresearch.backtest.specs import SPECS
from finresearch.horserace import addendum_a6, prereg, race, replay

PCS = next(s for s in SPECS if s.key == "nifty_pcs_m")
DCS = next(s for s in SPECS if s.key == "nifty_dcs_m")
START, END = date(2022, 4, 1), date(2022, 4, 30)
N = 300  # paths per card in these tests (the registered replay uses addendum_a6.N_PATHS)


@pytest.fixture(scope="module")
def hist() -> MemoryHistory:
    return synth(date(2021, 1, 4), date(2022, 8, 31), vol=0.15, seed=11)


@pytest.fixture(scope="module")
def made(hist):
    return replay.make_cards(hist, START, END, [PCS, DCS], n_paths=N)


def test_each_card_carries_p_path_with_its_monte_carlo_error(made) -> None:
    cards, _ = made
    pcs = [c for c in cards if c.spec == "nifty_pcs_m"]
    assert pcs
    for c in pcs[:4]:
        f, mc = c.forecasts["p_path"], c.path
        assert 0 < f.p < 1 and len(f.dots) == 20 and f.ev is not None and mc["reason"] is None
        assert mc["n"] == N and mc["pop_se"] == pytest.approx(math.sqrt(f.p * (1 - f.p) / N))
        assert mc["beta"] == pytest.approx(0.0, abs=1e-12)  # the synthetic iv30 never moves
        assert (
            mc["sigma_case"] in ("p_rw20", "p_rw60")
            and c.forecasts[mc["sigma_case"]].ev == c.forecasts["p_rw"].ev
        )
        assert sum(mc["reasons"].values()) == pytest.approx(1.0) and set(mc["reasons"]) <= {
            "target",
            "stop",
            "time",
        }
        assert mc["seed"] == addendum_a6.card_seed(c.spec, c.origin)


def test_a_hold_to_expiry_spec_has_the_incumbents_expiry_pop_within_the_monte_carlo_error(made) -> None:
    """A6.2: the paths' terminal distribution is p_rw's, so with no early exit (the debit control settles at expiry)
    p_path and p_rw differ only by simulation error (and the tick grid, which settlement does not touch)."""
    cards, _ = made
    dcs = [c for c in cards if c.spec == "nifty_dcs_m"]
    assert dcs
    for c in dcs[:4]:
        f, rw = c.forecasts["p_path"], c.forecasts["p_rw"]
        assert c.path["reasons"] == {"expiry": 1.0}
        assert f.p == pytest.approx(rw.p, abs=4 * c.path["pop_se"] + 0.005)


def test_an_unknown_spot_vol_slope_leaves_p_path_unknown() -> None:
    h = synth(date(2021, 1, 4), date(2022, 6, 30), vol=0.15, seed=11, iv30=None)
    cards, _ = replay.make_cards(h, START, END, [PCS], n_paths=N)
    assert cards and all(c.forecasts["p_path"].p is None for c in cards)
    assert all(c.path["reason"].startswith("beta unknown") for c in cards)


def test_the_replay_refuses_a_changed_addendum_a6(monkeypatch) -> None:
    monkeypatch.setattr(prereg, "ADDENDUM_A6_HASH", "0" * 64)
    with pytest.raises(race.PreregistrationError, match="addendum_a6"):
        replay.require_registered()


def test_the_exit_reason_score_is_against_trailing_dirichlet_frequencies() -> None:
    """A6.8 by hand: e1 (no earlier event) has the uniform 1/4 base; e2, after e1's stop resolved, has base
    stop (1 + 1) / (1 + 4) = 0.4 and 0.2 elsewhere, and observed a target."""
    d = date
    ev = lambda o, r, why: race.Event("NIFTY", r, "put_credit", o, r, 1, 1.0, None, True, {}, why)  # noqa: E731
    e1, e2 = ev(d(2024, 1, 1), d(2024, 1, 25), "stop"), ev(d(2024, 2, 1), d(2024, 2, 29), "profit_take")
    shares = {
        replay.event_key(e1): {"stop": 1.0},
        replay.event_key(e2): {"target": 0.5, "stop": 0.3, "time": 0.2},
    }
    got = replay.exit_reason_score([e1, e2], shares)
    assert got["n"] == 2
    assert got["model"] == pytest.approx((0.0 + math.log(2)) / 2)
    assert got["base"] == pytest.approx((math.log(4) + math.log(5)) / 2)
    assert got["model_minus_base"] == pytest.approx(got["model"] - got["base"])


def test_the_race_lists_p_path_and_reports_the_simulation_error(hist) -> None:
    rep = replay.run_window(hist, "design", [PCS], n_paths=60)
    assert rep["challengers"] == list(addendum_a6.CHALLENGERS) and "p_path" in rep["forecasters"]
    r = rep["replay"]
    assert r["addendum_a6_hash"] == prereg.ADDENDUM_A6_HASH and r["n_paths"] == 60
    mc = r["p_path_mc"]
    assert 0 < mc["mean_se"] <= mc["max_se"] <= 0.5 / math.sqrt(60) + 1e-12
    assert 0 < mc["brier_noise"] <= 0.25 / 60 and mc["unknown"] == 0
    assert r["exit_reasons"]["n"] > 0 and math.isfinite(r["exit_reasons"]["model"])

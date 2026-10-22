"""The SVI density challenger p_rn_svi in the card replay (#386, horse-race addendum A4), on synthetic F&O history
(tests/fno_synth.py: Black-76 at one flat volatility, so the fitted smile is flat and its density is the lognormal:
p_rn_svi must then agree with p_rn_b76, which uses the lognormal at the same ATM IV)."""

import math
from dataclasses import replace
from datetime import date

import pytest
from fno_synth import history as synth

from finresearch.backtest.history import MemoryHistory
from finresearch.backtest.specs import SPECS
from finresearch.horserace import addendum_a4, prereg, race, replay

PCS = next(s for s in SPECS if s.key == "nifty_pcs_m")
START, END = date(2022, 4, 1), date(2022, 4, 30)
VOL = 0.15


@pytest.fixture(scope="module")
def hist() -> MemoryHistory:
    return synth(date(2021, 12, 1), date(2022, 8, 31), vol=VOL, seed=11)


@pytest.fixture(scope="module")
def made(hist):
    return replay.make_cards(hist, START, END, [PCS])


def _day(hist):
    x = hist.day("NIFTY", date(2022, 4, 4))
    expiry = min(e for e in x.futures if (e - x.day).days > 20)
    return x, expiry, (expiry - x.day).days / 365


def test_the_smile_of_a_flat_volatility_chain_is_flat(hist) -> None:
    x, expiry, t = _day(hist)
    got = replay.svi_smile(x, expiry, t, replay.b76_atm(x, expiry, t))
    assert got["reason"] is None and got["n"] >= 10
    assert got["rmse_vol"] < 2e-3  # settles are rounded to the 5-paisa tick
    assert got["atm"] == pytest.approx(VOL, abs=2e-3) and abs(got["rr25"]) < 3e-3 and abs(got["bf25"]) < 3e-3


def test_an_unknown_discount_factor_leaves_the_smile_unknown(hist) -> None:
    x, expiry, t = _day(hist)
    b3 = replay.b76_atm(x, expiry, t) | {"df": None, "reason": "discount factor unknown: test"}
    got = replay.svi_smile(x, expiry, t, b3)
    assert got["params"] is None and "discount factor" in got["reason"]


def test_too_few_strikes_leave_the_smile_unknown(hist) -> None:
    x, expiry, t = _day(hist)
    b3 = replay.b76_atm(x, expiry, t)
    near = sorted({k for (e, _, k) in x.options if e == expiry}, key=lambda s: abs(s - b3["forward"]))[:4]
    y = replace(x, options={key: q for key, q in x.options.items() if key[0] != expiry or key[2] in near})
    got = replay.svi_smile(y, expiry, t, b3)
    assert got["params"] is None and "fewer than" in got["reason"]


def test_each_card_carries_p_rn_svi_equal_to_p_rn_b76_on_a_flat_smile(made) -> None:
    cards, _ = made
    assert cards
    for c in cards[:5]:
        f, ref = c.forecasts["p_rn_svi"], c.forecasts["p_rn_b76"]
        assert 0 < f.p < 1 and len(f.dots) == 20
        # the same PoP; EVs on two grids (+/- 12 ATM sd vs the lognormal's +/- 6 sigma) agree within Rs 2 on ~Rs 200
        assert f.p == pytest.approx(ref.p, abs=1e-3) and f.ev == pytest.approx(ref.ev, abs=2.0)
        assert c.svi["reason"] is None and c.svi["params"] is not None


def test_the_replay_refuses_a_changed_addendum_a4(monkeypatch) -> None:
    monkeypatch.setattr(prereg, "ADDENDUM_A4_HASH", "0" * 64)
    with pytest.raises(race.PreregistrationError, match="addendum_a4"):
        replay.require_registered()


def test_the_race_lists_p_rn_svi_as_a_challenger(hist) -> None:
    rep = replay.run_window(hist, "seen", [PCS])
    assert rep["challengers"] == list(addendum_a4.CHALLENGERS)
    assert rep["replay"]["addendum_a4_hash"] == prereg.ADDENDUM_A4_HASH
    assert "p_rn_svi_unknown" in rep["replay"] and math.isfinite(rep["replay"]["p_rn_svi_unknown"])

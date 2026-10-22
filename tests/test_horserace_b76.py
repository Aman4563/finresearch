"""The forward/IV challenger p_rn_b76 in the card replay (#385, horse-race addendum A3), on synthetic F&O history
(tests/fno_synth.py: Black-76 at one flat volatility, a flat 6.5 % rate, futures on monthly expiries)."""

import math
from dataclasses import replace
from datetime import date

import pytest
from fno_synth import RATE
from fno_synth import history as synth

from finresearch.backtest.history import MemoryHistory
from finresearch.backtest.specs import SPECS
from finresearch.horserace import addendum_a3, prereg, race, replay

PCS = next(s for s in SPECS if s.key == "nifty_pcs_m")
START, END = date(2022, 4, 1), date(2022, 4, 30)
VOL = 0.15


@pytest.fixture(scope="module")
def hist() -> MemoryHistory:
    return synth(date(2021, 12, 1), date(2022, 8, 31), vol=VOL, seed=11)


@pytest.fixture(scope="module")
def made(hist):
    return replay.make_cards(hist, START, END, [PCS])


def test_one_forward_per_expiry_recovers_the_future_and_the_rate(hist) -> None:
    x = hist.day("NIFTY", date(2022, 4, 4))
    expiry = min(e for e in x.futures if (e - x.day).days > 20)
    t = (expiry - x.day).days / 365
    got = replay.b76_atm(x, expiry, t)
    # the synthetic future is S e^(rT) rounded to the paisa, the options are priced on it at 6.5 % and 15 %
    assert got["forward"] == pytest.approx(x.futures[expiry], abs=0.05)
    assert got["r_impl"] == pytest.approx(RATE, abs=0.002) and got["sigma"] == pytest.approx(VOL, abs=1e-3)
    assert got["basis"] == pytest.approx(0.0, abs=5e-6) and got["n"] >= 10 and got["reason"] is None


def test_only_market_settles_enter(hist) -> None:
    x = hist.day("NIFTY", date(2022, 4, 4))
    expiry = min(e for e in x.futures if (e - x.day).days > 20)
    t = (expiry - x.day).days / 365
    # a model settle (no trade, differs from the close) 60 rupees off at the money would move a one-strike forward
    k = min({k for (e, _, k) in x.options if e == expiry}, key=lambda s: abs(s - x.futures[expiry]))
    q = x.options[(expiry, "call", k)]
    y = replace(
        x,
        options={**x.options, (expiry, "call", k): replace(q, settle=q.settle + 60, contracts=0, close=None)},
    )
    got = replay.b76_atm(y, expiry, t)
    assert k not in got["strikes"] and got["forward"] == pytest.approx(x.futures[expiry], abs=0.05)


def test_an_unidentified_discount_factor_leaves_the_challenger_unknown(hist) -> None:
    x = hist.day("NIFTY", date(2022, 4, 4))
    expiry = min(e for e in x.futures if (e - x.day).days > 20)
    t = (expiry - x.day).days / 365
    fwd = x.futures[expiry]
    keep = sorted({k for (e, _, k) in x.options if e == expiry}, key=lambda s: abs(s - fwd))[:3]
    noisy = {key: (replace(q, settle=q.settle + 3.0, close=q.close + 3.0) if key[1] == "call" and key[2] == max(keep)
                   else q) for key, q in x.options.items() if key[0] != expiry or key[2] in keep}  # fmt: skip
    got = replay.b76_atm(replace(x, options=noisy), expiry, t)
    assert got["sigma"] is None and "discount factor" in got["reason"] and got["forward"] is not None


def test_each_card_carries_p_rn_b76_close_to_p_rn_atm(made) -> None:
    cards, _ = made
    assert cards
    for c in cards[:5]:
        f, ref = c.forecasts["p_rn_b76"], c.forecasts["p_rn_atm"]
        assert 0 < f.p < 1 and len(f.dots) == 20 and f.p == pytest.approx(ref.p, abs=0.01)
        assert c.b76["sigma"] == pytest.approx(VOL, abs=2e-3) and c.b76["reason"] is None


def test_the_replay_refuses_a_changed_addendum_a3(monkeypatch) -> None:
    monkeypatch.setattr(prereg, "ADDENDUM_A3_HASH", "0" * 64)
    with pytest.raises(race.PreregistrationError, match="addendum_a3"):
        replay.require_registered()


def test_the_race_lists_p_rn_b76_as_a_challenger(hist) -> None:
    rep = replay.run_window(hist, "seen", [PCS])
    # later addenda append (A4, #386: p_rn_svi; A5, #387: p_rw_emp, p_rw_fhs); A3's challengers stay the leading
    # ones, in order
    assert rep["challengers"][: len(addendum_a3.CHALLENGERS)] == list(addendum_a3.CHALLENGERS)
    assert rep["replay"]["addendum_a3_hash"] == prereg.ADDENDUM_A3_HASH
    assert "p_rn_b76_unknown" in rep["replay"] and math.isfinite(rep["replay"]["p_rn_b76_unknown"])

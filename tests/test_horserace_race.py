"""The card horse race runner (#377): trailing forecasters, pairing, per-expiry DM with Holm, tail gating and the
pre-registered adoption rule, on synthetic events."""

from dataclasses import replace
from datetime import date, timedelta

import numpy as np
import pytest

from finresearch.horserace import prereg, race, specs

D0 = date(2026, 11, 3)
UDOTS = [-100 + 200 * (i + 0.5) / 20 for i in range(20)]  # U(-100, 100)


def ev(i: int, outcome: int, pnl: float, *, origin: date | None = None, resolved: date | None = None,
       gate: bool | None = None, sym: str = "NIFTY", **fc: race.Forecast) -> race.Event:  # fmt: skip
    o = origin or D0 + timedelta(days=7 * i)
    return race.Event(underlying=sym, expiry=o + timedelta(days=5), structure="iron_condor", origin=o,
                      resolved_on=resolved or o + timedelta(days=5), outcome=outcome, pnl=pnl, gate_pass=gate,
                      first=True, forecasts=dict(fc))  # fmt: skip


def test_require_registered_refuses_a_changed_spec(monkeypatch: pytest.MonkeyPatch) -> None:
    assert race.require_registered() == prereg.HASH
    monkeypatch.setattr(prereg, "HASH", "0" * 64)
    with pytest.raises(race.PreregistrationError):
        race.require_registered()


def test_trailing_rates_use_only_events_resolved_before_the_origin() -> None:
    a = ev(0, 1, 10.0, origin=date(2026, 11, 2), resolved=date(2026, 11, 6), gate=True)
    c = ev(
        0, 0, -5.0, origin=date(2026, 11, 4), resolved=date(2026, 11, 13), gate=False
    )  # a not yet resolved
    b = ev(0, 0, -5.0, origin=date(2026, 11, 9), resolved=date(2026, 11, 13), gate=True)
    out = {e.origin: e for e in race.with_trailing([b, a, c])}
    assert out[date(2026, 11, 2)].forecasts["p_base"].p == pytest.approx(0.5)
    assert out[date(2026, 11, 4)].forecasts["p_base"].p == pytest.approx(0.5)
    # b sees a (1 of 1): (1 + 1) / (1 + 2); its gate class (pass) holds a too
    assert out[date(2026, 11, 9)].forecasts["p_base"].p == pytest.approx(2 / 3)
    assert out[date(2026, 11, 9)].forecasts["p_gate"].p == pytest.approx(2 / 3)
    # c's gate class (fail) has nothing resolved before it
    assert out[date(2026, 11, 4)].forecasts["p_gate"].p == pytest.approx(0.5)


def test_unknown_gate_gives_no_gate_forecast() -> None:
    e = race.with_trailing([ev(0, 1, 1.0, gate=None)])[0]
    assert e.forecasts["p_gate"].p is None  # unknown, never 1/2


def _calibrated(n: int, challenger: str = "p_new") -> list[race.Event]:
    # y on a stratified, shuffled grid of U(-100, 100): the dots are exactly right, P(y > 0) = .5
    u = (np.arange(n) + 0.5) / n
    rng = np.random.default_rng(0)
    rng.shuffle(u)
    out = []
    for i, x in enumerate(u):
        y = float(-100 + 200 * x)
        out.append(ev(i, int(y > 0), y, p_rw=race.Forecast(0.9, 50.0, UDOTS),
                      **{challenger: race.Forecast(0.5, 0.0, UDOTS)}))  # fmt: skip
    return out


def test_a_better_calibrated_challenger_is_adopted_on_100_plus_events() -> None:
    rep = race.run(_calibrated(120), window="forward", challengers=("p_new",))
    nifty = rep["adoption"]["p_new"]["NIFTY"]
    assert rep["dm"]["brier"]["p_new"]["NIFTY"]["p_holm"] < 0.05
    assert nifty["tail"]["mode"] == "test"
    assert nifty["adopt"] is True, nifty["why"]


def test_no_adoption_below_100_independent_events() -> None:
    rep = race.run(_calibrated(60), window="forward", challengers=("p_new",))
    a = rep["adoption"]["p_new"]["NIFTY"]
    assert a["adopt"] is False and "100" in a["why"]
    assert rep["forecasters"]["p_new"]["NIFTY"]["tail"]["mode"] == "descriptive"


def test_no_adoption_outside_a_primary_window_or_for_a_benchmark() -> None:
    rep = race.run(_calibrated(120), window="seen", challengers=("p_new",))
    assert rep["adoption"]["p_new"]["NIFTY"]["adopt"] is False
    rep = race.run(_calibrated(120, "p_rn_atm"), window="forward", challengers=())
    assert rep["adoption"]["p_rn_atm"]["NIFTY"]["adopt"] is False  # a benchmark is reported, never adopted


def test_missing_probabilities_are_counted_not_scored() -> None:
    evs = _calibrated(20)
    evs[0].forecasts["p_new"] = race.Forecast(None, None, None)
    rep = race.run(evs, window="forward", challengers=("p_new",))
    f = rep["forecasters"]["p_new"]["NIFTY"]
    assert f["n"] == 19 and f["missing"] == 1


def test_every_forecaster_is_reported() -> None:
    rep = race.run(_calibrated(30), window="forward", challengers=("p_new",))
    assert {"p_rw", "p_new", "p_base"} <= set(rep["forecasters"])
    assert rep["registration_hash"] == specs.registration_hash()


def test_a_brier_winner_with_too_narrow_tails_is_not_adopted() -> None:
    narrow = [d / 5 for d in UDOTS]  # U(-20, 20) against outcomes from U(-100, 100): ~40 % below its 10 % VaR
    evs = [
        replace(e, forecasts={**e.forecasts, "p_new": race.Forecast(0.5, 0.0, narrow)})
        for e in _calibrated(120)
    ]
    rep = race.run(evs, window="forward", challengers=("p_new",))
    a = rep["adoption"]["p_new"]["NIFTY"]
    assert rep["dm"]["brier"]["p_new"]["NIFTY"]["p_holm"] < 0.05
    assert a["adopt"] is False and "tail coverage fails" in a["why"]


def test_an_unknown_window_is_refused_never_everything() -> None:
    with pytest.raises(ValueError, match="vault"):
        race.run(_calibrated(5), window="vault")  # the vault is registered elsewhere and never run here

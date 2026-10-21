"""The 2022-26 card replay of the horse race (#377, addendum A1), on synthetic F&O history (tests/fno_synth.py: a
made-up index priced by Black-76 at one flat volatility; settle == close)."""

import math
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

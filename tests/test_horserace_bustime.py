"""The business-time challenger p_rw_bus in the card replay (#384, horse-race addendum A2), on synthetic F&O history
(tests/fno_synth.py: every weekday a session)."""

from dataclasses import replace
from datetime import date, timedelta

import pytest
from fno_synth import history as synth

from finresearch.backtest.history import MemoryHistory
from finresearch.backtest.specs import SPECS
from finresearch.fincalc import bustime as bt
from finresearch.horserace import addendum_a2, prereg, replay

PCS = next(s for s in SPECS if s.key == "nifty_pcs_m")
START, END = date(2022, 4, 1), date(2022, 4, 30)


@pytest.fixture(scope="module")
def hist() -> MemoryHistory:
    return synth(date(2021, 12, 1), date(2022, 8, 31), vol=0.15, seed=11)


@pytest.fixture(scope="module")
def made(hist):
    return replay.make_cards(hist, START, END, [PCS])


def _weekdays(a: date, b: date, skip: set[date] = frozenset()) -> int:
    return sum(
        1 for i in range(1, (b - a).days + 1) if (d := a + timedelta(days=i)).weekday() < 5 and d not in skip
    )


def test_bus_sigmas_put_the_variance_on_the_business_clock() -> None:
    # sigma_eff^2 x t = sigma^2 x T_bus: 0.2 x sqrt(0.04 / 0.01) = 0.4
    assert replay.bus_sigmas({"p_rw20": 0.2, "p_rw60": None}, 0.04, 0.01) == {"p_rw20": pytest.approx(0.4),
                                                                               "p_rw60": None}  # fmt: skip
    assert replay.bus_sigmas({"p_rw20": 0.2, "p_rw60": 0.3}, None, 0.01) == {"p_rw20": None, "p_rw60": None}


def test_each_card_carries_t_bus_counted_by_hand(made) -> None:
    cards, _ = made
    for c in cards[:5]:
        days = (c.expiry - c.origin).days
        s = _weekdays(c.origin, c.expiry)
        assert c.t_bus == pytest.approx(
            (s + 0.10 * (days - s)) / 263.3
        )  # A2.2: w_o = 0, w_w = 0.10, Y = 263.3
        f = c.forecasts["p_rw_bus"]
        assert 0 < f.p < 1 and len(f.dots) == 20


def test_a_missing_stored_weekday_counts_as_a_holiday(hist, made) -> None:
    c = made[0][0]
    gone = c.origin + timedelta(days=7 if (c.origin + timedelta(days=7)).weekday() < 5 else 8)
    h2 = MemoryHistory([x for x in hist.days["NIFTY"].values() if x.day != gone])
    c2 = replay.make_cards(h2, c.origin, c.origin, [PCS])[0][0]
    days, s = (c.expiry - c.origin).days, _weekdays(c.origin, c.expiry, {gone})
    assert c2.t_bus == pytest.approx((s + 0.10 * (days - s)) / 263.3) and c2.t_bus < c.t_bus


def test_when_t_bus_equals_the_calendar_t_the_challenger_is_the_incumbent(hist, made, monkeypatch) -> None:
    def same(expiry, now, *a, **k):
        return bt.BusinessT((expiry - now.date()).days / 365)

    monkeypatch.setattr(replay.bt, "business_years", same)
    o = made[0][0].origin
    c = replay.make_cards(hist, o, o, [PCS])[0][0]
    assert c.forecasts["p_rw_bus"] == c.forecasts["p_rw"]


def test_an_unknown_calendar_makes_only_the_challenger_unknown(hist, made) -> None:
    c = made[0][3]
    cut = MemoryHistory(
        [x for x in hist.days["NIFTY"].values() if x.day <= c.origin]
    )  # no session up to expiry
    c2 = replay.make_cards(cut, START, c.origin, [PCS])[0][-1]
    assert c2.origin == c.origin and c2.t_bus is None and c2.forecasts["p_rw_bus"].p is None
    rest = {k: v for k, v in c2.forecasts.items() if k != "p_rw_bus"}
    assert rest == {k: v for k, v in c.forecasts.items() if k != "p_rw_bus"}  # no other forecast reads ahead


def test_run_window_registers_the_challenger_and_reports_the_calendar(hist) -> None:
    rep = replay.run_window(
        hist, "design", spec_list=[PCS], n_paths=50
    )  # few paths: p_path is tested elsewhere
    # addendum A3 (#385) appended p_rn_b76 after p_rw_bus, A4 (#386) p_rn_svi after it and A5 (#387) p_rw_emp and
    # p_rw_fhs after that,
    # and A6 (#388) p_path last
    assert rep["challengers"] == ["p_rw_bus", "p_rn_b76", "p_rn_svi", "p_rw_emp", "p_rw_fhs", "p_path"]
    assert "p_rw_bus" in rep["forecasters"]
    assert rep["adoption"]["p_rw_bus"]["NIFTY"]["adopt"] is False  # design never adopts
    assert rep["replay"]["addendum_a2"] == addendum_a2.ADDENDUM_VERSION
    assert rep["replay"]["addendum_a2_hash"] == prereg.ADDENDUM_A2_HASH
    assert rep["replay"]["calendar_gap_weekdays"] == {"NIFTY": 0}


def test_require_registered_checks_addendum_a2(monkeypatch) -> None:
    replay.require_registered()
    monkeypatch.setattr(prereg, "ADDENDUM_A2_HASH", "0" * 64)
    with pytest.raises(replay.race.PreregistrationError, match="addendum_a2"):
        replay.require_registered()
    monkeypatch.setattr(prereg, "ADDENDUM_A2_HASH", addendum_a2.addendum_hash())
    monkeypatch.setattr(bt, "PRIOR", replace(bt.PRIOR, nontrading=0.3))
    with pytest.raises(replay.race.PreregistrationError):
        replay.require_registered()

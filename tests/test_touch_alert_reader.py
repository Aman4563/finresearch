"""#520: the nightly touch alert reads the exact variant. Two short legs at 30 % volatility and 90 days sit in the
band where the variants disagree at the 0.60 level: the shortcut fired on the put and stayed silent on the call; the
exact probability does the opposite. Test database only; synthetic marks; expected values worked here by erfc."""

from __future__ import annotations

import math
from datetime import UTC, date, datetime
from decimal import Decimal as D

import pytest
from sqlalchemy import select, text

from finresearch.fincalc import risk_alerts as ra

DAY = date(2026, 11, 20)
NOW = datetime(2026, 11, 20, 16, 30, tzinfo=UTC)  # 22:00 IST
EXP = date(2027, 2, 18)
VOL, T = 0.30, 90 / 365
FWD = 24000 * math.exp(0.065 * T)  # the reader's forward: the close carried at the marks' 6.5 %  (24,387.76)


def N(x: float) -> float:
    return 0.5 * math.erfc(-x / math.sqrt(2.0))


def _d(k: float) -> tuple[float, float]:
    s = VOL * math.sqrt(T)
    d1 = (math.log(FWD / k) + s * s / 2) / s
    return d1, d1 - s


@pytest.fixture
def clean(env):
    from finresearch.db import session_scope

    def wipe():
        with session_scope() as s:
            s.execute(text("TRUNCATE fno_bhav, fno_mark, alert_eval_slot, alert_rule_state, alert, calendar_event, "
                           "calendar_coverage, broker_connection CASCADE"))  # fmt: skip

    wipe()
    yield env
    wipe()


def _marks(rows: list[dict]) -> None:
    from finresearch.db import session_scope
    from finresearch.db.models import AlertEvalSlot, FnoMark

    with session_scope() as s:
        for i, r in enumerate(rows):
            s.add(FnoMark(day=DAY, book="paper", leg_key=f"paper_leg:{i}", symbol="NIFTY", expiry=EXP,
                          right=r["right"], strike=D(str(r["strike"])), qty=-75, spot=D("24000"),
                          iv=D(str(r.get("iv", VOL))), t_years=D(str(round(r.get("t", T), 8)))))  # fmt: skip
        s.add(AlertEvalSlot(slot=f"fno_marks:{DAY}", result={"status": "done"}))


def _touch() -> dict:
    from finresearch.alerts.risk_readers import leg_readings
    from finresearch.db import session_scope

    with session_scope() as s:
        return {i: rd for r, i, rd in leg_readings(s, DAY, NOW) if r == "touch"}


def test_the_reader_gives_the_exact_probability_on_both_sides_and_names_the_formula(clean) -> None:
    _marks([{"right": "put", "strike": 22400}, {"right": "call", "strike": 26200}])
    got = _touch()
    d1, d2 = _d(22400.0)
    put = N(-d2) + FWD / 22400.0 * N(-d1)  # 0.5923; the shortcut 2 N(-d2) = 0.6197
    assert got["NIFTY 2027-02-18 22400PE"].value == pytest.approx(put, abs=1e-7)
    assert put < ra.TOUCH_LEVEL <= 2 * N(-d2)
    d1, d2 = _d(26200.0)
    call = N(d2) + FWD / 26200.0 * N(d1)  # 0.6077; the shortcut 2 N(d2) = 0.5785
    assert got["NIFTY 2027-02-18 26200CE"].value == pytest.approx(call, abs=1e-7)
    assert 2 * N(d2) < ra.TOUCH_LEVEL <= call
    for rd in got.values():
        assert ra.TOUCH_LABEL_EXACT in rd.detail and "2 x P_Q" not in rd.detail and "fno_mark" in rd.series


def test_the_short_call_the_shortcut_missed_now_fires_and_the_overstated_put_does_not(clean) -> None:
    from finresearch.alerts import risk
    from finresearch.db import session_scope
    from finresearch.db.models import Alert

    _marks([{"right": "put", "strike": 22400}, {"right": "call", "strike": 26200}])
    got = _touch()
    with session_scope() as s:
        res = {i: risk.apply(s, risk.RULES["touch"], i, rd, NOW) for i, rd in got.items()}
    assert res == {"NIFTY 2027-02-18 22400PE": "clear", "NIFTY 2027-02-18 26200CE": "fired"}
    with session_scope() as s:
        alerts = s.scalars(select(Alert)).all()
        assert len(alerts) == 1 and "26200CE" in alerts[0].message and "61% >= 60%" in alerts[0].message
        assert "exact for a driftless lognormal forward" in alerts[0].message


def test_a_leg_whose_inputs_are_not_positive_reads_cant_check_not_a_number(clean) -> None:
    """A stored IV of zero, or no time left, used to read as a 100 % touch probability (the shortcut's degenerate
    answer); it is unknown, and the alert says it cannot check."""
    from finresearch.alerts import risk
    from finresearch.db import session_scope

    _marks([{"right": "put", "strike": 22400, "iv": 0}, {"right": "call", "strike": 26200, "t": 0}])
    got = _touch()
    assert len(got) == 2
    for rd in got.values():
        assert rd.value is None and "no touch probability" in rd.detail
    with session_scope() as s:
        assert {risk.apply(s, risk.RULES["touch"], i, rd, NOW) for i, rd in got.items()} == {"unknown"}


def test_standing_state_carries_across_the_switch_without_a_second_firing(clean) -> None:
    """The rule's label and level are unchanged, so its signature is the one shipped with #398 and a leg that stood
    fired under the shortcut neither fires again nor is reset; it clears once the exact probability is below 0.60."""
    from finresearch.alerts import risk
    from finresearch.db import session_scope
    from finresearch.db.models import Alert

    rule = risk.RULES["touch"]
    assert risk._sig(rule) == "be42aa57cad4fe89" and rule.fire == 0.60  # the signature on main before #520
    leg = "NIFTY 2027-02-18 23000PE"
    with session_scope() as s:
        assert (
            risk.apply(s, rule, leg, risk.Reading(0.62, "fno_mark", "shortcut-era reading"), NOW) == "fired"
        )
        assert risk.apply(s, rule, leg, risk.Reading(0.61, "fno_mark", "exact reading"), NOW) == "still_fired"
        assert risk.apply(s, rule, leg, risk.Reading(0.59, "fno_mark", "exact reading"), NOW) == "clear"
    with session_scope() as s:
        assert len(s.scalars(select(Alert)).all()) == 1

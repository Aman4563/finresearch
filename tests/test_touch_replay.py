"""#520: the replay that compares the touch formula's two registered variants on stored marks (definitions in
fincalc/risk_alert_specs.REPLAY). Synthetic marks only; the counts are worked by hand from the four legs below."""

from __future__ import annotations

import json
import math
from datetime import date
from decimal import Decimal as D

import pytest
from sqlalchemy import func, select, text

from finresearch.fincalc import risk_alerts as ra

V1, V2 = "touch-reflection-v1", "touch-exact-v2"
VOL, T = 0.30, 90 / 365
FWD = 24000 * math.exp(0.065 * T)  # 24,387.76
EXP = date(2027, 2, 18)
D1, D2, D3, D4, D5 = (date(2026, 11, d) for d in (16, 17, 18, 19, 20))
# at 30 % and 90 days, by erfc (tests/test_touch_alert_reader.py): the shortcut / the exact probability
#   A 22400 put   0.6197 / 0.5923   at the level under variant 1 only
#   B 26200 call  0.5785 / 0.6077   at the level under variant 2 only
#   C 18000 put   far below 0.60 under both
#   E 24400 call  just above the forward: 0.94 / 1.00, at the level under both
LEGS = {"A": ("put", 22400.0), "B": ("call", 26200.0), "C": ("put", 18000.0), "E": ("call", 24400.0)}
PLAN = [(D1, "A", True), (D2, "B", True), (D2, "C", True), (D3, "A", True), (D3, "B", True), (D4, "A", False),
        (D5, "A", True), (D5, "E", True)]  # (session, leg, IV stored?)  # fmt: skip
WANT_LEG_DAYS = {
    "known": 7, "unknown": 1, "at_level": {V1: 4, V2: 3}, "both": 1, "neither": 1, "changed": 5,
    "only": {V1: {"below_forward": 3, "above_forward": 0}, V2: {"below_forward": 0, "above_forward": 2}},
}  # fmt: skip
# sessions: D1 A (variant 1 only); D2 B (variant 2 only); D3 both variants; D4 unknown only; D5 both
WANT_ALERT_DAYS = {
    "sessions": 5,
    "unknown_only": 1,
    "at_level": {V1: 3, V2: 3},
    "changed": 2,
    "only": {V1: 1, V2: 1},
}
# variant 1: A fires on D1, stands on D3, is unknown on D4 and fires again on D5; E fires on D5. Variant 2: B fires on
# D2 and stands on D3; E fires on D5
WANT_FIRINGS = {V1: 3, V2: 2}


def _n(x: float) -> float:
    return 0.5 * math.erfc(-x / math.sqrt(2.0))


def _gap(k: float) -> float:
    """|shortcut - exact| = (undiscounted option) / K, written out."""
    s = VOL * math.sqrt(T)
    d1 = (math.log(FWD / k) + s * s / 2) / s
    d2 = d1 - s
    return _n(-d2) - FWD / k * _n(-d1) if k < FWD else FWD / k * _n(d1) - _n(d2)


def test_the_replay_counts_leg_days_alert_days_and_firings_per_variant() -> None:
    marks = [ra.TouchMark(day, leg, FWD, LEGS[leg][1], VOL if known else None, T) for day, leg, known in PLAN]
    out = ra.touch_replay(marks[::-1])  # the order given does not matter: sessions are replayed in date order
    assert out["level"] == 0.60 and out["variants"] == [V1, V2]
    assert out["leg_days"] == WANT_LEG_DAYS
    assert out["alert_days"] == WANT_ALERT_DAYS
    assert out["firings"] == WANT_FIRINGS
    assert out["largest_gap"]["below_forward"] == pytest.approx(_gap(22400.0), abs=1e-9)  # A: 0.0274
    assert out["largest_gap"]["above_forward"] == pytest.approx(_gap(24400.0), abs=1e-9)  # E: 0.0594


def test_an_unknown_leg_day_is_counted_and_never_read_as_below_the_level() -> None:
    bad = [ra.TouchMark(D1, "A", None, None, None, None), ra.TouchMark(D1, "B", FWD, 26200.0, 0.0, T),
           ra.TouchMark(D2, "C", FWD, 18000.0, VOL, float("nan"))]  # fmt: skip
    out = ra.touch_replay(bad)
    assert (
        out["leg_days"]["unknown"] == 3 and out["leg_days"]["known"] == 0 and out["leg_days"]["neither"] == 0
    )
    assert out["alert_days"] == {"sessions": 2, "unknown_only": 2, "at_level": {V1: 0, V2: 0}, "changed": 0,
                                 "only": {V1: 0, V2: 0}}  # fmt: skip
    assert ra.touch_replay([])["alert_days"]["sessions"] == 0


@pytest.fixture
def clean(env):
    from finresearch.db import session_scope

    def wipe():
        with session_scope() as s:
            s.execute(text("TRUNCATE fno_mark, alert_rule_state, alert CASCADE"))

    wipe()
    yield env
    wipe()


def _store() -> None:
    from finresearch.db import session_scope
    from finresearch.db.models import FnoMark

    with session_scope() as s:
        for day, leg, known in PLAN:
            right, k = LEGS[leg]
            s.add(FnoMark(day=day, book="paper", leg_key=f"paper_leg:{leg}", symbol="NIFTY", expiry=EXP, right=right,
                          strike=D(str(k)), qty=-75, spot=D("24000"), iv=D(str(VOL)) if known else None,
                          t_years=D(str(round(T, 8)))))  # fmt: skip
        # never part of the comparison: a long option, a future, and a short leg outside the asked range
        s.add(FnoMark(day=D5, book="paper", leg_key="paper_leg:long", symbol="NIFTY", expiry=EXP, right="put",
                      strike=D("22400"), qty=75, spot=D("24000"), iv=D(str(VOL)), t_years=D(str(round(T, 8)))))  # fmt: skip
        s.add(FnoMark(day=D5, book="paper", leg_key="paper_leg:fut", symbol="NIFTY", expiry=EXP, right="future",
                      strike=None, qty=-75, spot=D("24000"), iv=None, t_years=D(str(round(T, 8)))))  # fmt: skip
        s.add(FnoMark(day=date(2026, 10, 30), book="paper", leg_key="paper_leg:A", symbol="NIFTY", expiry=EXP,
                      right="put", strike=D("22400"), qty=-75, spot=D("24000"), iv=D(str(VOL)),
                      t_years=D(str(round(T, 8)))))  # fmt: skip
        # Groww did not confirm the direction: an unknown leg-day, on its own session
        s.add(FnoMark(day=date(2026, 11, 23), book="real", leg_key="groww:X", symbol="NIFTY", expiry=EXP, right="call",
                      strike=D("26200"), qty=None, spot=D("24000"), iv=D(str(VOL)), t_years=D(str(round(T, 8)))))  # fmt: skip


def test_cli_risk_replay_touch_compares_the_variants_on_stored_marks_counts_only_read_only(clean) -> None:
    from typer.testing import CliRunner

    from finresearch.cli import app
    from finresearch.db import session_scope
    from finresearch.db.models import Alert, AlertRuleState, FnoMark
    from finresearch.fincalc import risk_alert_prereg as prereg

    _store()
    with session_scope() as s:
        before = s.scalar(select(func.count()).select_from(FnoMark))
    r = CliRunner().invoke(app, ["fno", "risk-replay", "--touch", "--from", "2026-11-01", "--to", "2026-11-30",
                                 "--json"])  # fmt: skip
    assert r.exit_code == 0, r.output
    out = json.loads(r.output)
    assert out["registration"] == prereg.HASH and out["variants"] == [V1, V2] and "fno_mark" in out["series"]
    assert (
        out["from"] == "2026-11-01" and out["first_mark"] == "2026-11-16" and out["last_mark"] == "2026-11-23"
    )
    assert out["leg_days"] == {**WANT_LEG_DAYS, "unknown": 2}  # + the unconfirmed Groww leg
    assert out["alert_days"] == {**WANT_ALERT_DAYS, "sessions": 6, "unknown_only": 2}
    assert out["firings"] == WANT_FIRINGS
    assert "22400" not in r.output and "26200" not in r.output and "NIFTY" not in r.output  # counts, no leg
    plain = CliRunner().invoke(
        app, ["fno", "risk-replay", "--touch", "--from", "2026-11-01", "--to", "2026-11-30"]
    )
    assert (
        plain.exit_code == 0 and "alert-days that change: 2" in plain.output and "22400" not in plain.output
    )
    with session_scope() as s:
        assert s.scalar(select(func.count()).select_from(FnoMark)) == before  # a replay writes nothing
        assert not s.scalars(select(AlertRuleState)).first() and not s.scalars(select(Alert)).first()


def test_cli_risk_replay_touch_is_refused_when_the_registration_moved(clean, monkeypatch) -> None:
    from typer.testing import CliRunner

    from finresearch.cli import app
    from finresearch.fincalc import risk_alert_prereg as prereg

    _store()
    monkeypatch.setattr(prereg, "HASH", "0" * 64)
    r = CliRunner().invoke(app, ["fno", "risk-replay", "--touch", "--json"])
    assert r.exit_code != 0 and "refused" in r.output and "leg_days" not in r.output

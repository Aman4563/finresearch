"""#354 (P0.4): displayed numbers and labels that were wrong by construction. One regression test per wrong value,
each failing on the code before the fix. Expected values are worked out by hand or from independent arithmetic in the
test; the OI reconciliation uses NSE's published totals for 13-Oct and 19-Oct-2026 (public aggregate data)."""

from __future__ import annotations

import math
from datetime import date, timedelta

import pytest
from sqlalchemy import text

from finresearch.fincalc import fno_history as fh
from finresearch.fincalc import market_context as mc
from finresearch.fincalc import volatility as vol


@pytest.fixture
def clean(env):
    from finresearch.db import session_scope

    def wipe():
        with session_scope() as s:
            s.execute(text("TRUNCATE fno_bhav, fno_bhav_day, fno_iv_eod, fno_contract, fno_participant_oi, "
                           "iv_history"))  # fmt: skip

    wipe()
    yield env
    wipe()


# ----------------------------------------------------------------------------------------------- (c) term structure
def test_term_shape_is_neutral_and_never_reads_stress(clean):
    from finresearch.db import session_scope
    from finresearch.db.models import FnoIvEod
    from finresearch.signals import fno_context as fc

    with session_scope() as s:
        s.add(FnoIvEod(symbol="NIFTY", day=date(2026, 10, 20), near_expiry=date(2026, 10, 27), near_iv=14.0,
                       next_expiry=date(2026, 11, 3), next_iv=12.5, rate=0.065))  # fmt: skip
        s.flush()
        got = fc.term_block(s, "NIFTY")
    assert got["slope"] == pytest.approx(-1.5)  # next − near, vol points
    assert got["shape"] == "near above next"
    text_out = " ".join([got["shape"], got["evidence"]["measures"], got["evidence"]["evidence"]]).lower()
    assert "stress" not in text_out and "inverted" not in text_out


# --------------------------------------------------------------------------------------- (i) participant title date
def test_participant_title_without_space_before_year_is_read():
    from pathlib import Path

    from finresearch.adapters import nse_participant_oi as po

    raw = (Path(__file__).parent / "fixtures" / "nse" / "fno" / "fao_participant_oi_09102026.csv").read_text()
    old = raw.replace("as on Oct 09, 2026", "as on Jan 03,2022")  # NSE's 2022 files have no space before the year
    assert po.parse(old).day == date(2022, 1, 3)


# ----------------------------------------------------------------------------------- (i) long-dated contract kind
def test_options_only_expiry_far_away_is_long_dated_not_weekly(clean):
    from finresearch.db import session_scope
    from finresearch.db.models import FnoBhav
    from finresearch.fnohist import derive

    d = date(2026, 10, 1)
    with session_scope() as s:
        for exp, kind in ((date(2026, 10, 6), "IDO"), (date(2026, 10, 27), "IDO"), (date(2026, 10, 27), "IDF"),
                          (date(2027, 3, 30), "IDO")):  # fmt: skip
            s.add(FnoBhav(day=d, symbol="NIFTY", instrument=kind, expiry=exp, strike=0 if kind == "IDF" else 25000,
                          option_type="XX" if kind == "IDF" else "CE", oi=650, lot_size=65))  # fmt: skip
        s.flush()
        derive.rebuild_contracts(s)
        got = {c.expiry: c.kind for c in derive.contracts(s, "NIFTY")}
    # 6-Oct: 5 days after first seen, options only → weekly; 27-Oct has a future → monthly; 30-Mar-2027 is 180 days
    # after first seen with no future → a long-dated (quarterly / half-yearly) option, not a weekly
    assert got == {date(2026, 10, 6): "weekly", date(2026, 10, 27): "monthly", date(2027, 3, 30): "long-dated"}


# --------------------------------------------------------------------------- (j) corporate-action futures quarantine
def test_lot_change_jump_is_quarantined_from_realised_vol():
    e = date(2026, 10, 27)
    days = [date(2026, 9, 1) + timedelta(days=i) for i in range(23)]
    px = [1000.0 * (1.01 if i % 2 else 1.0) for i in range(23)]
    px[12:] = [p / 2 for p in px[12:]]  # a 1:1 bonus: NSE halves the open future's price and doubles the lot
    lots = {(d, e): (500 if i < 12 else 1000) for i, d in enumerate(days)}
    futs = [(d, e, p) for d, p in zip(days, px, strict=True)]
    quarantined: list = []
    rets = fh.front_future_returns(futs, lots=lots, quarantined=quarantined)
    # the bonus day's return ln(0.5·1.0 / 1.01) is not a market move: excluded and reported
    assert [q[0] for q in quarantined] == [days[12]]
    assert quarantined[0][2] == pytest.approx(math.log(0.5 / 1.01))
    assert days[12] not in [d for d, _ in rets] and len(rets) == 21
    clean_rv = fh.realised_vol_pct([r for _, r in rets])
    # the 20 remaining returns alternate ±ln(1.01) except where the dropped day joins two flat/up days: < 25 %
    assert clean_rv is not None and clean_rv < 25
    # without the lots the jump goes through and RV20 is far above 100 % (the bug)
    assert fh.realised_vol_pct([r for _, r in fh.front_future_returns(futs)]) > 100
    # a lot change with a small price move (a contract revision without a price adjustment) is kept
    small = [(d, e, 1000.0 + i) for i, d in enumerate(days)]
    assert len(fh.front_future_returns(small, lots=lots)) == 22


# ------------------------------------------------------------------------------------ (b) IV rank over 252 sessions
def _sessions(n: int, start: date = date(2022, 1, 3)) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def test_iv_rank_needs_80_percent_of_252_sessions():
    days = _sessions(400)
    # FINNIFTY-shaped: an IV on only 1 session in 5 (the last one among them) → 51 of the last 252 sessions (20 %)
    sparse = [(d, 15.0 + (i % 7) if (len(days) - 1 - i) % 5 == 0 else None) for i, d in enumerate(days)]
    st = vol.iv_stats_sessions(sparse)
    assert st.rank is None and st.percentile is None
    assert st.status.startswith("insufficient coverage") and "of 252 sessions" in st.status
    # the old ranking took the last 252 non-null values: about 5 years of sessions, and called it ok
    assert vol.iv_stats([v for _, v in sparse if v is not None]).status == "ok"
    # dense: 210 of 252 (83 %) → computed over those 210 values only
    dense = [(d, float(i % 50) if (len(days) - 1 - i) % 6 else 7.5) for i, d in enumerate(days)]
    dense = [(d, None if (len(days) - 1 - i) % 6 == 5 else v) for i, (d, v) in enumerate(dense)]
    window = [v for _, v in dense[-252:] if v is not None]
    cur = dense[-1][1]
    st = vol.iv_stats_sessions(dense)
    assert st.n == len(window) == 210 and st.status == "ok"
    assert st.rank == pytest.approx((cur - min(window)) / (max(window) - min(window)))
    assert st.percentile == pytest.approx(sum(x < cur for x in window[:-1]) / (len(window) - 1))


def test_iv_rank_is_unknown_when_the_last_session_has_no_iv():
    days = _sessions(260)
    pts = [(d, 15.0 + i % 9) for i, d in enumerate(days)]
    pts[-1] = (days[-1], None)
    st = vol.iv_stats_sessions(pts)
    assert st.rank is None and st.current is None and "no IV" in st.status

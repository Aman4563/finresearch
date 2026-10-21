"""#354 (P0.4): displayed numbers and labels that were wrong by construction. One regression test per wrong value,
each failing on the code before the fix. Expected values are worked out by hand or from independent arithmetic in the
test; the OI reconciliation uses NSE's published totals for 13-Oct and 19-Oct-2026 (public aggregate data)."""

from __future__ import annotations

import math
from datetime import date, timedelta

import pytest
import respx
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
    # NSE's 2022 files have no space before the year
    old = raw.replace("as on Oct 09, 2026", "as on Jan 03,2022")
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
    assert got == {
        date(2026, 10, 6): "weekly",
        date(2026, 10, 27): "monthly",
        date(2027, 3, 30): "long-dated",
    }


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


def _eod(symbol: str, days: list[date], iv30, near, method=lambda i: "interpolated", rv20=None) -> None:
    from finresearch.db import session_scope
    from finresearch.db.models import FnoIvEod

    with session_scope() as s:
        for i, d in enumerate(days):
            v = iv30(i)
            s.add(FnoIvEod(symbol=symbol, day=d, iv_30d=v, iv_30d_method=None if v is None else method(i),
                           near_iv=near(i), next_iv=None, rv_20=None if rv20 is None else rv20(i), rate=0.065))  # fmt: skip


def test_sparse_30d_iv_falls_back_to_the_labelled_near_iv(clean):
    from finresearch.signals import fno_context as fc

    days = _sessions(300)
    last = len(days) - 1
    # FINNIFTY-shaped: the 30-day IV on 1 session in 5, the near-expiry ATM IV on every session
    _eod("FINNIFTY", days, lambda i: 15.0 + i % 7 if (last - i) % 5 == 0 else None, lambda i: 12.0 + i % 11)
    st, info = fc.iv_rank("FINNIFTY")
    near = [12.0 + i % 11 for i in range(len(days))][-252:]
    assert info["series"] == "near_iv" and "near-expiry" in info["label"] and "fallback" in info["label"]
    assert info["as_of"] == days[-1].isoformat() and "close" in info["label"]
    assert st.status == "ok" and st.n == 252
    assert st.rank == pytest.approx((near[-1] - min(near)) / (max(near) - min(near)))
    # with no near IV either: insufficient coverage, never a rank from a five-year "year"
    _eod("MIDCPNIFTY", days, lambda i: 15.0 + i % 7 if (last - i) % 5 == 0 else None, lambda i: None)
    st, info = fc.iv_rank("MIDCPNIFTY")
    assert st.rank is None and st.status.startswith("insufficient coverage") and info["series"] == "iv_30d"


def test_flat_30d_iv_rows_count_as_missing(clean):
    from finresearch.signals import fno_context as fc

    days = _sessions(260)
    # every third 30-day IV is a "flat" stand-in (the longest expiry, up to 7 days short of 30): 67 % coverage
    _eod("NIFTY", days, lambda i: 15.0 + i % 9, lambda i: None,
         method=lambda i: "flat" if (len(days) - 1 - i) % 3 == 1 else "interpolated")  # fmt: skip
    st, info = fc.iv_rank("NIFTY")
    assert info["series"] == "iv_30d" and st.rank is None and "insufficient coverage" in st.status


# ---------------------------------------------------------------------------- (d) IV − RV on matched horizons
def test_iv_minus_rv_uses_the_30_day_iv_not_the_live_weekly_atm_iv(clean, monkeypatch):
    from decimal import Decimal

    from finresearch.adapters.nse_fno import ChainRow, OptionChain, OptionQuote
    from finresearch.db import session_scope
    from finresearch.signals import fno_context as fc

    _eod("NIFTY", [date(2026, 10, 20)], lambda i: 13.0, lambda i: 18.0, rv20=lambda i: 10.5)
    q = OptionQuote(
        last_price=Decimal(100), iv=Decimal(22), oi=None, change_in_oi=None, volume=None, bid=None, ask=None
    )
    chain = OptionChain(symbol="NIFTY", expiry=date(2026, 10, 27), underlying=Decimal(25000), as_of=None,
                        rows=[ChainRow(strike=Decimal(25000), call=q, put=q)])  # fmt: skip
    with session_scope() as s:
        term = fc.term_block(s, "NIFTY")
    closes = [25000.0 * (1.01 if i % 2 else 1.0) for i in range(70)]
    v = fc.vol_block(chain, closes, date(2026, 10, 21), 0.065, term=term)
    # IV30 13.0 − RV20 10.5 at the 20-Oct close: 2.5 points. The live weekly ATM IV (22 %) against the chain-free RV
    # gave a different, mismatched number before
    assert v["iv_minus_rv20"] == pytest.approx(2.5)
    assert "30-day IV" in v["iv_minus_rv20_label"] and "2026-10-20" in v["iv_minus_rv20_label"]
    assert v["atm_iv"] == pytest.approx(22.0)


# ------------------------------------------------------------------------------- (f) alert readings name the series
def test_alert_iv_readings_name_the_series_they_read(clean):
    import asyncio

    from finresearch.alerts import compute

    days = _sessions(260)
    _eod("NIFTY", days, lambda i: 15.0 + i % 9, lambda i: 14.0)
    r = compute.Reader(None)
    atm = asyncio.run(r.read("fno", "atm_iv", "NIFTY", {}, {}))
    pct = asyncio.run(r.read("fno", "iv_percentile", "NIFTY", {}, {}))
    for got in (atm, pct):
        assert "iv_history" not in got.source and "bhavcopy 30-day IV" in got.source, got.source
    assert atm.value is not None and pct.value is not None


# ------------------------------------------------------------------------------- (g) roll-adjusted ADX on /fno
def test_registered_adx_is_unchanged_by_the_shared_roll_adjustment():
    """The direction model's ADX feature is registered (#327): extracting its roll adjustment for the context panel must
    not move it. Values recorded from the code before #354 on a seeded synthetic panel with 15 rolls."""
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).parent))
    from direction_synth import panel

    from finresearch.direction.features import _adx

    rows = panel(date(2022, 1, 3), date(2023, 3, 31), seed=3)
    assert _adx(rows) == 16.98271388224083
    assert _adx(rows[:150]) == 23.374853456011518
    assert _adx(rows[:60]) == 9.3800705008798


def test_context_adx_is_roll_adjusted_like_the_direction_model(clean):
    from finresearch.db import session_scope
    from finresearch.db.models import FnoBhav
    from finresearch.signals import fno_context as fc

    days = _sessions(90, date(2026, 6, 1))
    e1, e2 = days[44], days[-1] + timedelta(days=30)
    a = [24000 + 40 * i + (120 if i % 3 == 0 else -60) for i in range(len(days))]  # a zig-zag uptrend
    with session_scope() as s:
        for i, d in enumerate(days):
            for exp, f in ((e1, 1.0), (e2, 1.02)):  # the next contract trades 2 % above the near one (carry)
                if d > exp:
                    continue
                s.add(FnoBhav(day=d, symbol="NIFTY", instrument="IDF", expiry=exp, strike=0, option_type="XX",
                              high=(a[i] + 50) * f, low=(a[i] - 50) * f, close=a[i] * f, settle=a[i] * f, oi=1000))  # fmt: skip
        s.flush()
        bars = fc.front_futures(s, "NIFTY", days[-1])
    got = fc.trend_block([], float(a[-1]), bars, "test")["adx"]
    # ratio-adjusted at the roll (factor = near close / next close on the day before the roll = 1/1.02), the series is
    # the near contract's path throughout: ADX equals ADX on that path, with no 2 % jump at the roll
    # (the first session has no previous one, so the front-future rule starts on the second)
    want = mc.adx([x + 50 for x in a[1:]], [x - 50 for x in a[1:]], [float(x) for x in a[1:]])
    assert got["adx"] == pytest.approx(round(want["adx"], 2), abs=0.011)
    assert "ratio-adjusted" in got["on"]


def test_iv_api_ranks_over_sessions_and_labels_the_input(clean):
    from fastapi.testclient import TestClient

    from finresearch.api import create_app

    days = _sessions(71)
    _eod("NIFTY", days, lambda i: 15.0 + i % 10, lambda i: 14.0, rv20=lambda i: 11.0)
    with TestClient(create_app()) as c:
        short = c.get("/api/fno/NIFTY/iv").json()
        # 71 sessions are 28 % of a 252-session year: no rank, said so (before #354: "ok" over 71 days)
        assert short["rank"] is None and short["status"].startswith("insufficient coverage")
        _eod("NIFTY", _sessions(200, days[-1] + timedelta(days=1)), lambda i: 15.0 + i % 10, lambda i: 14.0,
             rv20=lambda i: 11.0)  # fmt: skip
        full = c.get("/api/fno/NIFTY/iv").json()
    assert full["status"] == "ok" and full["n"] == 252 and full["rank"] is not None
    assert (
        full["rank_input"].startswith("NSE F&O bhavcopy 30-day IV at the") and "close" in full["rank_input"]
    )
    assert full["rank_series"] == "iv_30d"


def test_iv_rank_factor_says_it_ranks_a_previous_close(env):
    import json
    from pathlib import Path

    from finresearch.adapters.nse_fno import parse_chain, parse_index_history
    from finresearch.signals import fno as fs

    fno = Path(__file__).parent / "fixtures" / "nse" / "fno"
    ch = parse_chain(
        "NIFTY", date(2026, 10, 6), json.loads((fno / "option_chain_NIFTY_20261006_trimmed.json").read_text())
    )
    closes = [float(c) for _, c in parse_index_history(json.loads(
        (fno / "indices_history_NIFTY50_202606_202609.json").read_text()))]  # fmt: skip
    a = fs.analyse(ch, 65, fs.preset_legs(ch, "iron_condor"), today=date(2026, 9, 29), closes=closes,
                   iv_series=[*([20.0] * 70), 13.36], capital=1_000_000, max_loss_pct=2, brokerage=20,
                   iv_source={"source": "bhavcopy"}, iv_as_of=date(2026, 9, 28))  # fmt: skip
    f = next(x for x in fs._factor_info(a) if x.name == "IV rank")
    # it ranks the bhavcopy 30-day IV of the 28-Sep close, not "today's ATM IV" (#354 b)
    assert (
        "today's ATM IV" not in f.explanation
        and "30-day IV" in f.explanation
        and "2026-09-28" in f.explanation
    )


# ----------------------------------------------------------------- (h) the hash checks the spec, not the engine code
def test_registration_says_matches_spec_not_matches_code():
    from finresearch.api import fno_backtests, fno_direction

    for reg in (fno_direction.registration(), fno_backtests.registration()):
        assert reg["matches_spec"] is True
        # the hash covers the registered spec's parameters only, not the engine that runs them (roadmap P2.4)
        assert "spec parameters only" in reg["hash_covers"] and "engine" in reg["hash_covers"]


# --------------------------------------------------- (i) participant files follow the bhavcopy manifest's sessions
@respx.mock
async def test_participant_update_follows_the_bhavcopy_manifest(clean):
    import httpx

    from finresearch.adapters.http import PoliteClient
    from finresearch.db import session_scope
    from finresearch.db.models import FnoBhavDay
    from finresearch.fnohist import participants

    with session_scope() as s:
        s.add(FnoBhavDay(day=date(2026, 10, 3), status="loaded"))  # a Saturday special session
        s.add(FnoBhavDay(day=date(2026, 10, 6), status="no_file"))  # a weekday holiday: no bhavcopy, no file
    asked: list[str] = []

    def answer(request):
        asked.append(request.url.path.rsplit("_", 1)[-1][:8])
        return httpx.Response(404)

    respx.get(url__startswith="https://nsearchives.nseindia.com/").mock(side_effect=answer)

    async def no_sleep(_s: float) -> None:
        return None

    async with PoliteClient(cache_dir=None, sleep=no_sleep, clock=lambda: 0.0, max_retries=0) as http:
        res = await participants.update(date(2026, 10, 9), http=http, lookback=6)
    # 3-Oct (traded, a Saturday) is fetched; 6-Oct (the manifest's holiday) and the Sunday are not
    assert sorted(asked) == ["03102026", "05102026", "07102026", "08102026", "09102026"]
    assert res["planned"] == 5


# ------------------------------------------------------------------------- (a) open interest excludes expiring OI
# NSE's participant-wise OI (public) on the two observed expiry days: index option longs by right, index future longs
PARTICIPANT_TOTALS = {date(2026, 10, 13): {"CE": 3_743_159, "PE": 4_252_513, "FUT": 431_001},
                      date(2026, 10, 19): {"CE": 4_029_420, "PE": 3_744_636, "FUT": 413_750}}  # fmt: skip
# the stored bhavcopy's index option and future contracts on those days (every index underlying, folded into one row
# per right and bucket; the expiring NIFTY weekly expired that day): (live, expiring) contracts
STORED = {date(2026, 10, 13): {"CE": (3_742_988, 2_452_276), "PE": (4_252_427, 3_436_969), "FUT": (431_001, 0)},
          date(2026, 10, 19): {"CE": (4_029_145, 3_280_597), "PE": (3_744_534, 2_611_793), "FUT": (413_750, 0)}}  # fmt: skip


def test_oi_aggregates_exclude_contracts_expiring_that_day(clean):
    from finresearch.db import session_scope
    from finresearch.db.models import FnoBhav
    from finresearch.direction import data

    lot = 65
    with session_scope() as s:
        for d, by in STORED.items():
            for right, (live, expiring) in by.items():
                fut = right == "FUT"
                for exp, n in ((d + timedelta(days=7), live), (d, expiring)):
                    if n:
                        s.add(FnoBhav(day=d, symbol="NIFTY", instrument="IDF" if fut else "IDO", expiry=exp,
                                      strike=0 if fut else 25000, option_type="XX" if fut else right, oi=n * lot,
                                      lot_size=lot, settle=100))  # fmt: skip
    rows, notes = data.load("NIFTY", url=None)
    for r in rows:
        want = PARTICIPANT_TOTALS[r.day]
        for got, key in ((r.call_oi_live, "CE"), (r.put_oi_live, "PE"), (r.fut_oi_live, "FUT")):
            assert abs(got / lot - want[key]) / want[key] < 1e-4, (
                r.day,
                key,
            )  # within 0.01 % of NSE's totals
        assert (
            r.call_oi_expiring == STORED[r.day]["CE"][1] * lot
            and r.put_oi_expiring == STORED[r.day]["PE"][1] * lot
        )
        # the registered features' all-contract sums overshoot NSE's totals by 65-81 % on these days (kept, disclosed)
        assert (r.call_oi + r.put_oi) / lot / (want["CE"] + want["PE"]) > 1.6
    assert "expiring" in notes["oi_note"]

"""The options panel's per-day computation (P1.12, #374; evals/experiments/stock_option_vrp/PREREG.md §2), pure: a
synthetic bhavcopy priced at known flat volatilities goes in, and the expected IVs, straddle, moneyness buckets,
liquidity, kept rows and OI totals are recomputed independently here."""

from __future__ import annotations

import math
from datetime import date

import pytest
from optpanel_synth import csv_text, underlying

from finresearch.adapters import nse_fno_bhav as fb
from finresearch.fincalc.fno_history import RATE
from finresearch.optpanel import panel, params

DAY = date(2026, 3, 2)
EXPS = [date(2026, 3, 31), date(2026, 4, 28), date(2026, 5, 26)]


def rows() -> list[fb.FoRow]:
    lines = (underlying(DAY, "ZZALPHA", 1000.0, 0.30, EXPS, lot=100)
             + underlying(DAY, "ZZBETA", 250.0, 0.45, EXPS, lot=400)
             + underlying(DAY, "ZZGAMMA", 500.0, 0.35, EXPS, lot=200, atm_contracts=5)  # never a market settle
             + underlying(DAY, "NIFTY", 23000.0, 0.12, EXPS, kind="IDO", lot=65)  # stored by fno_bhav: not panel
             + underlying(DAY, "NIFTYNXT50", 70000.0, 0.16, EXPS, kind="IDO", lot=25))  # fmt: skip
    return fb.parse_udiff(csv_text(lines))[0]


@pytest.fixture(scope="module")
def got() -> panel.DayPanel:
    return panel.compute_day(DAY, rows())


def fwd(spot: float, e: date) -> float:
    return spot * math.exp(RATE * (e - DAY).days / 365)


def test_universe_is_every_stock_option_plus_niftynxt50_only(got) -> None:
    assert set(got.rows) == {"ZZALPHA", "ZZBETA", "ZZGAMMA", "NIFTYNXT50"}
    assert panel.keep("IDO", "NIFTY") is False and panel.keep("IDO", "NIFTYNXT50") is True
    assert panel.keep("STO", "ANYSTOCK") is True and panel.keep("STF", "ANYSTOCK") is True


def test_ivs_recover_the_pricing_volatility(got) -> None:
    for sym, vol in (("ZZALPHA", 30.0), ("ZZBETA", 45.0), ("NIFTYNXT50", 16.0)):
        r = got.rows[sym]
        assert r["near_expiry"] == EXPS[0] and r["next_expiry"] == EXPS[1]
        for k in ("near_iv", "next_iv", "iv_30d"):
            assert r[k] == pytest.approx(vol, abs=0.3), (sym, k)
        assert abs(r["skew_25d"]) < 0.5


def test_untraded_options_leave_ivs_unknown_never_zero(got) -> None:
    r = got.rows["ZZGAMMA"]
    assert r["near_iv"] is None and r["iv_30d"] is None and r["straddle_strike"] is None
    assert r["straddle_call"] is None and r["straddle_put"] is None
    assert got.counts["no_iv"] == 1
    assert r["liq_contracts"] > 0  # the market data that exists is still recorded


def test_straddle_is_the_near_atm_call_plus_put(got) -> None:
    r = got.rows["ZZALPHA"]
    f = fwd(1000.0, EXPS[0])
    strikes = [round(1000.0 * (1 + i * 0.02), 2) for i in range(-15, 16)]
    k = min(strikes, key=lambda x: abs(x - f))
    assert float(r["straddle_strike"]) == k and r["straddle_expiry"] == EXPS[0]
    by = {
        (x.right): x for x in rows() if x.symbol == "ZZALPHA" and x.expiry == EXPS[0] and float(x.strike) == k
    }
    assert r["straddle_call"] == by["CE"].settle and r["straddle_put"] == by["PE"].settle
    assert r["straddle_call_contracts"] == by["CE"].contracts


def test_buckets_and_liquidity_match_an_independent_count(got) -> None:
    edges = params.BUCKET_EDGES
    want: dict[str, dict[str, dict[str, int]]] = {"call": {}, "put": {}}
    liq = 0
    for x in rows():
        if x.symbol != "ZZBETA" or x.right == "XX":
            continue
        m = math.log(float(x.strike) / fwd(250.0, x.expiry))
        label = panel.bucket_label(sum(m >= e for e in edges))
        b = want["call" if x.right == "CE" else "put"].setdefault(label, {"oi": 0, "contracts": 0})
        b["oi"] += x.oi
        b["contracts"] += x.contracts
        if abs(float(x.strike) / fwd(250.0, x.expiry) - 1) <= params.LIQ_BAND:
            liq += x.contracts
    assert got.rows["ZZBETA"]["buckets"] == want
    assert got.rows["ZZBETA"]["liq_contracts"] == liq
    assert len({panel.bucket_label(i) for i in range(len(edges) + 1)}) == len(edges) + 1


def test_kept_rows_are_the_two_nearest_expiries_within_the_band(got) -> None:
    kept = [x for x in got.kept if x.symbol == "ZZALPHA"]
    want = [x for x in rows() if x.symbol == "ZZALPHA" and x.right != "XX" and x.expiry in EXPS[:2]
            and abs(math.log(float(x.strike) / fwd(1000.0, x.expiry))) <= params.ROWS_LN_BAND]  # fmt: skip
    assert sorted((x.expiry, x.strike, x.right) for x in kept) == sorted(
        (x.expiry, x.strike, x.right) for x in want
    )
    assert not any(x.symbol == "NIFTY" for x in got.kept)


def test_day_totals_count_stock_option_oi_in_contracts_excluding_expiring(got) -> None:
    calls = sum(
        x.oi // x.lot_size for x in rows() if x.instrument == "STO" and x.right == "CE" and x.expiry > DAY
    )
    puts = sum(
        x.oi // x.lot_size for x in rows() if x.instrument == "STO" and x.right == "PE" and x.expiry > DAY
    )
    assert got.totals == {"sto_call_oi_contracts": calls, "sto_put_oi_contracts": puts}


def test_totals_are_unknown_when_a_lot_size_is_missing() -> None:
    rs = rows()
    for x in rs:
        x.lot_size = None  # the legacy file (before Jul-2024) has no lot size
    assert panel.compute_day(DAY, rs).totals == {"sto_call_oi_contracts": None, "sto_put_oi_contracts": None}

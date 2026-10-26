"""Feature drift (#378, skill-monitor-v2): PSI on the reference window's decile bins. Every expected value is worked by
hand from the registered rule (specs_v2.Drift); unknown or too few values read "insufficient data", never ok."""

from __future__ import annotations

import math
from datetime import date, timedelta

import direction_synth as ds
import pytest

from finresearch.skillmon import drift
from finresearch.skillmon import stats as st

REF_300 = [float(i) for i in range(1, 301)]  # deciles 30.9, 60.8, ..., 270.1: exactly 30 values per bin


def test_decile_edges_are_the_reference_percentiles_by_linear_interpolation() -> None:
    edges = st.decile_edges([float(i) for i in range(1, 101)])  # q(k/10) = 1 + 99 k/10
    assert edges == pytest.approx([10.9, 20.8, 30.7, 40.6, 50.5, 60.4, 70.3, 80.2, 90.1])
    # ties merge: q10..q70 = 0, q80 = 0.2 (between the 200th and 201st values), q90 = 1
    assert st.decile_edges([0.0] * 200 + [1.0] * 50) == pytest.approx([0.0, 0.2, 1.0])


def test_bins_are_right_closed() -> None:
    # an edge value falls in the lower bin: (-inf, 1] holds 0.5 and 1.0, (1, 2] holds 1.5 and 2.0
    assert st.bin_counts([0.5, 1.0, 1.5, 2.0, 2.5], [1.0, 2.0]) == [2, 2, 1]
    assert st.bin_counts(REF_300, st.decile_edges(REF_300)) == [30] * 10


def test_psi_when_every_live_value_lies_above_the_reference_top_decile() -> None:
    live = [271.0 + i % 30 for i in range(250)]  # all in (270.1, +inf)
    r = drift.input_psi(REF_300, live)
    # nine bins at the 1e-4 floor against 0.1, the top bin 1.0 against 0.1
    assert r["psi"] == pytest.approx(9 * (1e-4 - 0.1) * -6.907755279 + 0.9 * 2.302585093, rel=1e-9)
    assert r["status"] == "act" and r["n_ref"] == 300 and r["n_live"] == 250 and r["bins"] == 10


def test_a_flag_falls_in_two_bins_and_its_shift_is_scored() -> None:
    ref = [0.0] * 200 + [1.0] * 50  # 20 % ones; edges 0, 0.2, 1
    r = drift.input_psi(ref, [0.0] * 125 + [1.0] * 125)  # 50 % ones
    # 0.4159: (0.5 - 0.8) ln(0.5 / 0.8) + (0.5 - 0.2) ln(0.5 / 0.2); the two empty bins add 0
    assert r["psi"] == pytest.approx((0.5 - 0.8) * -0.470003629 + (0.5 - 0.2) * 0.916290732, rel=1e-8)
    assert r["status"] == "act" and r["bins"] == 4


def test_the_live_sample_is_the_last_250_known_values() -> None:
    # 40 fives, an unknown and a 1.0 come before the last 250 known values (51..300), so they are not used
    r = drift.input_psi(REF_300, [5.0] * 40 + [None, 1.0] + [float(i) for i in range(51, 301)])
    assert r["n_live"] == 250 and r["status"] in ("ok", "watch", "act")
    same = drift.input_psi(REF_300, [float(i) for i in range(51, 301)])
    assert r["psi"] == pytest.approx(same["psi"])


@pytest.mark.parametrize(
    ("psi", "status"), [(0.0999, "ok"), (0.10, "watch"), (0.2499, "watch"), (0.25, "act")]
)
def test_the_registered_levels_are_inclusive(psi: float, status: str) -> None:
    assert drift.status_of(psi) == status


def test_too_few_known_values_is_insufficient_data_never_ok() -> None:
    few_ref = drift.input_psi([*REF_300[:249], None], [1.0] * 250)  # 249 known reference values
    assert few_ref["status"] == "insufficient data" and few_ref["psi"] is None and "249" in few_ref["why"]
    few_live = drift.input_psi(REF_300, [1.0] * 249 + [None] * 5)
    assert few_live["status"] == "insufficient data" and few_live["psi"] is None and few_live["n_live"] == 249


# --------------------------------------------------------------------------- both models on a stored-style panel
def _panel(live_iv: float = 15.0, live_end: date = date(2025, 10, 31)) -> list:
    """Made-up sessions: 2022-01-03 to 2024-06-28 at IV 15 (inside both reference windows), then a live stretch from
    2024-07-01 at `live_iv` (realised volatility scales with it in direction_synth)."""
    return ds.panel(date(2022, 1, 3), date(2024, 6, 28), seed=3) + ds.panel(
        date(2024, 7, 1), live_end, seed=4, iv=live_iv
    )


def _group(rep: dict, model: str, sym: str = "NIFTY") -> dict:
    return next(g for g in rep["groups"] if g["model"] == model and g["underlying"] == sym)


def test_the_card_inputs_by_hand() -> None:
    from finresearch.direction.features import DayRow, build_features

    rows = [
        DayRow(date(2023, 1, 2) + timedelta(days=i), ret=0.01 * (-1) ** i, iv_30=10.0 + i % 2)
        for i in range(260)
    ]
    s = drift.card_series(rows, build_features(rows))
    # session 251 (IV 11): its 252 sessions hold 126 earlier ones at IV 10, below it, and 125 at 11
    assert s["iv_pctile"][251] == pytest.approx(126 / 251) and s["iv_pctile"][250] == 0.0
    assert s["iv_pctile"][200] is None  # 201 sessions with an IV: iv_stats_sessions needs 202 of 252
    rv20 = (
        0.01 * math.sqrt(20 / 19) * math.sqrt(252) * 100
    )  # returns alternating ±1 %: sample sd × √252, in %
    assert s["rv_20"][251] == pytest.approx(rv20) and s["iv_rv_ratio"][251] == pytest.approx(11 / rv20)


def test_a_planted_iv_shift_reads_act_on_the_levels_and_not_on_their_ratio() -> None:
    rep = drift.report({"NIFTY": _panel(live_iv=25.0)})
    card = _group(rep, "card")
    assert card["status"] == "act" and {"iv_30", "rv_20", "rv_60"} <= {a["input"] for a in card["act"]}
    assert card["inputs"]["iv_rv_ratio"]["status"] != "act"  # realised volatility rose with the IV
    psis = [a["psi"] for a in card["act"]]
    assert psis == sorted(psis, reverse=True) and card["reference"] == ["2022-04-01", "2024-06-30"]
    direction = _group(rep, "direction")
    assert direction["status"] == "act" and direction["reference"] == ["2022-01-01", "2024-06-30"]
    assert direction["inputs"]["fii_fut_net"]["status"] == "insufficient data"  # no participant data stored
    assert "fii_fut_net" in direction["insufficient"]
    assert rep["alarms"] == 2  # one per model and underlying with an input at act


def test_without_a_shift_the_iid_inputs_stay_below_act() -> None:
    rep = drift.report({"NIFTY": _panel(live_iv=15.0)})
    assert _group(rep, "card")["inputs"]["iv_30"]["psi"] < 0.25  # i.i.d. noise around the same IV
    assert _group(rep, "direction")["inputs"]["dow_mon"]["psi"] < 0.1  # the weekday mix does not move


def test_a_missing_panel_is_unknown_and_a_short_live_stretch_is_insufficient_never_ok() -> None:
    rep = drift.report({"NIFTY": _panel(live_end=date(2024, 9, 30))})
    for model in ("direction", "card"):
        bank = _group(rep, model, "BANKNIFTY")
        assert bank["status"] == "unknown" and "BANKNIFTY" in bank["why"]
        nifty = _group(rep, model)
        assert nifty["status"] == "insufficient data" and not nifty["act"]
        assert (
            "of the 250 needed" in nifty["inputs"]["iv_30"]["why"]
            and nifty["inputs"]["iv_30"]["n_live"] < 250
        )
    assert rep["alarms"] == 0

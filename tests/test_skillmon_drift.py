"""Feature drift (#378, skill-monitor-v2): PSI on the reference window's decile bins. Every expected value is worked by
hand from the registered rule (specs_v2.Drift); unknown or too few values read "insufficient data", never ok."""

from __future__ import annotations

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

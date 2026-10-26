"""Control charts over the daily data-quality series (#368, dq.control): EWMA and tabular CUSUM against a robust
40-session baseline, hand-computed on a baseline alternating 99 / 101 (median 100, MAD 1, sigma 1.4826), event
semantics (a lasting step warns once), and unknown when the history is short or flat. Pure; no database."""

from __future__ import annotations

import pytest

BASE = [99.0, 101.0] * 20  # median 100, MAD 1: sigma = 1.4826


def run(last: float, pre: float = 100.0) -> list[float]:
    return BASE + [pre] * 19 + [last]


@pytest.mark.parametrize("last,status", [(106.0, "ok"), (109.0, "warn")])
def test_ewma_hand_computed(last, status):
    from finresearch.dq import control

    # u = (x - 100) / 1.4826; the run sits on the centre, so z_t = 0.2 u: 106 -> 0.80939, 109 -> 1.21408;
    # limit 2.962 x sqrt(0.2 / 1.8) = 0.98733
    c = control.chart("ewma", run(last))
    assert c.status == status
    assert c.stat == pytest.approx(0.2 * (last - 100) / 1.4826, abs=1e-6)
    assert c.limit == pytest.approx(0.98733, abs=1e-5)


@pytest.mark.parametrize("last,status,stat", [(106.0, "ok", 3.54694), (109.0, "warn", 5.57042),
                                              (91.0, "warn", -5.57042)])  # fmt: skip
def test_cusum_hand_computed_both_sides(last, status, stat):
    from finresearch.dq import control

    # C+ = max(0, u - 0.5), C- = max(0, -u - 0.5); limit h = 5; the statistic is signed (-C- below the centre)
    c = control.chart("cusum", run(last))
    assert (c.status, c.limit) == (status, 5.0)
    assert c.stat == pytest.approx(stat, abs=1e-5)


@pytest.mark.parametrize("kind", ["ewma", "cusum"])
def test_a_lasting_step_warns_once(kind):
    """70 quiet sessions, then a step of +6 that lasts 30: evaluated session by session, the chart warns on exactly
    one session, the second of the step (the quiet run leaves EWMA near +0.075 and C+ at 0.174, so EWMA 0.869 then
    1.505 > 0.98733, CUSUM 3.721 then 7.268 > 5), and reads ok with 'beyond its limit' on the 28 that follow."""
    from finresearch.dq import control

    series = [99.0, 101.0] * 35 + [106.0] * 30
    seen = [control.chart(kind, series[: t + 1][-60:]) for t in range(59, len(series))]
    warns = [i + 59 for i, c in enumerate(seen) if c.status == "warn"]
    assert warns == [71]
    after = seen[71 - 59 + 1 :]
    assert len(after) == 28 and all(c.status == "ok" and c.beyond_since is not None for c in after)


def test_quiet_series_never_warns():
    from finresearch.dq import control

    series = [99.0, 101.0, 100.0, 98.5, 101.5] * 24
    for kind in ("ewma", "cusum"):
        assert all(control.chart(kind, series[: t + 1][-60:]).status == "ok" for t in range(59, len(series)))


@pytest.mark.parametrize("values,why", [([100.0] * 59, "60 sessions"), ([100.0] * 60, "flat")])
def test_short_or_flat_history_is_unknown(values, why):
    from finresearch.dq import control

    c = control.chart("ewma", values)
    assert c.status == "unknown" and why in c.note

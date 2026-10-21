"""The skill monitor's evaluation of a cell (#378): below the trigger only CITL and the promotion CI, labelled
"insufficient data" (never ok); at the trigger every registered check with its alarm rule."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import numpy as np
import pytest

from finresearch.skillmon import cells, monitor, prereg, specs

T0 = datetime(2026, 1, 1, 11, 0, tzinfo=UTC)


def data(p, o, *, pairs=None, benchmark="bench", method="model", underlying="NIFTY") -> cells.CellData:
    ind = [SimpleNamespace(id=i, probability=a, outcome=b, resolved_at=T0 + timedelta(days=i))
           for i, (a, b) in enumerate(zip(p, o, strict=True))]  # fmt: skip
    return cells.CellData(cells.Cell(method, "up_h5", underlying), "fno", benchmark, logged=len(p), independent=ind,
                          pairs=pairs if pairs is not None else [(a, 0.5, b) for a, b in zip(p, o, strict=True)])  # fmt: skip


def calibrated(n, seed=1):
    rng = np.random.default_rng(seed)
    p = list(rng.uniform(0.3, 0.7, n))
    return p, [int(rng.random() < a) for a in p]


def test_below_the_trigger_reads_insufficient_data_with_citl_and_the_promotion_ci() -> None:
    p, o = calibrated(60)
    r = monitor.evaluate(data(p, o))
    assert r["status"] == "insufficient data" and r["mode"] == "on read"
    assert r["independent"] == 60 and r["trigger"] == specs.TRIGGER.full_events
    assert r["citl"] == pytest.approx(sum(o) / 60 - sum(p) / 60)
    assert r["skill"]["n"] == 60 and r["skill"]["lo"] is not None
    assert "cusum" not in r and "spiegelhalter" not in r and r["alarms"] == []


def test_an_empty_cell_is_insufficient_never_ok() -> None:
    r = monitor.evaluate(data([], []))
    assert r["status"] == "insufficient data" and r["citl"] is None
    assert r["skill"]["promotion"] == "insufficient data"


def test_promotion_needs_the_registered_number_of_events() -> None:
    p = [0.9] * 30 + [0.1] * 30
    o = [1] * 30 + [0] * 30
    r = monitor.evaluate(data(p, o))  # 60 >= 50 events, model far better than 0.5
    assert r["skill"]["promotion"] == "CI above 0"
    assert (
        monitor.evaluate(data(p[:20] + p[30:50], o[:20] + o[30:50]))["skill"]["promotion"]
        == "insufficient data"
    )


def test_full_mode_on_a_calibrated_cell_is_ok() -> None:
    p, o = calibrated(300, seed=3)
    r = monitor.evaluate(data(p, o))
    assert r["mode"] == "full" and r["alarms"] == [] and r["status"] == "ok", r
    assert r["murphy"]["bins"] == specs.CALIBRATION.murphy_bins and r["cusum"]["alarm"] is None


def test_full_mode_raises_alarms_on_a_drifted_cell() -> None:
    p = [0.3] * 300
    o = [int(i % 10 < 3) for i in range(150)] + [int(i % 10 < 7) for i in range(150)]  # 30 % then 70 % up
    r = monitor.evaluate(data(p, o, pairs=[(0.3, 0.5, b) for b in o]))
    checks = {a["check"] for a in r["alarms"]}
    assert r["status"] == "alarm" and {"cusum", "spiegelhalter", "skill"} <= checks
    cus = next(a for a in r["alarms"] if a["check"] == "cusum")
    assert cus["side"] == "up" and 150 <= cus["at"] < 250


def test_full_mode_with_an_undefined_check_is_unknown_not_ok() -> None:
    p, o = [0.5] * 120, [int(i % 2 == 0) for i in range(120)]  # Spiegelhalter's variance is 0 at p = 0.5
    r = monitor.evaluate(data(p, o, benchmark=None, pairs=[]))
    assert r["status"] == "unknown" and r["skill"]["status"] == "no benchmark"


def test_a_real_money_cell_is_monitored_in_full_below_the_trigger(monkeypatch) -> None:
    p, o = calibrated(40)
    monkeypatch.setattr(specs, "TRIGGER", specs.Trigger(real_money_cells=("model|up_h5|NIFTY",)))
    assert monitor.evaluate(data(p, o))["mode"] == "full"


def test_the_registration_is_pinned_and_a_changed_spec_is_refused(monkeypatch) -> None:
    assert specs.registration_hash() == prereg.HASH
    monkeypatch.setattr(prereg, "HASH", "0" * 64)
    with pytest.raises(monitor.PreregistrationError):
        monitor.require_registered()


def test_prereg_md_quotes_the_hash_and_the_thresholds() -> None:
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / "evals/experiments/skill_monitoring/PREREG.md").read_text()
    c = specs.CALIBRATION
    assert prereg.HASH in text and f"h_up = {c.cusum_h_up}" in text and f"h_down = {c.cusum_h_down}" in text
    assert specs.TRIGGER.full_events == 100 and specs.TRIGGER.promotion_events == 50

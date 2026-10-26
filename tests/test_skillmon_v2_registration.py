"""skill-monitor-v2's registration (#378): its hash is pinned in prereg.V2_HASH, v1's registration and hash are
untouched, and ADDENDUM.md (never PREREG.md) restates the pinned hash and the registered numbers."""

from __future__ import annotations

from pathlib import Path

from finresearch.skillmon import prereg, specs, specs_v2

EXPERIMENT = Path(__file__).resolve().parents[1] / "evals/experiments/skill_monitoring"
V1_HASH = "48a6ef33ede244488a3a489ed9ddc781b2443a92dc24c113756aeac255ea3de8"  # PR #442's pinned v1


def test_v2_is_pinned_and_v1_is_unchanged() -> None:
    assert specs_v2.registration_hash() == prereg.V2_HASH
    assert specs.registration_hash() == prereg.HASH == V1_HASH
    assert specs_v2.SPEC_VERSION == "skill-monitor-v2" and specs_v2.BASE_VERSION == specs.SPEC_VERSION
    assert (
        V1_HASH in (EXPERIMENT / "PREREG.md").read_text()
        and prereg.V2_HASH not in (EXPERIMENT / "PREREG.md").read_text()
    )


def test_the_registration_tracks_the_windows_it_borrows() -> None:
    """The reference windows and the walk-forward's years are read from the modules that register them, so a change
    there changes v2's hash (and the monitor refuses) instead of silently moving the reference."""
    from finresearch.direction import specs as dspecs
    from finresearch.evals import ipo_model as im
    from finresearch.horserace import specs as hspecs

    reg = specs_v2.registration()
    assert reg["models"]["direction"]["reference"] == (dspecs.TRAIN_START, dspecs.IS_END)
    design = hspecs.WINDOWS["design"]
    assert [d.isoformat() for d in reg["models"]["card"]["reference"]] == [design["start"], design["end"]]
    assert reg["models"]["direction"]["inputs"] == tuple(dspecs.FEATURES)
    assert reg["ipo"]["years"] == tuple(im.GATE_YEARS) and reg["ipo"]["taus"] == tuple(im.TAUS)


def test_addendum_restates_the_hash_and_the_registered_numbers() -> None:
    text = (EXPERIMENT / "ADDENDUM.md").read_text()
    d, i, r = specs_v2.DRIFT, specs_v2.IPO, specs_v2.RISK
    assert prereg.V2_HASH in text and specs_v2.SPEC_VERSION in text and "post hoc" in text
    assert f"{d.watch:.2f}" in text and f"{d.act:.2f}" in text and f"{d.live_values} " in text
    assert f"{i.nominal_pct} %" in text and f"±{i.tolerance_pp} pp" in text and f"{i.pass_years} of 7" in text
    assert (
        f"p < {r.alpha}" in text
        and f"{r.consecutive} consecutive" in text
        and f"{r.min_days} resolved days" in text
    )
    for day in ("2022-01-01", "2022-04-01", "2024-06-30"):
        assert day in text


def test_a_changed_v2_registration_is_refused(monkeypatch) -> None:
    import pytest

    from finresearch.skillmon import monitor

    assert monitor.require_registered_v2() == prereg.V2_HASH
    monkeypatch.setattr(prereg, "V2_HASH", "0" * 64)
    with pytest.raises(monitor.PreregistrationError, match="skill-monitor-v2 is refused"):
        monitor.require_registered_v2()
    monitor.require_registered()  # v1 is checked on its own pinned hash and still runs

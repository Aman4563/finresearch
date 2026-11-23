"""#520: the touch formula's registration record. The risk alerts shipped (#398) with no pinned hash; the record in
fincalc/risk_alert_specs.py is post hoc and pins both variants, the level, the labels and the replay's definitions."""

from __future__ import annotations

import dataclasses
import math
from pathlib import Path

import pytest

from finresearch.fincalc import risk_alert_prereg as prereg
from finresearch.fincalc import risk_alert_specs as sp
from finresearch.fincalc import risk_alerts as ra

ADDENDUM = Path(__file__).resolve().parents[1] / "evals/experiments/risk_alerts/ADDENDUM.md"


def test_the_record_and_each_variant_hash_to_the_pinned_values() -> None:
    assert sp.registration_hash() == prereg.HASH, (
        "risk_alert_specs.py (or a label or the level it reads from risk_alerts.py) changed: never edit a registered "
        "variant in place; add a variant, bump VARIANTS_TRIED and SPEC_VERSION, record it in ADDENDUM.md"
    )
    assert {v.key: sp.variant_hash(v) for v in sp.VARIANTS} == prereg.VARIANT_HASHES
    assert sp.VARIANTS_TRIED == len(sp.VARIANTS) == 2 and sp.SPEC_VERSION == "risk-alert-touch-v2"


def test_variant_1_is_the_shipped_label_and_level_and_an_edit_moves_its_hash() -> None:
    # as shipped by PR #453: the level, and the label's formula wording
    assert ra.TOUCH_LEVEL == 0.60 and "driftless reflection, 2 x P_Q(S_T beyond K)" in ra.TOUCH_LABEL
    assert sp.V1.label is ra.TOUCH_LABEL and sp.V1.level == ra.TOUCH_LEVEL == sp.V2.level
    for change in ({"level": 0.55}, {"label": ra.TOUCH_LABEL + "."}, {"formula": "2 N(-d1)"}):
        assert sp.variant_hash(dataclasses.replace(sp.V1, **change)) != prereg.VARIANT_HASHES[sp.V1.key]
    assert sp.variant_hash(dataclasses.replace(sp.V2, level=0.55)) != prereg.VARIANT_HASHES[sp.V2.key]


def test_the_alert_reads_variant_2_and_variant_1_stays_callable() -> None:
    assert ra.TOUCH_VARIANT == sp.ACTIVE == sp.V2.key == "touch-exact-v2"
    assert ra.TOUCH_VARIANTS[sp.V1.key] == (ra.touch_prob, ra.TOUCH_LABEL)
    assert ra.TOUCH_VARIANTS[sp.V2.key] == (ra.touch_prob_exact, ra.TOUCH_LABEL_EXACT)
    assert sp.V2.label is ra.TOUCH_LABEL_EXACT
    for word in ("risk-neutral", "exact", "continuously", "daily closes", "not a forecast"):
        assert word in ra.TOUCH_LABEL_EXACT


@pytest.mark.parametrize("v", sp.VARIANTS, ids=lambda v: v.key)
def test_each_variant_reproduces_its_registered_hand_worked_values(v: sp.TouchVariant) -> None:
    fn = ra.TOUCH_VARIANTS[v.key][0]
    for log_moneyness, sigma, t, want in v.reference:
        assert fn(100.0, 100.0 * math.exp(log_moneyness), sigma, t) == pytest.approx(want, abs=1e-5)


def test_the_replay_reads_the_marks_at_the_rate_the_alert_uses() -> None:
    from finresearch.alerts import risk_readers

    assert sp.MARK_RATE == risk_readers.MARK_RATE == 0.065
    assert sp.registration()["replay"]["criterion"].startswith("none")


def test_the_addendum_quotes_the_hashes_and_says_post_hoc() -> None:
    text = ADDENDUM.read_text()
    assert prereg.HASH in text and all(h in text for h in prereg.VARIANT_HASHES.values())
    assert "post hoc" in text and sp.SPEC_VERSION in text and "there is no PREREG.md" in text
    assert "keep 0.60 or re-centre" in text and "Broadie, Glasserman and Kou (1997)" in text


def test_the_replay_is_refused_when_the_record_differs_from_its_pin(monkeypatch) -> None:
    assert prereg.require_registered() == prereg.HASH
    monkeypatch.setattr(prereg, "HASH", "0" * 64)
    with pytest.raises(prereg.PreregistrationError, match="refused"):
        prereg.require_registered()


def test_the_inventory_lists_the_registration() -> None:
    from finresearch.inventory import registry

    e = registry.entry_for_registration(sp.SPEC_VERSION)
    assert e is not None and e.key == "risk_alerts"

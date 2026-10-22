"""The collectable-VRP registration (P3.6, #389) is pinned: specs.py hashes to prereg.HASH, PREREG.md quotes it, the
vault study file it implements is unchanged and still the one the vault pack sealed, and every setting moves the
hash."""

import hashlib
from pathlib import Path

import pytest

from finresearch.vrp import prereg, specs

ROOT = Path(__file__).resolve().parents[1]


def test_registration_hash_is_pinned_and_quoted() -> None:
    assert specs.registration_hash() == prereg.HASH, (
        "vrp/specs.py changed after registration: record it in ADDENDUM.md as a new SPEC_VERSION and commit the "
        "new hash before the study reads a stored session (never edit PREREG.md)"
    )
    text = (ROOT / "evals/experiments/vrp_collectable/PREREG.md").read_text()
    assert prereg.HASH in text and specs.SPEC_VERSION in text and specs.VAULT_FILE_SHA256 in text


def test_the_vault_study_file_is_the_one_registered_and_sealed() -> None:
    from finresearch.vault import pack, registry

    body = (ROOT / "evals/experiments/vault/vrp.md").read_bytes()
    assert hashlib.sha256(body).hexdigest() == specs.VAULT_FILE_SHA256
    assert registry.study_hash(registry.STUDIES["vrp_primary"], ROOT) == pack.STUDY_HASHES["vrp_primary"]


def test_every_setting_moves_the_hash(monkeypatch) -> None:
    from finresearch.fincalc import fno_history as fh
    from finresearch.fincalc import mfiv
    from finresearch.fincalc import realvol as rv
    from finresearch.vrp import collect, measure, run, study

    for mod, name, value in ((mfiv, "MIN_PER_SIDE", 2), (mfiv, "MAX_GAP", 0.1), (mfiv, "N30", 40_000),
                             (measure, "ROLL_SESSIONS", 5), (fh, "RATE", 0.07), (fh, "MIN_CONTRACTS", 1),
                             (study, "HORIZON_DAYS", 31), (study, "NW_LAG", 2), (study, "TERCILE_BURN_IN", 6),
                             (study, "GATE_MIN_CORR", 0.8), (study, "GATE_MAX_GAP", 2.0), (rv, "YZ_ALPHA", 1.5),
                             (collect, "BAND", 0.75), (collect, "FUT_SLIPPAGE_BPS", 0.5), (collect, "PUT_DELTA", -0.3),
                             (collect, "BOOTSTRAP_SEED", 1), (collect, "CRASHES", {}), (run, "PERIODS", {}),
                             (study, "LOG_RATIO_LABEL", "x")):  # fmt: skip
        monkeypatch.setattr(mod, name, value)
        assert specs.registration_hash() != prereg.HASH, name
        monkeypatch.undo()


def test_require_registered_refuses_a_changed_spec(monkeypatch) -> None:
    from finresearch.vrp import collect

    assert prereg.require_registered() == prereg.HASH
    monkeypatch.setattr(collect, "BAND", 1.0)
    with pytest.raises(prereg.PreregistrationError):
        prereg.require_registered()

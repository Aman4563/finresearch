"""The volatility race's registration (P3.7, #390) is pinned: specs.py hashes to prereg.HASH, PREREG.md quotes it, the
vault study file it implements is unchanged, and every numeric setting it reads moves the hash."""

import hashlib
from pathlib import Path

import pytest

from finresearch.volrace import prereg, race, specs

ROOT = Path(__file__).resolve().parents[1]


def test_registration_hash_is_pinned_and_quoted() -> None:
    assert specs.registration_hash() == prereg.HASH, (
        "volrace/specs.py changed after registration: record it in ADDENDUM.md as a new SPEC_VERSION and commit "
        "the new hash before the race reads a stored session (never edit PREREG.md)"
    )
    text = (ROOT / "evals/experiments/vol_forecast_race/PREREG.md").read_text()
    assert prereg.HASH in text and specs.SPEC_VERSION in text
    assert race.CHALLENGER == "har_iv" and race.BENCHMARK == "mkt" and race.HORIZONS == (5, 10, 22)


def test_the_vault_study_file_is_the_one_registered() -> None:
    body = (ROOT / "evals/experiments/vault/vol_race.md").read_bytes()
    assert hashlib.sha256(body).hexdigest() == specs.VAULT_FILE_SHA256


def test_require_registered_refuses_a_changed_spec(monkeypatch) -> None:
    assert prereg.require_registered() == prereg.HASH
    monkeypatch.setattr(race, "MCS_REPS", 999)
    with pytest.raises(prereg.PreregistrationError):
        prereg.require_registered()


def test_every_setting_moves_the_hash(monkeypatch) -> None:
    from finresearch.fincalc import fno_history as fh
    from finresearch.fincalc import realvol as rv
    from finresearch.fincalc import taildist as td

    for mod, name, value in ((race, "TRAIL", 21), (race, "MAX_ROWS", 4000), (race, "GARCH_WINDOW", 750),
                             (race, "DM_ALPHA", 0.1), (race, "ADOPT_MIN_HORIZONS", 1), (race, "MCS_ALPHA", 0.25),
                             (race, "MCS_MEAN_BLOCK", 2.0), (race, "SEED", 1), (race, "MIN_MCS_ROWS", 5),
                             (rv, "YZ_ALPHA", 1.5), (rv, "HAR_MONTH", 21), (rv, "GARCH_START", (0.1, 0.8)),
                             (td, "EWMA_LAMBDA", 0.97), (td, "GJR_START", (0.1, 0.1, 0.8)), (fh, "ATM_BAND", 0.05),
                             (fh, "MIN_CONTRACTS", 1)):  # fmt: skip
        monkeypatch.setattr(mod, name, value)
        assert specs.registration_hash() != prereg.HASH, name
        monkeypatch.undo()

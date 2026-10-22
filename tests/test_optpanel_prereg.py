"""The stock-option VRP study's registration (P1.12, #374) is pinned before any panel code exists: optpanel/specs.py
hashes to prereg.HASH, PREREG.md quotes it and is itself pinned by its sha256, and every numeric setting the panel
or the study reads moves the hash."""

import hashlib
from pathlib import Path

import pytest

from finresearch.optpanel import params, prereg, specs

ROOT = Path(__file__).resolve().parents[1]
PREREG_MD = ROOT / "evals/experiments/stock_option_vrp/PREREG.md"


def test_registration_hash_is_pinned_and_quoted() -> None:
    assert specs.registration_hash() == prereg.HASH, (
        "optpanel/specs.py or optpanel/params.py changed after registration: record it in "
        "evals/experiments/stock_option_vrp/ADDENDUM.md as a new SPEC_VERSION and commit the new hash first "
        "(never edit PREREG.md)"
    )
    text = PREREG_MD.read_text()
    assert prereg.HASH in text and specs.SPEC_VERSION in text


def test_prereg_md_is_the_one_registered() -> None:
    assert hashlib.sha256(PREREG_MD.read_bytes()).hexdigest() == prereg.PREREG_SHA256


def test_require_registered_passes_on_the_committed_files() -> None:
    assert prereg.require_registered() == prereg.HASH


def test_require_registered_refuses_a_changed_spec(monkeypatch) -> None:
    monkeypatch.setattr(params, "LIQ_BAND", 0.06)
    with pytest.raises(prereg.PreregistrationError):
        prereg.require_registered()


def test_require_registered_refuses_an_edited_or_missing_prereg_md(tmp_path) -> None:
    with pytest.raises(prereg.PreregistrationError, match=r"PREREG\.md"):
        prereg.require_registered(root=tmp_path)  # no PREREG.md under this root
    p = tmp_path / prereg.PREREG_FILE
    p.parent.mkdir(parents=True)
    p.write_bytes(PREREG_MD.read_bytes() + b"\nan edit\n")
    with pytest.raises(prereg.PreregistrationError, match=r"PREREG\.md"):
        prereg.require_registered(root=tmp_path)


def test_every_setting_moves_the_hash(monkeypatch) -> None:
    from finresearch.backtest import specs as bspecs
    from finresearch.fincalc import fno_history as fh

    for mod, name, value in ((params, "ROWS_LN_BAND", 0.25), (params, "ROWS_EXPIRIES", 3),
                             (params, "LIQ_BAND", 0.04), (params, "BUCKET_EDGES", (-0.1, 0.1)),
                             (params, "BAN_ENTER", 0.9), (params, "BAN_EXIT", 0.75),
                             (params, "MIN_COVERAGE", 0.4), (params, "COVERAGE_SESSIONS", 40),
                             (params, "MIN_CROSS_SECTION", 30), (params, "QUANTILES", (10,)),
                             (params, "ALPHA", 0.1), (params, "SAMPLE_START", "2023-01-01"),
                             (fh, "RATE", 0.07), (fh, "MIN_CONTRACTS", 5), (fh, "ATM_BAND", 0.05),
                             (fh, "MIN_DTE", 5), (fh, "TARGET_DTE", 45), (fh, "FLAT_WITHIN", 3),
                             (fh, "RV_WINDOW", 10), (fh, "QUARANTINE_LN", 0.3),
                             (bspecs, "BOOK", bspecs.Book(min_slippage=0.2))):  # fmt: skip
        monkeypatch.setattr(mod, name, value)
        assert specs.registration_hash() != prereg.HASH, name
        monkeypatch.undo()

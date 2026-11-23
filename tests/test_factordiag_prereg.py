"""The stock factor diagnostics' registration (#506) is pinned before any code that could read a price exists:
factordiag/specs.py hashes to prereg.HASH, PREREG.md quotes it and is itself pinned by its sha256, the two membership
files of the base experiment are the ones pinned, every setting the study reads moves the hash, and the registered
family sizes are the ones the spec's own constants give."""

import hashlib
from datetime import date
from pathlib import Path

import pytest

from finresearch.factordiag import prereg, specs

ROOT = Path(__file__).resolve().parents[1]
PREREG_MD = ROOT / prereg.PREREG_FILE


def test_registration_hash_is_pinned_and_quoted() -> None:
    assert specs.registration_hash() == prereg.HASH, (
        "factordiag/specs.py (or a setting it reads) changed after registration: record it in "
        "evals/experiments/stock_factor_diagnostics/ADDENDUM.md as a new SPEC_VERSION and commit the new hash before "
        "the diagnostic reads a price (never edit PREREG.md)"
    )
    text = PREREG_MD.read_text()
    assert prereg.HASH in text and specs.SPEC_VERSION in text


def test_prereg_md_is_the_one_registered() -> None:
    assert hashlib.sha256(PREREG_MD.read_bytes()).hexdigest() == prereg.PREREG_SHA256


def test_the_membership_files_are_the_ones_pinned() -> None:
    for name, want in ((specs.CHANGES_FILE, specs.CHANGES_SHA256),
                       (specs.CONSTITUENTS_FILE, specs.CONSTITUENTS_SHA256)):  # fmt: skip
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == want, name


def test_require_registered_passes_on_the_committed_files() -> None:
    assert prereg.require_registered() == prereg.HASH


def test_require_registered_refuses_a_changed_setting(monkeypatch) -> None:
    monkeypatch.setattr(specs, "FDR", 0.2)
    with pytest.raises(prereg.PreregistrationError, match="registered hash"):
        prereg.require_registered()


def _copy(tmp_path: Path, *names: str) -> None:
    for name in names:
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_bytes((ROOT / name).read_bytes())


def test_require_registered_refuses_an_edited_or_missing_prereg_md(tmp_path) -> None:
    _copy(tmp_path, specs.CHANGES_FILE, specs.CONSTITUENTS_FILE)
    with pytest.raises(prereg.PreregistrationError, match=r"PREREG\.md"):
        prereg.require_registered(root=tmp_path)  # no PREREG.md under this root
    p = tmp_path / prereg.PREREG_FILE
    p.parent.mkdir(parents=True)
    p.write_bytes(PREREG_MD.read_bytes() + b"\nan edit\n")
    with pytest.raises(prereg.PreregistrationError, match=r"PREREG\.md"):
        prereg.require_registered(root=tmp_path)


def test_require_registered_refuses_a_changed_membership_file(tmp_path) -> None:
    _copy(tmp_path, prereg.PREREG_FILE, specs.CHANGES_FILE, specs.CONSTITUENTS_FILE)
    assert prereg.require_registered(root=tmp_path) == prereg.HASH
    log = tmp_path / specs.CHANGES_FILE
    log.write_text(log.read_text() + "2026-10-01,include,MADEUPCO,https://example.invalid/none.pdf\n")
    with pytest.raises(prereg.PreregistrationError, match="membership file"):
        prereg.require_registered(root=tmp_path)


def test_every_setting_moves_the_hash(monkeypatch) -> None:
    from finresearch.evals import stock_backtest as bt
    from finresearch.evals import stock_universe as su
    from finresearch.fincalc import signals as sg

    for mod, name, value in (
        (specs, "HORIZONS", (1, 3, 6)), (specs, "SEGMENTS", ((0, 1), (1, 12))),
        (specs, "BLOCK_MONTHS", {**specs.BLOCK_MONTHS, 12: 12}), (specs, "NW_LAG", {**specs.NW_LAG, 12: 12}),
        (specs, "MIN_NAMES", 10), (specs, "MIN_MONTHS", 24), (specs, "MIN_BLOCKS_CI", 2),
        (specs, "MIN_BLOCKS_A", 4), (specs, "MIN_BLOCKS_B", 3), (specs, "QUANTILES", 3),
        (specs, "SPLIT", date(2019, 12, 31)), (specs, "FAMILY_B_HORIZONS", (1,)), (specs, "FDR", 0.05),
        (specs, "CI_LEVEL", 0.9), (specs, "POWER", 0.9), (specs, "EXACT_MAX_BLOCKS", 10),
        (specs, "EXACT_MAX_COMBOS", 1000), (specs, "DRAWS", 1000), (specs, "SEED", 1), (specs, "TOLERANCE", 1e-9),
        (specs, "PRICE_WINDOW_DAYS", 3), (specs, "AUDIT_EVERY", 6), (specs, "MOM_POINTS", 30.0),
        (specs, "MOM_SCALE", 1.0), (specs, "TREND_POINTS", 10.0), (specs, "TREND_SCALE", 0.2),
        (specs, "TREND_WINDOW", 100), (specs, "CHANGES_SHA256", "0" * 64), (specs, "CONSTITUENTS_SHA256", "0" * 64),
        (specs, "COMPONENTS", {**specs.COMPONENTS, "extra": "a sixth score"}),
        (specs, "READING", {**specs.READING, "always": "anything goes"}),
        (specs, "FAMILIES", {**specs.FAMILIES, "A": "more tests"}), (specs, "NULLS", {**specs.NULLS, "family_a": "x"}),
        (bt, "WARMUP", 200), (bt, "COST_BUY", 0.002), (bt, "COST_SELL", 0.002),
        (sg, "TRADING_DAYS", 250), (sg, "SKIP_DAYS", 0), (su, "MARKET", "OTHERETF"),
        (su, "DATA_SYMBOL", {**su.DATA_SYMBOL, "MADEUPCO": "MADEUP2"}),
    ):  # fmt: skip
        monkeypatch.setattr(mod, name, value)
        assert specs.registration_hash() != prereg.HASH, name
        monkeypatch.undo()


def test_the_families_are_the_registered_sizes_and_the_blocks_cover_the_horizons() -> None:
    assert len(specs.COMPONENTS) * len(specs.HORIZONS) == 20 and "20 tests" in specs.FAMILIES["A"]
    assert len(specs.COMPONENTS) * len(specs.FAMILY_B_HORIZONS) == 10 and "10 tests" in specs.FAMILIES["B"]
    assert set(specs.BLOCK_MONTHS) == set(specs.NW_LAG) == set(specs.HORIZONS) >= set(specs.FAMILY_B_HORIZONS)
    assert {b for _, b in specs.SEGMENTS} <= set(specs.HORIZONS)
    for h in specs.HORIZONS:  # the brief's rule: lag and block at least the horizon (here at least 2h)
        assert specs.BLOCK_MONTHS[h] >= max(12, 2 * h) and specs.NW_LAG[h] == max(6, 2 * h)

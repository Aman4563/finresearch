"""The seen-period risk-budget study's registration (P4.2 Stage 2, #395) is pinned before any code that replays stored
history exists: riskbudget/specs.py hashes to prereg.HASH, PREREG.md quotes it and is itself pinned by its sha256,
the sealed vault study file it implements is unchanged, and every setting the study reads moves the hash."""

import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

from finresearch.riskbudget import prereg, specs

ROOT = Path(__file__).resolve().parents[1]
PREREG_MD = ROOT / "evals/experiments/risk_budget_seen/PREREG.md"


def test_registration_hash_is_pinned_and_quoted() -> None:
    assert specs.registration_hash() == prereg.HASH, (
        "riskbudget/specs.py (or a setting it reads) changed after registration: record it in "
        "evals/experiments/risk_budget_seen/ADDENDUM.md as a new SPEC_VERSION and commit the new hash before the "
        "study replays a stored session (never edit PREREG.md)"
    )
    text = PREREG_MD.read_text()
    assert prereg.HASH in text and specs.SPEC_VERSION in text and specs.VAULT_FILE_SHA256 in text


def test_prereg_md_is_the_one_registered() -> None:
    assert hashlib.sha256(PREREG_MD.read_bytes()).hexdigest() == prereg.PREREG_SHA256


def test_the_vault_study_file_is_the_one_registered_and_sealed() -> None:
    from finresearch.vault import pack, registry

    body = (ROOT / specs.VAULT_FILE).read_bytes()
    assert hashlib.sha256(body).hexdigest() == specs.VAULT_FILE_SHA256
    study = registry.STUDIES[specs.VAULT_STUDY]
    assert registry.study_hash(study, ROOT) == pack.STUDY_HASHES[specs.VAULT_STUDY]


def test_require_registered_passes_on_the_committed_files() -> None:
    assert prereg.require_registered() == prereg.HASH


def test_require_registered_refuses_a_changed_setting(monkeypatch) -> None:
    from finresearch.fincalc import tradeplan

    monkeypatch.setattr(tradeplan, "SHOCK_BUDGET_PCT", 5.0)
    with pytest.raises(prereg.PreregistrationError, match="registered hash"):
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
    from finresearch.backtest import dbhistory
    from finresearch.backtest import specs as bspecs
    from finresearch.fincalc import (
        beta_specs,
        book_scenarios,
        fno_history,
        margin_approx,
        tradeplan,
        volatility,
    )

    seller = replace(bspecs.SPECS[0], profit_take_pct=40.0)
    for mod, name, value in (
        (specs, "PERIOD_START", specs.PERIOD_START.replace(day=2)), (specs, "PERIOD_END", specs.PERIOD_START),
        (specs, "SPEC_KEYS", specs.SPEC_KEYS[:3]), (specs, "PRIMARY_MOVES", (-0.03,)),
        (specs, "BLOCK_MONTHS", 6), (specs, "DRAWS", 1000), (specs, "SEED", 1), (specs, "ALPHA", 0.1),
        (specs, "MAX_TRADES_FALL", 0.5), (specs, "MIN_MONTHS", 6), (specs, "KELLY_MIN_TRADES", 20),
        (specs, "FILL", "next_open"), (specs, "SPAN_RANGES", False),
        (tradeplan, "BOOK_BUDGET_PCT", 6.0), (tradeplan, "SHOCK_BUDGET_PCT", 5.0), (tradeplan, "SHOCK_MOVES", (-0.05,)),
        (tradeplan, "MARGIN_UTIL_MAX", 0.6), (tradeplan, "CASH_SHARE_MIN", 0.4), (tradeplan, "MONTH_LOSS_STOP_PCT", 5.0),
        (bspecs, "BOOK", replace(bspecs.BOOK, max_lots=10)), (bspecs, "CRITERIA", replace(bspecs.CRITERIA, bootstrap_seed=1)),
        (bspecs, "SPECS", (seller, *bspecs.SPECS[1:])),
        (book_scenarios, "DOWN_SLOPE", 1.0), (book_scenarios, "UP_SLOPE", 0.7), (book_scenarios, "MIN_VOL", 0.02),
        (beta_specs, "SPEC", replace(beta_specs.SPEC, min_returns=100)),
        (margin_approx, "PARAMS", {**margin_approx.PARAMS, "index": {"price_scan": 0.07, "vol_scan": 0.04,
                                                                     "exposure": 0.02}}),
        (margin_approx, "EXTREME_WEIGHT", 0.3), (dbhistory, "SPAN_MAX_AGE_DAYS", 30),
        (volatility, "WINDOW", 200), (volatility, "MIN_DAYS", 40), (fno_history, "QUARANTINE_LN", 0.3),
    ):  # fmt: skip
        monkeypatch.setattr(mod, name, value)
        assert specs.registration_hash() != prereg.HASH, name
        monkeypatch.undo()


def test_a_spec_added_to_the_library_later_leaves_the_registration_as_it_is(monkeypatch) -> None:
    from finresearch.backtest import specs as bspecs

    extra = replace(bspecs.SPECS[0], key="nifty_pcs_m_variant")
    monkeypatch.setattr(bspecs, "SPECS", (*bspecs.SPECS, extra))
    monkeypatch.setattr(bspecs, "VARIANTS_TRIED", bspecs.VARIANTS_TRIED + 1)
    assert specs.registration_hash() == prereg.HASH

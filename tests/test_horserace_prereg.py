"""The card horse race's registration (#377) is pinned: specs.py hashes to prereg.HASH, and PREREG.md quotes it."""

from pathlib import Path

from finresearch.horserace import prereg, specs

ROOT = Path(__file__).resolve().parents[1]


def test_registration_hash_is_pinned() -> None:
    assert specs.registration_hash() == prereg.HASH, (
        "horserace/specs.py changed after registration: record it in ADDENDUM.md as a new SPEC_VERSION and "
        "commit the new hash before any out-of-sample score (never edit PREREG.md)"
    )


def test_prereg_md_quotes_the_hash_and_the_incumbent() -> None:
    text = (ROOT / "evals/experiments/card_horse_race/PREREG.md").read_text()
    assert prereg.HASH in text
    assert specs.INCUMBENT == "p_rw" and specs.INCUMBENT in specs.FORECASTERS
    assert specs.TESTS.tail_min_events == 100 and specs.SCORES.primary_binary == "brier"


def test_addendum_hash_is_pinned_and_quoted() -> None:
    from finresearch.horserace import addendum

    assert addendum.addendum_hash() == prereg.ADDENDUM_HASH, "a changed addendum is a new ADDENDUM_VERSION"
    assert addendum.registration()["base_hash"] == prereg.HASH
    text = (ROOT / "evals/experiments/card_horse_race/ADDENDUM.md").read_text()
    assert prereg.ADDENDUM_HASH in text and "post hoc" in text


def test_addendum_a2_hash_is_pinned_quoted_and_covers_the_clock_weights(monkeypatch) -> None:
    from finresearch.fincalc import bustime as bt
    from finresearch.horserace import addendum, addendum_a2

    assert addendum_a2.addendum_hash() == prereg.ADDENDUM_A2_HASH, (
        "a changed addendum is a new ADDENDUM_VERSION"
    )
    reg = addendum_a2.registration()
    assert (
        reg["base_hash"] == prereg.HASH and reg["a1_hash"] == prereg.ADDENDUM_HASH == addendum.addendum_hash()
    )
    assert specs.CHALLENGERS == () and addendum_a2.CHALLENGERS == ("p_rw_bus",)
    text = (ROOT / "evals/experiments/card_horse_race/ADDENDUM.md").read_text()
    assert prereg.ADDENDUM_A2_HASH in text and "Addendum A2 (post hoc)" in text
    monkeypatch.setattr(bt, "PRIOR", bt.Weights(overnight=0.0, nontrading=0.2))
    assert addendum_a2.addendum_hash() != prereg.ADDENDUM_A2_HASH  # editing the weights moves the hash


def test_addendum_a3_hash_is_pinned_quoted_and_covers_the_engine_constants(monkeypatch) -> None:
    from finresearch.fincalc import black76 as b
    from finresearch.horserace import addendum_a2, addendum_a3

    assert addendum_a3.addendum_hash() == prereg.ADDENDUM_A3_HASH, (
        "a changed addendum is a new ADDENDUM_VERSION"
    )
    reg = addendum_a3.registration()
    assert reg["base_hash"] == prereg.HASH and reg["a1_hash"] == prereg.ADDENDUM_HASH
    assert reg["a2_hash"] == prereg.ADDENDUM_A2_HASH == addendum_a2.addendum_hash()
    assert addendum_a2.CHALLENGERS == ("p_rw_bus",) and addendum_a3.CHALLENGERS == ("p_rw_bus", "p_rn_b76")
    text = (ROOT / "evals/experiments/card_horse_race/ADDENDUM.md").read_text()
    assert prereg.ADDENDUM_A3_HASH in text and "Addendum A3 (post hoc)" in text
    for name, value in (("PARITY_BAND", 0.04), ("R_SE_MAX", 0.02), ("PRICE_TOL", 1e-8), ("MAX_BAND", 0.03)):
        monkeypatch.setattr(b, name, value)
        assert addendum_a3.addendum_hash() != prereg.ADDENDUM_A3_HASH, (
            name
        )  # editing a constant moves the hash
        monkeypatch.undo()


def test_addendum_a5_hash_is_pinned_quoted_and_covers_every_fitted_setting(monkeypatch) -> None:
    from finresearch.fincalc import taildist as td
    from finresearch.horserace import addendum_a3, addendum_a5

    assert addendum_a5.addendum_hash() == prereg.ADDENDUM_A5_HASH, (
        "a changed addendum is a new ADDENDUM_VERSION"
    )
    reg = addendum_a5.registration()
    assert reg["base_hash"] == prereg.HASH and reg["a1_hash"] == prereg.ADDENDUM_HASH
    assert reg["a2_hash"] == prereg.ADDENDUM_A2_HASH and reg["a3_hash"] == prereg.ADDENDUM_A3_HASH
    assert (*addendum_a3.CHALLENGERS, "p_rw_emp", "p_rw_fhs") == addendum_a5.CHALLENGERS
    text = (ROOT / "evals/experiments/card_horse_race/ADDENDUM.md").read_text()
    assert prereg.ADDENDUM_A5_HASH in text and "Addendum A5 (post hoc)" in text
    for name, value in (("MIN_Z", 40), ("PATHS", 10_000), ("GJR_WINDOW", 750), ("FHS_BLOCK", 3),
                        ("BLOCK_MEAN", 4.0), ("GJR_START", (0.1, 0.1, 0.8)), ("NM_MAXFEV", 3000)):  # fmt: skip
        monkeypatch.setattr(td, name, value)
        assert addendum_a5.addendum_hash() != prereg.ADDENDUM_A5_HASH, (
            name
        )  # editing a setting moves the hash
        monkeypatch.undo()
    for name, value in (("BUCKETS", {"short": (1, 10, "any")}), ("SEED", 1), ("GPD_TAIL", 0.95)):
        monkeypatch.setattr(addendum_a5, name, value)
        assert addendum_a5.addendum_hash() != prereg.ADDENDUM_A5_HASH, name
        monkeypatch.undo()

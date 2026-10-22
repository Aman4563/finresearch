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

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

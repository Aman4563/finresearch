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


def test_addendum_a6_hash_is_pinned_quoted_and_covers_the_simulation_settings(monkeypatch) -> None:
    """A6 (#388): the exit-rule Monte Carlo p_path. The hash covers the simulation settings (paths, seed, steps) and
    the path model's constants, so changing any of them is a new addendum version."""
    from finresearch.fincalc import black76 as b
    from finresearch.fnobook import path_pop as pp
    from finresearch.horserace import addendum_a3, addendum_a6

    assert addendum_a6.addendum_hash() == prereg.ADDENDUM_A6_HASH, (
        "a changed addendum is a new ADDENDUM_VERSION"
    )
    reg = addendum_a6.registration()
    assert reg["base_hash"] == prereg.HASH and reg["a1_hash"] == prereg.ADDENDUM_HASH
    assert reg["a2_hash"] == prereg.ADDENDUM_A2_HASH and reg["a3_hash"] == prereg.ADDENDUM_A3_HASH
    assert (
        addendum_a6.CHALLENGER == "p_path" and (*addendum_a3.CHALLENGERS, "p_path") == addendum_a6.CHALLENGERS
    )
    sim = reg["simulation"]
    assert sim["n_paths"] == addendum_a6.N_PATHS == 2000 and sim["seed"] == addendum_a6.SEED == 388
    assert sim["steps"] == addendum_a6.STEPS and "session" in sim["steps"]
    text = (ROOT / "evals/experiments/card_horse_race/ADDENDUM.md").read_text()
    assert prereg.ADDENDUM_A6_HASH in text and "Addendum A6 (post hoc)" in text
    for mod, name, value in ((addendum_a6, "N_PATHS", 2001), (addendum_a6, "SEED", 1), (addendum_a6, "STEPS", "x"),
                             (pp, "BETA_WINDOW", 260), (pp, "BETA_MIN", 100), (pp, "TICK", 0.1),
                             (b, "SIGMA_LO", 1e-3)):  # fmt: skip
        monkeypatch.setattr(mod, name, value)
        assert addendum_a6.addendum_hash() != prereg.ADDENDUM_A6_HASH, (
            name
        )  # editing a setting moves the hash
        monkeypatch.undo()


def test_each_card_has_its_own_reproducible_seed() -> None:
    from datetime import date

    from finresearch.horserace import addendum_a6

    a = addendum_a6.card_seed("nifty_pcs_m", date(2024, 7, 1))
    assert a == addendum_a6.card_seed("nifty_pcs_m", date(2024, 7, 1)) and 0 <= a < 2**63
    assert a != addendum_a6.card_seed("nifty_pcs_m", date(2024, 7, 2))
    assert a != addendum_a6.card_seed("nifty_ccs_m", date(2024, 7, 1))

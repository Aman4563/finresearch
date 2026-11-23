"""The registered procedures of the stock factor diagnostics (#506) on generated panels with known answers: a planted
rank IC is recovered inside its interval, a pure-noise panel with a market factor is rejected at close to the nominal
rate (measured here, over many seeds), the randomisation p is roughly uniform under the null, and the source's
stock-relabelling null (not used) over-rejects on the same panels. No price is read: arrays only."""

import math

import numpy as np
import pytest

from finresearch.factordiag import specs, stats, synth


def _planted_ics(rng: np.random.Generator, rho: float, months: int = 150, names: int = 50) -> np.ndarray:
    """Monthly rank ICs of a bivariate-normal panel with Pearson correlation rho, independent across months."""
    s = rng.normal(size=(months, names))
    r = rho * s + math.sqrt(1 - rho * rho) * rng.normal(size=(months, names))
    return np.array([stats.spearman(a, b) for a, b in zip(s, r, strict=True)])


def _expected_spearman(rho: float, n: int) -> float:
    """E[r_s] of n bivariate-normal pairs: 6 / (pi (n + 1)) x (asin(rho) + (n - 2) asin(rho / 2)) (Moran 1948,
    Biometrika 35; the large-n limit is Pearson's (6 / pi) asin(rho / 2))."""
    return 6 / (math.pi * (n + 1)) * (math.asin(rho) + (n - 2) * math.asin(rho / 2))


def test_expected_spearman_formula_has_the_known_limits() -> None:
    assert _expected_spearman(0.0, 50) == 0.0
    assert _expected_spearman(1.0, 50) == pytest.approx(1.0)  # asin(1) = pi/2, asin(1/2) = pi/6
    assert _expected_spearman(0.3, 10**7) == pytest.approx(6 / math.pi * math.asin(0.15), abs=1e-6)


@pytest.mark.parametrize("rho", [0.05, 0.10, -0.08])
def test_a_planted_rank_ic_is_recovered_inside_its_interval(rho: float) -> None:
    x = _planted_ics(np.random.default_rng(506), rho)
    ci = stats.block_interval(x, specs.BLOCK_MONTHS[1])
    want = _expected_spearman(rho, 50)
    assert ci["lo"] < want < ci["hi"], (rho, ci)
    assert (
        ci["hi"] - ci["lo"] < 0.08
    )  # and the interval is informative: about +/- 0.025 at 150 months of 50 names
    assert np.sign(float(x.mean())) == np.sign(rho)


def test_the_interval_covers_the_planted_ic_at_about_its_level_over_many_seeds() -> None:
    rng, rho, panels = np.random.default_rng(7), 0.10, 300
    want = _expected_spearman(rho, 50)
    hits = 0
    for _ in range(panels):
        ci = stats.block_interval(_planted_ics(rng, rho), specs.BLOCK_MONTHS[1])
        hits += ci["lo"] < want < ci["hi"]
    # 95 % nominal; the binomial sd at 300 panels is 1.3 points, so three sds either side
    assert 0.91 <= hits / panels <= 0.99, hits / panels


def test_a_planted_ic_is_detected_and_a_null_one_is_not() -> None:
    rng = np.random.default_rng(11)
    strong, null = _planted_ics(rng, 0.10), _planted_ics(rng, 0.0)
    assert stats.sign_flip_p(strong, 12)["p"] < 0.01  # 13 blocks: the floor is 2 / 8192
    assert stats.sign_flip_p(null, 12)["p"] > 0.05
    mde = stats.block_interval(null, 12)["mde"]
    assert (
        0.02 < mde < 0.06
    )  # about (2.18 + 0.87) x 0.14 / sqrt(150) x sqrt(12/12): the panel cannot see IC 0.01


# --------------------------------------------------------------------------------------------- the synthetic null
def test_the_generator_follows_the_registered_recipe() -> None:
    scores, forward = synth.null_panel(np.random.default_rng(1))
    assert set(scores) == set(synth.SCORES) and set(forward) == set(specs.HORIZONS)
    assert all(s.shape == (151, 50) for s in scores.values())
    assert {h: f.shape for h, f in forward.items()} == {
        1: (151, 50),
        3: (149, 50),
        6: (146, 50),
        12: (140, 50),
    }
    c = specs.CALIBRATION
    assert (c["panels"], c["seed"], c["nominal"], c["horizons"]) == (2000, 506, 0.05, specs.HORIZONS)
    assert "151 decision months" in c["recipe"] and "50 names" in c["recipe"] and "75th" in c["recipe"]


def test_the_vectorised_ic_equals_the_registered_spearman() -> None:
    scores, forward = synth.null_panel(np.random.default_rng(2))
    fast = synth.ic_series(scores["mom"], forward[3])
    slow = [stats.spearman(s, f) for s, f in zip(scores["mom"], forward[3], strict=False)]
    assert len(fast) == 149 and fast.tolist() == pytest.approx(slow, abs=1e-12)


@pytest.fixture(scope="module")
def measured() -> dict:
    return synth.measure(300, 2026)  # a fresh stream, not the registered seed: the property, not the table


def test_a_noise_panel_is_rejected_at_close_to_the_nominal_rate(measured: dict) -> None:
    """300 null panels: the binomial sd of a 5 % rate is 1.26 points, so [1 %, 10 %] is about +/- 4 sd around the
    registered 3.1 to 6.4 %. The measured rates are printed by `python -m finresearch.factordiag.synth`."""
    for key in ("sign_flip", "interval_misses_zero", "family_b"):
        for score, rates in measured[key].items():
            assert all(0.01 <= r <= 0.10 for r in rates), (key, score, rates)
    flat = [r for by in measured["sign_flip"].values() for r in by]
    assert 0.03 <= sum(flat) / len(flat) <= 0.07  # 12 cells x 300 panels: the average is tight around 5 %


def test_newey_west_over_rejects_on_overlapping_horizons_which_is_why_it_gives_no_p(measured: dict) -> None:
    nw, flip = measured["newey_west_normal"], measured["sign_flip"]
    for score in ("lowvol", "mom"):  # persistent scores, 12-month windows: registered 13 to 15 %
        assert nw[score][3] > 0.09 and nw[score][3] > 1.5 * flip[score][3], (score, nw[score], flip[score])
    assert all(nw[s][0] < 0.11 for s in synth.SCORES)  # at one month nothing overlaps: only mildly liberal


def _ks_uniform(p: list[float]) -> float:
    """The Kolmogorov-Smirnov distance of a sample from the uniform distribution on (0, 1)."""
    x = np.sort(np.asarray(p))
    n = len(x)
    return float(max(np.max(np.arange(1, n + 1) / n - x), np.max(x - np.arange(n) / n)))


def test_the_randomisation_p_values_are_roughly_uniform_under_the_null() -> None:
    """400 null panels. The 1 % critical KS distance at n = 400 is 1.63 / 20 = 0.082; a p on a grid of 2 / 2^q adds at
    most one grid step (8 blocks at six months: 0.008), so 0.10 is the bound for every cell."""
    rng = np.random.default_rng(31)
    a: dict[tuple[str, int], list[float]] = {
        ("iid", 1): [],
        ("lowvol", 1): [],
        ("mom", 6): [],
        ("lowvol", 3): [],
    }
    b: dict[tuple[str, int], list[float]] = {("lowvol", 1): [], ("mom", 3): []}
    for _ in range(400):
        scores, forward = synth.null_panel(rng)
        for name, h in a:
            a[name, h].append(
                stats.sign_flip_p(synth.ic_series(scores[name], forward[h]), specs.BLOCK_MONTHS[h])["p"]
            )
        for name, h in b:
            x = synth.ic_series(scores[name], forward[h])
            b[name, h].append(stats.period_permutation_p(x[:75], x[75:], specs.BLOCK_MONTHS[h])["p"])
    for key, ps in {**{("A", *k): v for k, v in a.items()}, **{("B", *k): v for k, v in b.items()}}.items():
        assert _ks_uniform(ps) < 0.10, (key, _ks_uniform(ps))
        assert 0.44 <= float(np.mean(ps)) <= 0.56, (
            key,
            float(np.mean(ps)),
        )  # a uniform p has mean 1/2 (sd 0.014)


def test_the_ks_helper_on_known_samples() -> None:
    grid = [(i + 0.5) / 100 for i in range(100)]  # the midpoints: the best a sample of 100 can do, D = 0.005
    assert _ks_uniform(grid) == pytest.approx(0.005)
    assert _ks_uniform([0.9] * 10) == pytest.approx(
        0.9
    )  # all mass at 0.9: the empirical CDF is 0 just below it


def _relabel_p(
    score: np.ndarray, forward: np.ndarray, block: int, rng: np.random.Generator, draws: int = 99
) -> float:
    """The source's null, which is NOT registered: one relabelling of the stocks per block of dates."""
    a = np.argsort(np.argsort(score[: len(forward)], axis=1), axis=1).astype(float)
    b = np.argsort(np.argsort(forward, axis=1), axis=1).astype(float)
    a -= a.mean(axis=1, keepdims=True)
    b -= b.mean(axis=1, keepdims=True)
    a /= np.sqrt((a * a).sum(axis=1, keepdims=True))
    b /= np.sqrt((b * b).sum(axis=1, keepdims=True))
    obs, count = abs(float((a * b).sum())), 0
    for _ in range(draws):
        tot = sum(
            float((a[sl][:, rng.permutation(a.shape[1])] * b[sl]).sum()) for sl in stats.blocks(len(a), block)
        )
        count += abs(tot) >= obs
    return (1 + count) / (1 + draws)


def test_relabelling_the_stocks_over_rejects_when_a_score_is_tied_to_beta() -> None:
    """Why the registration departs from the source: with a market factor and a score tied to beta the relabelled ICs
    are far less variable than real ones, so a true null is rejected three to four times too often (600 panels: 16
    to 17 % at one month, 20 to 22 % at twelve; the registered 400-panel figures were 21 and 24 %, ADDENDUM.md note
    1). The registered sign-flip, on the very same panels, stays near 5 %. 300 panels: the binomial sd of a 17 %
    rate is 2.2 points, so 10 % is three sds below it."""
    rng, panels = np.random.default_rng(41), 300
    relabel, flip = {1: 0, 12: 0}, {1: 0, 12: 0}
    for _ in range(panels):
        scores, forward = synth.null_panel(rng)
        for h in relabel:
            relabel[h] += _relabel_p(scores["lowvol"], forward[h], max(12, 3 * h), rng) <= 0.05
            x = synth.ic_series(scores["lowvol"], forward[h])
            flip[h] += stats.sign_flip_p(x, specs.BLOCK_MONTHS[h])["p"] <= 0.05
    for h in relabel:
        assert relabel[h] / panels > 0.10 and relabel[h] > 2 * flip[h], (
            h,
            relabel[h] / panels,
            flip[h] / panels,
        )
        assert flip[h] / panels < 0.09, (h, flip[h] / panels)

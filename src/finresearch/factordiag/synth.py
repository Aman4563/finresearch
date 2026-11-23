"""The synthetic NULL of the registered calibration (#506, specs.CALIBRATION): generated arrays only, no file read.

Returns share a market factor, r = beta m + sigma e with E[m] = 0, so no score has any ranking skill, and one score is
persistent and tied to beta. `measure` counts how often each registered procedure rejects at a nominal 5 %; the
generator draws from its random stream in the order the registration states, so

    uv run python -m finresearch.factordiag.synth 2000 506

reprints the registered table (under a minute).
"""

from __future__ import annotations

import math
import sys

import numpy as np

from finresearch.factordiag import specs, stats

NAMES, DECISIONS, BEFORE, AFTER = 50, 151, 13, 12
SPLIT_AFTER = 75  # family B: the first 75 decision months against the rest
SCORES = ("lowvol", "mom", "iid")
Z95 = 1.959963984540054  # the two-sided 5 % normal critical value


def null_panel(rng: np.random.Generator) -> tuple[dict[str, np.ndarray], dict[int, np.ndarray]]:
    """One panel of the recipe: scores[name] is decision months x names; forward[h] holds the h-month return of the
    first 152 - h decision months (sums of monthly returns)."""
    months = BEFORE + DECISIONS + AFTER
    beta = rng.uniform(0.6, 1.5, NAMES)
    sigma = 0.05 + 0.04 * (beta - 0.6) / 0.9 + rng.normal(0, 0.005, NAMES)
    market = rng.normal(0, 0.055, months)
    r = beta[None, :] * market[:, None] + rng.normal(0, 1, (months, NAMES)) * sigma[None, :]
    ar = np.zeros((months, NAMES))
    ar[0] = rng.normal(0, 0.15, NAMES)
    for t in range(1, months):
        ar[t] = 0.95 * ar[t - 1] + rng.normal(0, 0.15 * math.sqrt(1 - 0.95**2), NAMES)
    c = np.cumsum(r, axis=0)
    d = np.arange(BEFORE, BEFORE + DECISIONS)
    scores = {"lowvol": -(beta[None, :] + ar[d]), "mom": (c[d - 1] - c[d - 12]) / sigma[None, :],
              "iid": rng.normal(0, 1, (DECISIONS, NAMES))}  # fmt: skip
    forward = {h: (c[d + h] - c[d])[: DECISIONS - h + 1] for h in specs.HORIZONS}
    return scores, forward


def ic_series(score: np.ndarray, forward: np.ndarray) -> np.ndarray:
    """The monthly rank IC of one score against one horizon's forward returns (rows are months). Generated values
    are continuous, so no two tie and ordinal ranks equal the average ranks of stats.spearman (a test compares the
    two); whole rows are ranked at once for speed."""
    a = np.argsort(np.argsort(score[: len(forward)], axis=1), axis=1).astype(float)
    b = np.argsort(np.argsort(forward, axis=1), axis=1).astype(float)
    a -= a.mean(axis=1, keepdims=True)
    b -= b.mean(axis=1, keepdims=True)
    return (a * b).sum(axis=1) / np.sqrt((a * a).sum(axis=1) * (b * b).sum(axis=1))


def measure(panels: int, seed: int) -> dict[str, dict[str, list[float]]]:
    """The share of null panels rejected at a nominal 5 %, per procedure and score, in specs.HORIZONS order (family B
    at its two horizons): `sign_flip` (family A's p <= 0.05), `interval_misses_zero` (the 95 % block interval),
    `newey_west_normal` (|t| beyond 1.96) and `family_b` (its p <= 0.05)."""
    rng = np.random.default_rng(seed)
    keys = ("sign_flip", "interval_misses_zero", "newey_west_normal", "family_b")
    hits = {k: {s: [0] * len(specs.FAMILY_B_HORIZONS if k == "family_b" else specs.HORIZONS) for s in SCORES}
            for k in keys}  # fmt: skip
    for _ in range(panels):  # one panel per pass: nothing else is drawn from the stream
        scores, forward = null_panel(rng)
        for name in SCORES:
            for j, h in enumerate(specs.HORIZONS):
                x = ic_series(scores[name], forward[h])
                size = specs.BLOCK_MONTHS[h]
                hits["sign_flip"][name][j] += stats.sign_flip_p(x, size)["p"] <= 0.05
                ci = stats.block_interval(x, size)
                hits["interval_misses_zero"][name][j] += not ci["lo"] <= 0.0 <= ci["hi"]
                hits["newey_west_normal"][name][j] += abs(stats.newey_west(x, specs.NW_LAG[h])["t"]) >= Z95
                if h in specs.FAMILY_B_HORIZONS:
                    p = stats.period_permutation_p(x[:SPLIT_AFTER], x[SPLIT_AFTER:], size)["p"]
                    hits["family_b"][name][specs.FAMILY_B_HORIZONS.index(h)] += p <= 0.05
    return {k: {s: [n / panels for n in v] for s, v in by.items()} for k, by in hits.items()}


def main(argv: list[str]) -> None:
    panels = int(argv[0]) if argv else 200
    seed = int(argv[1]) if len(argv) > 1 else specs.CALIBRATION["seed"]
    rates = measure(panels, seed)
    print(f"synthetic null, {panels} panels, seed {seed}: share rejected at a nominal 5 %")
    for k, by in rates.items():
        hs = specs.FAMILY_B_HORIZONS if k == "family_b" else specs.HORIZONS
        print(f"{k} (horizons {', '.join(str(h) for h in hs)} months)")
        for s, v in by.items():
            print(f"  {s:7}" + "".join(f"{x:9.4f}" for x in v))


if __name__ == "__main__":
    main(sys.argv[1:])

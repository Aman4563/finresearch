"""The IPO ranges' pinball loss and 80 % coverage per walk-forward year, with issue #378's acceptance (d)
(skill-monitor-v2, registered in skillmon/specs_v2.py `IpoIntervals`).

Per test year of evals.ipo_model's walk-forward (its own split, fits and table forecaster are called here; its
`walk_forward` and the committed artefact are untouched), two forecasters: the quantile regression's q10/q50/q90
("model") and the base-rate cell's empirical 10/50/90 % returns ("table", the range the IPO card shows while the model
has not passed its gate). Pinball loss L_tau(q, y) = (y - q)(tau - 1{y < q}) (stats.pinball), descriptive; coverage
= the share of issues with q10 <= y <= q90 and its exact binomial p (horserace.scores.band_coverage). (d): within
±7 pp of 80 % (decided in integers) in >= 5 of the 7 complete years; a skipped year is insufficient data, never
within. Read only: `evaluate` takes rows (`finresearch forecasts ipo-intervals` loads them)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from finresearch.evals import ipo_model as im
from finresearch.horserace import scores as sc
from finresearch.skillmon import prereg, specs_v2
from finresearch.skillmon import stats as st

MIN_TRAIN = 30  # evals.ipo_model.walk_forward skips a year with fewer training rows (its `len(train) < 30`)


def _key(tau: float) -> str:
    return f"q{int(tau * 100)}"  # the walk-forward's own keys: q10, q50, q90


def within(inside: int, n: int) -> bool:
    """|100 inside - 80 n| <= 7 n: integers, so 73 of 100 is within (0.8 - 0.73 is 0.0700...06 in floats)."""
    i = specs_v2.IPO
    return n > 0 and abs(100 * inside - i.nominal_pct * n) <= i.tolerance_pp * n


def year_scores(q: Mapping[str, Sequence[float]], y: Sequence[float]) -> dict[str, Any]:
    """One year of one forecaster: mean pinball loss per tau and over the taus, the p10-p90 coverage and (d)'s mark."""
    i, n = specs_v2.IPO, len(y)
    pin = {_key(t): sum(st.pinball(a, b, t) for a, b in zip(q[_key(t)], y, strict=True)) / n for t in i.taus}
    cov = sc.band_coverage(q["q10"], q["q90"], y, i.nominal_pct / 100)
    return {"n": n, "pinball": pin, "pinball_mean": sum(pin.values()) / len(pin), "inside": cov["inside"],
            "coverage": cov["share"], "p": cov["p"], "within": within(cov["inside"], n)}  # fmt: skip


def acceptance(years: Mapping[int, Mapping[str, Any] | None]) -> dict[str, Any]:
    """(d) for one forecaster over the registered complete years: pass, fail or insufficient data (never assumed)."""
    i = specs_v2.IPO
    marks = {y: "insufficient data" if not (s := years.get(y)) or not s.get("n") else
             "within" if s["within"] else "outside" for y in i.years}  # fmt: skip
    k, u = sum(m == "within" for m in marks.values()), sum(m == "insufficient data" for m in marks.values())
    verdict = "pass" if k >= i.pass_years else "fail" if k + u < i.pass_years else "insufficient data"
    return {"verdict": verdict, "within": k, "insufficient": u, "years": marks,
            "rule": f"within ±{i.tolerance_pp} pp of {i.nominal_pct} % in at least {i.pass_years} of the "
                    f"{len(i.years)} complete years {i.years[0]}-{i.years[-1]}"}  # fmt: skip


def evaluate(rows: Sequence[Any]) -> dict[str, Any]:
    """Every walk-forward year's scores for both forecasters, and (d) for each (module docstring)."""
    i = specs_v2.IPO
    data, keys = im.usable(rows), [_key(t) for t in i.taus]
    years: list[dict[str, Any]] = []
    for t in im.TEST_YEARS:
        train, test = [r for r in data if im._year(r) < t], [r for r in data if im._year(r) == t]
        fold: dict[str, Any] = {"year": t, "n_train": len(train), "n_test": len(test)}
        if len(train) < MIN_TRAIN or not test:
            years.append({**fold, "skipped": "too few rows (the walk-forward's own rule): insufficient data"})
            continue
        pred = im.predict(im.fit(train), [im.raw_features(r) for r in test])
        table = [x[1] for x in map(im.table_forecaster(train), test)]
        y = [float(im._f(r, "return_open")) for r in test]  # type: ignore[arg-type]
        fold["model"] = year_scores({k: [float(v) for v in pred[k]] for k in keys}, y)
        fold["table"] = year_scores({k: [x[k] for x in table] for k in keys}, y)
        years.append(fold)
    by = {f["year"]: f for f in years}
    return {"version": specs_v2.SPEC_VERSION, "registration_hash": prereg.V2_HASH, "n_rows": len(data),
            "years": years, "partial": [y for y in im.TEST_YEARS if y not in i.years],
            "acceptance": {name: acceptance({y: by.get(y, {}).get(name) for y in i.years}) for name in i.forecasters},
            "seen": i.seen}  # fmt: skip

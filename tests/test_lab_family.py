"""#380 (P2.5): the Lab's trial family. The matrix takes each spec's longest done trial with a daily P&L vector and keeps
only the sessions every one of them covers (a day a trial did not replay is never filled with 0); PBO, SPA and
Romano-Wolf run on it with the registered settings (stratlab.rigour.FAMILY)."""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
from test_stratlab import lab_db  # noqa: F401  (fixture)


def _add(spec_hash: str, start: date, pnl: list[float], status: str = "done", mode: str = "is") -> None:
    from finresearch.db import session_scope
    from finresearch.db.models import LabTrial

    daily = [[(start + timedelta(days=i)).isoformat(), x] for i, x in enumerate(pnl)]
    with session_scope() as s:
        s.add(LabTrial(spec_hash=spec_hash, underlying="NIFTY", mode=mode, status=status, daily=daily))


def test_the_matrix_keeps_common_days_and_the_longest_trial_per_spec(lab_db):  # noqa: F811
    from finresearch.db import session_scope
    from finresearch.stratlab import family

    d0 = date(2024, 1, 1)
    _add("a", d0, [1, 2, 3, 4, 5])
    _add("a", d0, [1, 2, 3, 4, 5, 6, 7])  # the longer trial of the same spec replaces the shorter
    _add("b", d0 + timedelta(days=2), [10, 20, 30, 40, 50, 60])  # covers day 2 to day 7
    _add("c", d0, [], status="failed")  # no vector: counted in N, not in the matrix
    with session_scope() as s:
        m = family.matrix(s)
    assert m.hashes == ["a", "b"]
    assert m.days == [(d0 + timedelta(days=i)).isoformat() for i in range(2, 7)]
    assert m.values.tolist() == [[3, 10], [4, 20], [5, 30], [6, 40], [7, 50]]
    assert m.without_vector == ["c"]


def test_family_statistics_are_unknown_on_a_short_grid():
    from finresearch.stratlab import family

    m = family.Matrix(hashes=["a", "b"], days=["d"] * 10, values=np.ones((10, 2)), without_vector=[])
    got = family.statistics(m)
    assert (
        got["pbo"]["pbo"] is None
        and got["spa"]["p_consistent"] is None
        and got["romano_wolf"]["rejected"] is None
    )
    assert "fewer than" in got["note"]


def test_family_statistics_find_a_planted_strategy_and_not_noise():
    from finresearch.stratlab import family

    rng = np.random.default_rng(11)
    noise = rng.normal(0, 1, size=(800, 20))
    m = family.Matrix(hashes=[f"h{i}" for i in range(20)], days=[str(i) for i in range(800)], values=noise,
                      without_vector=[])  # fmt: skip
    quiet = family.statistics(m)
    assert quiet["spa"]["p_consistent"] > 0.05 and quiet["romano_wolf"]["rejected"] == []
    planted = noise.copy()
    planted[:, 3] += 0.3  # daily SR 0.3: t = 8.5 over 800 days
    loud = family.statistics(family.Matrix(m.hashes, m.days, planted, []))
    assert loud["spa"]["p_consistent"] < 0.01 and "h3" in loud["romano_wolf"]["rejected_hashes"]
    assert loud["pbo"]["pbo"] < 0.05
    assert loud["settings"]["mean_block"] == 10.0 and loud["settings"]["draws"] == 1000
    again = family.statistics(
        family.Matrix(m.hashes, m.days, planted, [])
    )  # fixed seed: the same p every time
    assert again["spa"]["p_consistent"] == loud["spa"]["p_consistent"]

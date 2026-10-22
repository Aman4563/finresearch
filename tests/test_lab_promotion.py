"""#380 (P2.5): forward incubation is the only promotion path. 'incubation passed' needs EVERY check: the forward
holdout's candidate status, PBO < 0.2, SPA p < 0.05, a forward paper record with the library's trade count and t, and
MinTRL met; an unknown value always fails, and only stratlab.promotion can emit the passing status."""

from __future__ import annotations

import math
from datetime import date, timedelta
from pathlib import Path

import pytest
from test_stratlab import lab_db, mem, spec  # noqa: F401  (fixtures)

from finresearch.stratlab import promotion as pm

E0 = date(2025, 1, 2)
GOOD = dict(holdout="candidate", pbo=0.05, spa_p=0.01,
            nets=[300.0 + 40 * math.sin(i) for i in range(40)],
            expiries=[E0 + timedelta(days=7 * i) for i in range(40)], registered=True)  # fmt: skip


def _status(**kw) -> tuple[str, dict]:
    got = pm.checks(**{**GOOD, **kw})
    return got["status"], {c["criterion"]: c for c in got["checks"]}


def test_every_check_passing_is_the_only_way_to_incubation_passed():
    st, c = _status()
    assert st == pm.PASSED == "incubation_passed" and all(x["pass"] for x in c.values())
    assert set(c) == {"Registration", "Forward holdout", "PBO", "SPA p (consistent)", "Forward paper trades",
                      "Forward paper t-stat (expiry-clustered)", "MinTRL (trades)"}  # fmt: skip


@pytest.mark.parametrize(("kw", "criterion"), [
    (dict(holdout=None), "Forward holdout"), (dict(holdout="too_early"), "Forward holdout"),
    (dict(holdout="fails"), "Forward holdout"), (dict(pbo=None), "PBO"), (dict(pbo=0.2), "PBO"),
    (dict(spa_p=None), "SPA p (consistent)"), (dict(spa_p=0.05), "SPA p (consistent)"),
    (dict(nets=GOOD["nets"][:29], expiries=GOOD["expiries"][:29]), "Forward paper trades"),
    (dict(registered=False), "Registration"),
])  # fmt: skip
def test_any_failed_or_unknown_check_keeps_no_proven_edge(kw, criterion):
    st, c = _status(**kw)
    assert st == "no_proven_edge" and c[criterion]["pass"] is False


def test_unknown_reads_unknown_never_zero():
    _, c = _status(pbo=None, spa_p=None)
    assert c["PBO"]["value"] is None and "unknown" in c["PBO"]["rule"]
    assert c["SPA p (consistent)"]["value"] is None


def test_paper_t_and_min_trl_by_hand():
    # 30 trades alternating 100 and -60 (cluster = one expiry each): mean 20, sample sd = 80 * sqrt(30/29) = 81.37;
    # clustered t = mean / sqrt(G/(G-1) * sum(e^2) / n^2) = 20 / sqrt(30/29 * 30 * 6400 / 900) = 1.3463 < 2 -> fails
    nets = [100.0 if i % 2 == 0 else -60.0 for i in range(30)]
    st, c = _status(nets=nets, expiries=GOOD["expiries"][:30])
    assert c["Forward paper t-stat (expiry-clustered)"]["value"] == pytest.approx(1.3463, abs=1e-4)
    assert st == "no_proven_edge"
    # MinTRL per trade: SR = 20 / 81.37 = 0.2458; deviations +-80 are symmetric two-point: skew 0, kurtosis 1, so
    # MinTRL = 1 + (1 - 0 + (1 - 1)/4 SR^2) (1.6449 / 0.2458)^2 = 1 + 44.78 = 45.8 trades > 30 -> fails
    sr = 20 / (80 * math.sqrt(30 / 29))
    need = 1 + (1.6448536 / sr) ** 2
    assert (
        c["MinTRL (trades)"]["value"] == pytest.approx(need, rel=1e-4)
        and c["MinTRL (trades)"]["pass"] is False
    )


def test_the_passing_status_is_written_only_by_the_promotion_module():
    src = Path(pm.__file__).resolve().parents[1]
    hits = [p for p in src.rglob("*.py") if '"incubation_passed"' in p.read_text()]
    assert hits == [Path(pm.__file__).resolve()]


def test_judge_from_the_database_is_no_proven_edge_without_a_paper_record(lab_db, mem):  # noqa: F811
    from finresearch.db import session_scope
    from finresearch.stratlab import store

    with session_scope() as s:
        v1 = store.create_strategy(s, "", spec(), mem).versions[0].id
        got = pm.judge(s, v1)
    c = {x["criterion"]: x for x in got["checks"]}
    assert got["status"] == "no_proven_edge"
    assert c["Forward holdout"]["value"] is None and c["Forward paper trades"]["value"] == 0
    assert c["PBO"]["value"] is None  # one spec: no family to test


def test_the_rigour_registration_is_pinned_and_quoted_in_the_prereg():
    from finresearch.stratlab import rigour, rigour_prereg

    assert rigour.registration_hash() == rigour_prereg.HASH
    text = (Path(__file__).resolve().parents[1] / "evals/experiments/lab_rigour/PREREG.md").read_text()
    g = rigour.GATE
    assert rigour_prereg.HASH in text and f"PBO < {g.max_pbo:g}" in text and f"p < {g.max_spa_p:g}" in text


def test_a_changed_registration_fails_the_gate(monkeypatch):
    from finresearch.stratlab import rigour_prereg

    monkeypatch.setattr(rigour_prereg, "HASH", "0" * 64)
    assert pm.checks(**{**GOOD, "registered": False})["status"] == "no_proven_edge"

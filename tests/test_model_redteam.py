"""#379 second slice: red-team reviews are filed as evals/redteam/<entry>/<date>-<slug>.md with the PREREG's commit and
its timestamp; the inventory reads them, a malformed file is reported (never silently dropped), and a tier-A model
cannot read validated without a filed review."""

from __future__ import annotations

from dataclasses import replace
from datetime import date

from finresearch.inventory import redteam, registry

GOOD = """---
model: fno_strategy_gate
reviewed_on: 2026-10-23
reviewer: separate Claude session, given only the PREREG and the code
prereg: evals/experiments/card_ledger/PREREG.md
prereg_commit: 0123456789abcdef0123456789abcdef01234567
prereg_committed_at: 2026-10-20T10:00:00+05:30
verdict: changes-needed
---
# Findings
1. Something to fix.
"""


def _file(tmp_path, rel: str, body: str):
    p = tmp_path / "evals" / "redteam" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)
    return p


def test_a_filed_review_is_read_with_its_prereg_commit_and_timestamp(tmp_path):
    _file(tmp_path, "fno_strategy_gate/2026-10-23-first.md", GOOD)
    got = redteam.scan(tmp_path)
    assert not got.invalid
    (r,) = got.by_model["fno_strategy_gate"]
    assert (
        r.verdict == "changes-needed"
        and r.prereg_commit.startswith("0123")
        and r.reviewed_on == date(2026, 10, 23)
    )
    assert r.path == "evals/redteam/fno_strategy_gate/2026-10-23-first.md"


def test_a_malformed_or_misfiled_review_is_reported_not_dropped(tmp_path):
    _file(
        tmp_path,
        "fno_strategy_gate/2026-10-23-a.md",
        GOOD.replace("prereg_commit: 0123", "prereg_commit: zz"),
    )
    _file(tmp_path, "no_such_model/2026-10-23-b.md", GOOD.replace("fno_strategy_gate", "no_such_model"))
    _file(tmp_path, "direction_logistic/2026-10-23-c.md", GOOD)  # folder and front matter disagree
    got = redteam.scan(tmp_path)
    assert not got.by_model and len(got.invalid) == 3
    assert any("prereg_commit" in w for _, w in got.invalid) and any(
        "no inventory entry" in w for _, w in got.invalid
    )
    assert any("folder" in w for _, w in got.invalid)


def test_the_repository_s_filed_reviews_are_all_valid():
    got = redteam.scan()
    assert not got.invalid, got.invalid


def test_a_tier_a_model_is_not_validated_without_a_filed_review():
    gate = registry.entry("fno_strategy_gate")
    ok = replace(gate, declared="validated", validated_on=date(2026, 10, 1), valid_until=date(2027, 1, 1))
    st, why = ok.status(date(2026, 10, 22), reviews=0)
    assert st == "unvalidated" and "red-team review" in why
    assert ok.status(date(2026, 10, 22), reviews=1)[0] == "validated"
    tier_b = replace(registry.entry("ipo_blend"), declared="validated", validated_on=date(2026, 10, 1),
                     valid_until=date(2027, 1, 1))  # fmt: skip
    assert (
        tier_b.status(date(2026, 10, 22), reviews=0)[0] == "validated"
    )  # B needs calibration, not a red team


def test_scaffold_files_a_review_with_the_prereg_s_commit_and_needs_a_verdict(tmp_path):
    import subprocess

    def git(*a):
        subprocess.run(["git", *a], cwd=tmp_path, check=True, capture_output=True,
                       env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t",
                            "GIT_COMMITTER_EMAIL": "t@example.invalid", "PATH": "/usr/bin:/bin:/usr/local/bin",
                            "GIT_AUTHOR_DATE": "2026-10-20T10:00:00+05:30",
                            "GIT_COMMITTER_DATE": "2026-10-20T10:00:00+05:30"})  # fmt: skip

    pre = tmp_path / "evals" / "experiments" / "x" / "PREREG.md"
    pre.parent.mkdir(parents=True)
    pre.write_text("# registered\n")
    git("init", "-q")
    git("add", ".")
    git("commit", "-qm", "register")
    p = redteam.scaffold("fno_strategy_gate", "evals/experiments/x/PREREG.md", today=date(2026, 10, 23),
                         slug="first", root=tmp_path)  # fmt: skip
    assert p == tmp_path / "evals/redteam/fno_strategy_gate/2026-10-23-first.md"
    text = p.read_text()
    assert "prereg_committed_at: 2026-10-20T10:00:00+05:30" in text and "signals/fno.py" in text
    got = redteam.scan(tmp_path)  # unfinished: the verdict placeholder keeps CI red until it is filled in
    assert not got.by_model and "verdict" in got.invalid[0][1]
    (tmp_path / p).write_text(text.replace("verdict: TODO", "verdict: do-not-adopt"))
    assert redteam.scan(tmp_path).by_model["fno_strategy_gate"][0].prereg_commit

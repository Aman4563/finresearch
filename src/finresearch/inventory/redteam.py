"""Red-team reviews of the inventory's models (#379, the "challenge substitute"): every adoption decision gets a review
by a separate Claude session given only the PREREG and the code, filed beside the PREREG commit's timestamp.

Where: evals/redteam/<entry key>/<YYYY-MM-DD>-<slug>.md (a recipe's key `recipe:x` is the folder `recipe-x`), a small
front matter then the findings (evals/redteam/README.md). `scan` reads them; a file that is malformed, names an
unknown entry or sits in another entry's folder is reported in `invalid`, never silently dropped (CI keeps the
repository's set valid: tests/test_model_redteam.py). A tier-A model cannot read validated without one
(registry.ModelEntry.status). Read only: nothing here writes a review.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from finresearch.config import REPO_ROOT

DIR = Path("evals") / "redteam"
FIELDS = ("model", "reviewed_on", "reviewer", "prereg", "prereg_commit", "prereg_committed_at", "verdict")
VERDICTS = ("adopt", "do-not-adopt", "changes-needed")
_SHA = re.compile(r"[0-9a-f]{7,40}")


@dataclass(frozen=True)
class Review:
    model: str
    reviewed_on: date
    reviewer: str
    prereg: str
    prereg_commit: str
    prereg_committed_at: str
    verdict: str
    path: str

    def to_json(self) -> dict:
        return {"model": self.model, "reviewed_on": self.reviewed_on.isoformat(), "reviewer": self.reviewer,
                "prereg": self.prereg, "prereg_commit": self.prereg_commit,
                "prereg_committed_at": self.prereg_committed_at, "verdict": self.verdict, "path": self.path}  # fmt: skip


@dataclass
class Scan:
    by_model: dict[str, list[Review]] = field(default_factory=dict)
    invalid: list[tuple[str, str]] = field(default_factory=list)  # (path, why)


def folder(key: str) -> str:
    return key.replace(":", "-")


def _front(text: str) -> dict[str, str] | None:
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---" or "---" not in [x.strip() for x in lines[1:]]:
        return None
    end = 1 + [x.strip() for x in lines[1:]].index("---")
    out = {}
    for x in lines[1:end]:
        if ":" in x:
            k, v = x.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def _parse(path: Path, rel: str) -> Review | str:
    from finresearch.inventory.registry import entry

    fm = _front(path.read_text())
    if fm is None:
        return "no front matter (--- ... ---)"
    missing = [f for f in FIELDS if not fm.get(f)]
    if missing:
        return f"missing {', '.join(missing)}"
    if entry(fm["model"]) is None:
        return f"model {fm['model']!r} has no inventory entry"
    if path.parent.name != folder(fm["model"]):
        return f"filed in folder {path.parent.name!r} but reviews {fm['model']!r}"
    if not _SHA.fullmatch(fm["prereg_commit"]):
        return f"prereg_commit {fm['prereg_commit']!r} is not a git commit id"
    if fm["verdict"] not in VERDICTS:
        return f"verdict must be one of {', '.join(VERDICTS)}"
    try:
        reviewed = date.fromisoformat(fm["reviewed_on"])
        if datetime.fromisoformat(fm["prereg_committed_at"]).tzinfo is None:
            return "prereg_committed_at needs a time zone"
    except ValueError as e:
        return f"unreadable date: {e}"
    return Review(fm["model"], reviewed, fm["reviewer"], fm["prereg"], fm["prereg_commit"], fm["prereg_committed_at"],
                  fm["verdict"], rel)  # fmt: skip


def scan(root: Path = REPO_ROOT) -> Scan:
    out = Scan()
    base = root / DIR
    for p in sorted(base.glob("*/*.md")) if base.exists() else []:
        rel = p.relative_to(root).as_posix()
        got = _parse(p, rel)
        if isinstance(got, str):
            out.invalid.append((rel, got))
        else:
            out.by_model.setdefault(got.model, []).append(got)
    return out


def prereg_commit(root: Path, prereg: str) -> tuple[str, str]:
    """(commit, committer time ISO) of the commit that last changed the PREREG; raises when git cannot say."""
    import subprocess

    r = subprocess.run(["git", "log", "-1", "--format=%H %cI", "--", prereg], cwd=root, capture_output=True,
                       text=True, timeout=10)  # fmt: skip
    if r.returncode or not r.stdout.strip():
        raise ValueError(f"git has no commit for {prereg}: commit the PREREG before its review")
    sha, at = r.stdout.split()
    return sha, at


def scaffold(key: str, prereg: str, *, today: date, slug: str, root: Path = REPO_ROOT) -> Path:
    """Start a review file for `key`: the front matter with the PREREG's commit and time filled in, `verdict: TODO`
    (invalid until the reviewer fills it, so CI stays red on an unfinished review) and what the separate session
    may be given: the PREREG and the entry's code, nothing else."""
    from finresearch.inventory.registry import entry

    e = entry(key)
    if e is None:
        raise ValueError(f"no inventory entry {key!r}")
    if not (root / prereg).is_file():
        raise ValueError(f"{prereg} is not a file")
    sha, at = prereg_commit(root, prereg)
    path = root / DIR / folder(key) / f"{today.isoformat()}-{slug}.md"
    if path.exists():
        raise ValueError(f"{path.relative_to(root)} exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    given = "\n".join(f"- src/finresearch/{c}" for c in e.code)
    path.write_text(f"---\nmodel: {key}\nreviewed_on: {today.isoformat()}\nreviewer: TODO (the separate Claude session)\n"
                    f"prereg: {prereg}\nprereg_commit: {sha}\nprereg_committed_at: {at}\nverdict: TODO\n---\n"
                    f"# Red-team review: {e.name}\n\nGiven only:\n- {prereg} (commit {sha[:12]}, {at})\n{given}\n\n"
                    "## Findings\n\n1. \n")  # fmt: skip
    return path

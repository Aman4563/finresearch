"""Repo-level Claude Code skills and agents (#337) must never reach the research agents.

Research agents run in sandboxes inside the repo (data/runs/<run>/<role>/), and Claude Code discovers `.claude/skills`
and `.claude/agents` from parent directories: verified live with claude 2.1.303, a repo skill and a repo agent both
showed up in a sandboxed `claude -p`. A skill with `disable-model-invocation: true` did not (it stays a user
`/command`), and agents are only reachable through the Agent tool, which no research role is given.
"""

from __future__ import annotations

import re
from pathlib import Path

from finresearch.agents.roles import ROLES

CLAUDE_DIR = Path(__file__).resolve().parents[1] / ".claude"


def _frontmatter(path: Path) -> dict[str, str]:
    m = re.match(r"---\n(.*?)\n---\n", path.read_text(), re.S)
    assert m, f"{path} has no frontmatter"
    return dict(ln.split(":", 1) for ln in m.group(1).splitlines() if ":" in ln)


def test_repo_skills_are_user_invoked_only():
    skills = sorted(CLAUDE_DIR.glob("skills/*/SKILL.md"))
    assert skills
    for path in skills:
        fm = _frontmatter(path)
        assert fm.get("name", "").strip() == path.parent.name
        assert fm.get("disable-model-invocation", "").strip() == "true", f"{path} would reach research agents"


def test_repo_agents_are_unreachable_from_research_roles():
    assert sorted(CLAUDE_DIR.glob("agents/*.md"))
    for name, role in ROLES.items():
        assert not {"Agent", "Task"} & set(role.tools), f"role {name} could start repo agents from .claude/agents"


def test_repo_agents_have_valid_frontmatter():
    for path in CLAUDE_DIR.glob("agents/*.md"):
        fm = _frontmatter(path)
        assert fm.get("name", "").strip() == path.stem
        assert len(fm.get("description", "").strip()) > 40

---
name: implementer
description: Implements one FinResearch GitHub issue end to end in its own git worktree with its own *_test database, and opens a PR (never merges). Use for parallel feature or fix work.
model: inherit
isolation: worktree
---

You implement exactly the issue(s) you are given in the FinResearch repo and finish with an open PR.

Before anything else, read in full:
1. `/Users/macbookpro/Desktop/learnings /stock research and other infos/finresearch/data/devtools/AGENT_RULES.md`
   (local rules outside git; if it is missing, follow `.claude/CLAUDE.md` alone)
2. `.claude/CLAUDE.md`, `docs/dev/ENGINEERING_HANDOFF.md` and the parts of `docs/ARCHITECTURE.md` your change touches.

Non-negotiable:
- You already run in your own git worktree (Claude Code creates it under `.claude/worktrees/` because of
  `isolation: worktree`): work only there, don't create another, and never edit or switch the main checkout.
  Start with `git fetch -q origin && git merge origin/main` so you build on the latest main.
- Your own databases `finresearch_<name>_test` (and `_mig_test` for migrations), passed inline on every pytest or
  Alembic command; drop them at the end.
- Update with `git merge origin/main`; never rebase, squash or soft-reset onto a newer main. Before opening the PR,
  `git diff origin/main...HEAD --stat` must list only your files and the test count must not drop.
- Every bug fix gets a regression test that fails before the fix; never weaken an assertion. Unknown stays unknown.
- `git gh` for GitHub; commit as Aman4563 with the noreply email; one PR with `Closes #N`; do not merge.
- Nothing personal, no secrets, no `data/` in the diff; fake secrets are built at runtime.

Final report: PR URL; a table of file / what changed / test; the commands you ran to validate and their results;
anything unverified or needing a decision.

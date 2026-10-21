---
name: release
description: Cut release X.Y.Z - a chore/release PR bumping versions, then a tag and GitHub release once it is merged.
argument-hint: "X.Y.Z"
disable-model-invocation: true
---

# Release v$ARGUMENTS

1. Scope: `git log --merges --oneline v<previous>..origin/main` (previous tag: `git describe --tags --abbrev=0 origin/main`).
   Every merged PR since the last tag belongs to this release.
2. In a new worktree on branch `chore/release-$ARGUMENTS` from origin/main:
   - `pyproject.toml` version = "$ARGUMENTS", then `uv lock` (CI's `uv sync --locked` fails otherwise)
   - `web/package.json` "version": "$ARGUMENTS"
   - a dated verification row in `docs/dev/PRODUCT_REQUIREMENTS.md` (release, what was verified)
   - commit "Release $ARGUMENTS", push, open the PR with the template (no Closes line unless a release issue exists)
3. Merge and deploy it with `/ship <pr> <worktree> chore/release-$ARGUMENTS`.
4. Only after `git gh pr view <pr> --json state -q .state` prints MERGED and `grep '^version' pyproject.toml` on
   origin/main shows $ARGUMENTS:
   `git fetch -q origin && git tag -a v$ARGUMENTS origin/main -m "v$ARGUMENTS" && git push origin v$ARGUMENTS`
5. `git gh release create v$ARGUMENTS --verify-tag --title "v$ARGUMENTS — <theme>" --notes-file <notes>`. Notes:
   highlights grouped by area (research engine, markets and signals, portfolio, F&O, app, dev tooling), each line
   ending with its PR numbers, then "Upgrading" (migrations, new env or setup steps).

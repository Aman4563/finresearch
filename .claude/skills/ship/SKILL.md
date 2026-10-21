---
name: ship
description: Merge a green PR with a merge commit and deploy it to the live app through merge_deploy.sh, safely.
argument-hint: "<pr-number> <worktree-dir-name> <branch>"
disable-model-invocation: true
---

# Ship PR: $ARGUMENTS

`data/devtools/merge_deploy.sh` (in the main checkout, git-ignored) is the only way changes reach the live app. It
checks mergeability and the test-count guard, waits for CI, merges with a merge commit, pulls main, backs up and
migrates the live database when needed, rebuilds the web app and restarts :8710/:3100.

1. No research run is live: `ps -Ao command | grep -c '^claude -p.*--strict-mcp-config'` prints 0 (the restart would
   kill the run's API and the pull would change its tree). If one is live, stop and say so.
2. Review state: `git gh pr view <pr> --json title,state,mergeable,headRefName` and `git gh pr checks <pr>`.
   For report-engine changes the PR body must carry a gold-set summary. Offer `/code-review` first if nobody has
   reviewed the diff.
3. From the main checkout, run it bare (never piped: a pipe hides its exit code) with `run_in_background` (it waits
   for CI, up to ~20 min):
   `cd "<main checkout>" && data/devtools/merge_deploy.sh <pr> <worktree-dir-name> <branch>; echo "exit=$?"`
   Exit codes: 3 not mergeable, 4 CI not green, 5 the merge lowers the test count or deletes a test file (only the
   user may set ALLOW_TEST_DROP=1), 6 not merged.
4. Only on exit 0: `git gh pr view <pr> --json state -q .state` is MERGED, the last line shows `api 200 web 200`, and
   `curl -s http://127.0.0.1:8710/api/health` reports the expected version.
5. Report: merged commit, migration and backup (if any), health. Never chain a release after this with `&&`.

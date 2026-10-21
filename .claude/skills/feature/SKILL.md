---
name: feature
description: Turn a one-line requirement into a shipped FinResearch feature - interview, spec, edge-case sweep, tests first, build, hostile review, PR.
argument-hint: "<one-line requirement>"
disable-model-invocation: true
---

# Feature: $ARGUMENTS

Run the stages in order and stop at each "Gate" until it holds. This replaces superpowers' brainstorming for this
repo (do not interview twice); superpowers' planning, subagent and TDD skills may be used inside stages 5 and 6.

## 1. Interview (no code, no files yet)
Read `.claude/CLAUDE.md`, `docs/dev/ENGINEERING_HANDOFF.md` and the code the requirement touches first, so questions
are about decisions, not facts you can look up. Then ask at most 8 questions with AskUserQuestion, multiple choice,
recommended option first. Cover only what is genuinely open:
- users and surface: CLI, API, web page, monitor job, alert
- data source and its terms (official exchange/AMFI/RBI/FBIL file, Groww read-only, manual import); never a source
  that needs scraping against its terms or broker credentials with trading scope
- Indian rules that apply (tax section and effective dates, SEBI, settlement cycle)
- what the user sees when data is missing, stale or partial (unknown stays unknown)
- whether personal data is involved (it must never reach a model)
- scope cut: what is explicitly out of this version
Gate: the user answered, or said "use your defaults".

## 2. Spec and issue
In a new worktree (`git fetch -q origin && git worktree add ../wt-<slug> -b feat/<slug> origin/main`), write
`specs/<slug>.md`:
- Goal, non-goals, users and surfaces
- Data contracts: sources (URL, licence or terms, refresh), units, precision (Decimal), time zone (IST)
- Schema delta (tables, columns, indexes; migration needed?), API routes and payloads, UI states including
  loading, empty, partial and error
- Calculations: formulas with a cited source for every financial constant and rule (section, circular, date)
- Acceptance criteria as numbered Given / When / Then, each with concrete numbers
- Risks and open questions
Open the issue with `git gh issue create` (acceptance criteria copied from the spec, `area:*` and `type:*` labels),
and put its number at the top of the spec.
Gate: the user approves the spec (show the acceptance criteria and the schema delta, not the whole file).

## 3. Edge-case sweep
Walk `edge-cases.md` (next to this file) against the spec. For each item write one line in a `## Edge cases` table in
the spec: case, decision (test / handle / out of scope), and why. Add an acceptance criterion for every "test".

## 4. Tests first
Turn every acceptance criterion and every "test" edge case into failing tests: pytest (offline: respx or
`httpx.MockTransport`, the fake `claude` CLI, synthetic fixtures with made-up names and ISINs) and Playwright for UI.
Expected values are computed independently, by hand or from the cited source, and the working is written in a
comment next to each one. Run them on your own `*_test` database and confirm they fail for the right reason, then
commit the tests alone ("Add failing tests for <feature> (#N)") and record that commit's SHA.
Gate: every test fails, and fails on missing behaviour, not on a typo or import error.

## 5. Build
Implement until the tests pass, following the spec. Independent parts (for example backend calculation, API, web
page) may go to `implementer` subagents in their own worktrees, each given the spec, the issue and the files it owns.
Rules: Python computes, Decimal for money, IST helpers, `read_lines()` for documents, GETs never write, nothing
personal reaches a model, brokers stay read-only, and a migration goes through `/new-migration`'s steps.
Gate: `git diff <tests SHA>..HEAD -- <test files>` is empty (the implementation did not edit the tests), the full
suite and ruff pass, and web changes pass `pnpm typecheck && pnpm lint` and their e2e specs.

## 6. Hostile review
1. Ask the `invariant-auditor` agent to review the branch as a hostile quant auditor: find at least five ways the
   numbers could be wrong (look-ahead bias, survivorship, 252 vs 365 annualisation, sign errors, FIFO lot errors,
   Decimal rounding, stale prices treated as current, unknown shown as zero, tax-date boundaries), each with
   file:line and a reproducing test, or say plainly that it found none. It must not invent findings.
2. Report-engine changes: ask the `gold-reviewer` agent for the gold-set score.
3. Run `/code-review` on the diff.
Fix every confirmed finding with a regression test; list the rest as "needs decision".

## 7. Hand-off
Append a dated row to `docs/dev/PRODUCT_REQUIREMENTS.md`, merge origin/main into the branch (never rebase), push and
open the PR from the template with `Closes #N`, the acceptance criteria checked off, the review results and anything
unverified. Do not merge: tell the user to run `/ship <pr> wt-<slug> feat/<slug>`.

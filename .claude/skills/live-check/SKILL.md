---
name: live-check
description: Live-check the research engine on one instrument (Orient Cables for IPOs) with budget checks, smoke tests, a monitored run and a gold-set score.
argument-hint: "[ipo|stock|fund|bond] [--streams a,b]"
disable-model-invocation: true
---

# Live check: $ARGUMENTS

Spends real Claude Max usage: a full IPO run uses most of a 5-hour window. One instrument per asset class only.

| kind  | instrument                         | start command (from the main checkout)                       |
|-------|------------------------------------|--------------------------------------------------------------|
| ipo   | Orient Cables                      | `uv run finresearch ipo run orient-cables`                   |
| stock | Infosys                            | `uv run finresearch stock run infosys`                       |
| fund  | Axis Midcap, AMFI 120505           | `uv run finresearch fund run 120505`                         |
| bond  | L&T Finance NCD INE027E07998       | `uv run finresearch bond run INE027E07998`                   |

Check each command's `--help` before running it; never guess flags. `--streams a,b` (IPO) limits the run to a subset of
streams when only those changed: a cheaper check.

## 1. Pre-flight (stop and report if any step fails)
1. No other run is live: `ps -Ao command | grep -c '^claude -p.*--strict-mcp-config'` prints 0, and
   `psql -d finresearch -Atc "select id, kind, status from research_run where status = 'running'"` is empty.
2. Budget: `uv run finresearch bridge limits`. A full IPO run needs the 5-hour window under about 20% used; else say
   when it resets and stop (or offer `--streams` for a partial check).
3. `uv run finresearch bridge health`: the claude_max tier must be usable.
4. `uv run python scripts/smoke_live.py` (bridge, local models, OCR and agent isolation; no database).
5. Only when MCP tools, the claim ledger or agents changed: `uv run python scripts/smoke_mcp_live.py`.

## 2. Run it
Start the run with `run_in_background` and note the run id it prints. Then start a Monitor whose command polls
`uv run finresearch research status <run_id>` every few minutes and prints a line only when a step changes status or the
run reaches done, blocked, paused or failed. Do not poll with sleep in the foreground. A paused run (window limit)
resumes with `uv run finresearch research resume <run_id> --wait`.

## 3. Score and report
1. `uv run finresearch eval gold <run_id>` against `evals/gold/`. The release bar is at least 90% key-fact recall and
   0 contradicted facts. Never trim or edit gold facts to pass; an ill-posed fact is fixed with a reason in the gold
   file's `notes`, in its own PR.
2. `uv run finresearch research status <run_id>` for the 5-hour window share per step.
3. Report: run id, status, gold summary (paste-ready for the PR), window used, slowest and costliest steps, and
   anything blocked by the publish gate (`report_blocked.md`).

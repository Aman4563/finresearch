# Strategy Lab rigour: pre-registration (issue #380, roadmap P2.5)

Registered 22-Oct-2026, before the null experiment below was run and before any PBO, SPA or Romano-Wolf value was
computed on a real Lab trial. The registration is `src/finresearch/stratlab/rigour.py` (`FAMILY`, `GATE`,
`NULL_SIM`); its hash is pinned in `stratlab/rigour_prereg.py`:

`registration hash = a54d87a3a4899bc6c8fab7c40c66e64f168f982cf2c04e20f76bc0b4a27dd22a`

A change to any value is a new registration with its own hash, recorded as post hoc in an `ADDENDUM.md` beside this
file (never by editing this file). No existing spec, criterion or PREREG.md is changed by this item.

## 1. Every run is a trial

- Every Lab run that starts (in sample, out of sample; done, no data, failed, timed out, or marked stale) writes one
  `lab_trial` row in the transaction that gives the run its final status. The table is append-only (a trigger refuses
  UPDATE and DELETE, as `card_log`). A done run stores its daily P&L vector: the change in marked equity per session
  over the replayed window, the first session against the capital.
- Runs that ended before the log existed are back-filled by the migration (no vector: `[]`).
- **N** (the deflated Sharpe's trial count) = `VARIANTS_TRIED` of the library (8) + every distinct spec hash ever run
  in the Lab (in-sample runs included) + every vault read with `counts_in_n` (vault PREREG §2.5). N can only grow.

## 2. The sealed holdout

- A spec's **registration date** is the IST day its hash was first saved (`lab_version.created_at`, now append-only).
- Its **holdout** is every stored session after that day. Each out-of-sample run reports it as its own window, judged
  by the library's criteria (with the in-sample window and the holdout's slippage ×2 stress); only this window can read
  `candidate`. With no session after the registration the holdout reads `too_early`.
- The published out-of-sample window keeps the #361 label (an in-sample result).
- **The Lab never reads the vault** (vault PREREG §2.4: the Lab has no study id). The issue's "one logged read of the
  vault per spec hash" is not implemented: it would need a new vault pack (an owner decision, see the PR).

## 3. The trial family and its statistics (`stratlab.family`, `stratlab.overfit`)

- **Matrix:** per spec hash its longest done trial with a vector; the grid is the sessions that every one of them
  covers (a day a trial did not replay is dropped, never filled with 0). Fewer than 64 common sessions or fewer than
  2 specs: every statistic is unknown.
- **PBO** (Bailey, Borwein, López de Prado & Zhu 2017, CSCV): S = 16 blocks, the oldest T mod S rows dropped;
  statistic = per-session Sharpe (mean / sample sd) of the joined rows; n* = the first argmax in training; ω̄ =
  r̄/(N+1) with tied values sharing their average rank; λ = ln(ω̄/(1−ω̄)); PBO = #{λ ≤ 0} / 12,870 (the paper integrates
  over (−∞, 0], so a tied median counts as overfit). A trial whose Sharpe is undefined in any half (zero variance) is
  excluded and listed, never ranked as 0. Also reported: the OLS slope of R̄_n* on R_n* and Prob[R̄_n* < 0].
- **Bootstrap:** stationary (Politis & Romano 1994), mean block 10 sessions, 1,000 draws, seed 380, circular wrap; the
  same draws serve SPA and Romano-Wolf.
- **ω̂²** = γ̂₀ + 2 Σ κ(n,i) γ̂ᵢ with κ(n,i) = ((n−i)/n)(1−q)^i + (i/n)(1−q)^(n−i), q = 1/10 (Hansen 2005 §2.4).
- **SPA** (Hansen 2005), benchmark = not trading (P&L 0): T = max(maxₖ √n d̄ₖ/ω̂ₖ, 0); p = share of T* > T. Consistent
  recentring: a trial with √n d̄ₖ/ω̂ₖ < −√(2 ln ln n) keeps its negative mean, every other is recentred to 0. The
  lower and upper p-values are reported; the gate reads the consistent one. A never-traded trial (ω̂ = 0, d̄ = 0)
  contributes 0 to every maximum; a constant non-zero trial makes the test unknown.
- **Romano-Wolf** (2005): tₖ = √n d̄ₖ/ω̂ₖ; c = the 0.95 quantile (linear interpolation) of the bootstrap maximum of
  √n(d̄*ₖ − d̄ₖ)/ω̂ₖ over the trials not yet rejected; reject every tₖ > c, drop them and repeat. Reported, not gating.
- **White's Reality Check** (2000, unstudentized) is implemented and tested for reference; SPA supersedes it.

## 4. The promotion gate (`stratlab.promotion`): forward incubation is the only path

"Incubation passed" (the only status that lifts "no proven edge") needs every one of:

1. the registration hash above matches;
2. the latest out-of-sample run reads `candidate` on its forward holdout (§2);
3. PBO < 0.2 on the trial family;
4. SPA consistent p < 0.05 on the trial family;
5. at least 30 closed forward paper trades (the library's `min_oos_trades`) from the version's paper rule sets, each
   signalled after the registration date;
6. their expiry-clustered t ≥ 2.0 (the library's `min_t_stat`);
7. MinTRL (Bailey & López de Prado 2012, eq. 11) per trade: the paper record has at least
   1 + [1 − γ₃·SR + (γ₄−1)/4·SR²]·(z₀.₀₅/SR)² trades, SR = mean / sample sd of the trades' net P&L, γ₃, γ₄ their
   skewness and raw kurtosis. SR ≤ 0: no length suffices.

Any unknown value fails its check. Passing places no order and sizes nothing.

## 5. The null experiment (synthetic data only; `finresearch fno lab null-sim`)

Seed 380; iid normal daily P&L unless stated; the family settings of §3.

| item | panels | design | registered criterion |
|---|---|---|---|
| A. noise PBO | 200 | N = 300 noise trials, T = 1,000 | mean PBO in [0.45, 0.55] |
| B. noise SPA | same panels | as A | SPA consistent p < 0.05 in at most 5 % of panels |
| C. planted | 200 per T | 1 trial with annual SR 1.5 (daily 1.5/√252) + 50 noise, T = 1,000 (primary) and 3,400 | PBO < 0.2 in the mean, and SPA p < 0.05 in at least 80 % of panels |
| D. incubation power | 1,000 runs | one trial, annual SR 1.5, 3,400 sessions | MinTRL met (n ≥ MinTRL(SR̂, γ̂₃, γ̂₄)) in at least 70 % of runs |

C at T = 1,000 is the primary reading (the panel shape of A); T = 3,400 is reported beside it. A rough power estimate
made while writing this file (before any run) suggests C may fail at T = 1,000 (an SR of 1.5 over 4 years is about
t = 3 among 51 trials); if so it is reported as failed, not re-tuned.

Not run in this item (they need the stored F&O history and would write trial rows): the 300 random real-data
configurations (holdout gate rejects at least 95 % of the in-sample-selected ones; PBO reported, not gated) and the
run on the 8 library specs plus the Lab variants. They are left to the owner (see the PR).

Results go in `RESULTS.md` and `null_sim_<date>.json` beside this file.

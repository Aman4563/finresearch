# Strategy Lab rigour: null experiment results (issue #380)

Run 22-Oct-2026 with the registered settings (`finresearch fno lab null-sim`, registration hash `a54d87a3a489…`,
`registered_settings: true`, 1,276 s). The registration (`PREREG.md`, commit 37aa695) was pushed before this run.
Synthetic data only; full output in `null_sim_2026-10-22.json`.

| item | registered criterion | result | verdict |
|---|---|---|---|
| A. noise PBO (200 panels, N = 300, T = 1,000) | mean PBO in [0.45, 0.55] | 0.508 | pass |
| B. noise SPA (same panels) | SPA p < 0.05 in at most 5 % of panels | 8.0 % (16 of 200) | **fail** |
| C. planted SR 1.5 + 50 noise, T = 1,000 (primary) | mean PBO < 0.2 and SPA rejects in >= 80 % | PBO 0.282, rejects 49 % | **fail** |
| C. planted, T = 3,400 (reported beside) | as above | PBO 0.043, rejects 97.5 % | pass |
| D. incubation power, SR 1.5 over 3,400 sessions | MinTRL met in >= 70 % of runs | 100 % | pass |

Reading, without re-tuning anything:

- **B** is over its 5 % line. With 200 panels the binomial standard error at 5 % is 1.5 points, so 8 % is about two
  standard errors above. A post hoc diagnostic (not part of the registration; 400 panels, 500 draws) gave 6.0 % for
  one noise trial and 7.0 % for 20, so the excess is a mild finite-sample over-size of the consistent SPA with a
  10-session stationary bootstrap, not a gross error. The hand-computed tests of `tests/test_lab_overfit.py` pin the
  statistic itself to the paper. A fix (more draws, a studentized bootstrap or a different block length) would be a
  new registration (ADDENDUM.md), not a change to this one.
- **C at T = 1,000** fails as the power estimate in PREREG.md §5 warned: an annual Sharpe of 1.5 over four years is
  about t = 3 among 51 trials, so the IS-best trial is often a noise one. At T = 3,400 the method finds it. The gate
  is protective by design: it cannot promote a real SR-1.5 strategy on four years of data.
- **D**: at 3,400 sessions an SR of 1.5 needs a MinTRL of about 300 sessions, so the incubation length rule never
  binds; the binding checks are the forward holdout, PBO, SPA and the paper trade count.

Not run (PREREG.md §5): the 300 random real-data configurations and the run on the 8 library specs plus the Lab
variants. They need the stored F&O history and write trial rows, so they are left to the owner.

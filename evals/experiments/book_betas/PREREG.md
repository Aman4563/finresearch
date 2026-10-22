# Pre-registration: beta to NIFTY for the F&O book (P4.1 #394, P4.2 #395), version `book-beta-v1`

Registered 22-Oct-2026, before the estimator read any stored futures price: no `fno_bhav` row of the live database
was read for it, and its tests use synthetic series only. The registration is `src/finresearch/fincalc/beta_specs.py`;
its sha256 `13c22ee1e527d32529eaa14514e6d0a72c29a40fc84ee914857cd860d141b892` is pinned in
`fincalc/beta_prereg.py`, and the estimator refuses to return a beta while the two differ. This file restates it in
prose; where they differ, `beta_specs.py` wins. It is never edited: changes go to `ADDENDUM.md` as post hoc, with a new
`SPEC_VERSION` and hash committed before the changed estimator reads real data.

## Why

The scenario engine (#438) and the common-shock sizing term (#443) move every underlying other than NIFTY by β × the
NIFTY move. With no β registered, any BANKNIFTY, FINNIFTY or stock leg made the shock unknown, so every such card
sized 0 and read WAIT. This registers one estimator and one conservative way of using it. It creates no edge claim:
β only scales a stress.

## Data and returns

- **Source:** NSE bhavcopy settlement prices of futures stored in `fno_bhav` (IDF for indices, STF for stocks). The
  market series is NIFTY's own front future, not the index close, so both sides carry the same settle timing.
- **Roll rule:** `fincalc.fno_history.front_future_returns` with the stored lot sizes. The return for session d1 is
  taken within one contract, the earliest expiry after d0 settled on both days, so the front-month change never
  makes a return across two contracts. A lot change is adjusted by its factor when that explains the move; any
  |ln| > 0.25 still is quarantined and counted as missing (#369).
- **Returns:** log returns ln(F_d1 / F_d0).
- **Calendar:** NIFTY's sessions (the days with a NIFTY front-future return). An underlying's return for d1 is paired
  only when its own previous stored day is the same previous NIFTY session; a two-session return is missing.

## Estimator

- **Window:** the last **250** NIFTY returns up to `as_of` (the last NIFTY return).
- **Weights:** EWMA with a half-life of **63 sessions**: w = 2^(-k/63), k = NIFTY sessions back from `as_of`
  (k = 0 at `as_of`), so a missing pair does not shift the weights of the others.
- **Regression:** weighted least squares with an intercept. x̄ = Σwx/Σw, ȳ = Σwy/Σw, Sxx = Σw(x − x̄)²,
  β = Σw(x − x̄)(y − ȳ) / Sxx, e = y − ȳ − β(x − x̄).
- **Standard error:** n_eff = (Σw)²/Σw², s² = (Σw e²/Σw) × n_eff/(n_eff − 2), SE = √(s² / (n_eff × Sxx/Σw)). With
  every w = 1 this is the ordinary OLS standard error.

## Unknown, with the reason (never 0, never a silent 1)

- fewer than **120** paired returns in the window (too little history);
- more than **25** NIFTY sessions in the window without a paired return (gaps);
- a missing pair among the last **5** NIFTY sessions (a roll or data gap at the end of the series);
- `as_of` more than **7** calendar days before today (IST) (a stale series);
- Sxx = 0 or n_eff ≤ 2 (no variation).

## Conservative use: the band (z = 1.645, one-sided 95 %)

- lo = β − z·SE (not floored); hi = max(1, β + z·SE), a floor of **1** on the upper end: no F&O underlying is
  assumed to fall less than NIFTY in a market-wide shock (correlations rise in sell-offs, and the common-shock term
  carries no idiosyncratic loss). NIFTY is (1, 1), with no estimate.
- Every P&L under a NIFTY move m (the scenario grid, the replays, reverse stress and the common-shock sizing term) is
  evaluated, per underlying, at both ends (its legs moved by lo × m and by hi × m) and the worse of the two is taken,
  then summed over underlyings. The upper end alone would flatter a leg that gains from a larger move (a hedge, a
  long call on +5 %); the lower end covers it. This is the worse of the band's two ends, not the worst over the
  interval: a structure whose loss peaks at an interior β (a butterfly, a ratio spread) can lose more inside it.
- The point estimate is shown, and used only for the NIFTY-equivalent delta (a display, no gate).

## Shown with every use

β, its SE, lo and hi, the paired returns and missing sessions, the window's first and last session, the half-life and
`as_of`, on the scenario output (`GET /api/fno/scenarios`) and on the plan card's common-shock row.

## What is not claimed

No out-of-sample test of β's accuracy is registered here. The P&L-explain test of #394 (a)/(b) will cover the joint
moves; until it runs, the band is a stated prior on estimation error, not a validated forecast interval.

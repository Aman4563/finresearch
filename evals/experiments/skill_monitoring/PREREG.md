# Pre-registration: live skill monitoring (P2.3, #378), version `skill-monitor-v1`

Registered 22-Oct-2026, before the monitor computed any statistic on a real outcome: no forecast-ledger row was read
for it, and every threshold below comes from synthetic simulation. The registration is
`src/finresearch/skillmon/specs.py`; its sha256 `48a6ef33ede244488a3a489ed9ddc781b2443a92dc24c113756aeac255ea3de8` is
pinned in `skillmon/prereg.py`, and the monitor refuses to report while the two differ. This file restates it in
prose; where they differ, `specs.py` wins. It is never edited: changes go to `ADDENDUM.md` as post hoc, with a new
`SPEC_VERSION` and hash committed before the changed monitor reads an outcome.

Nothing here promotes a model, changes a call or sizes a trade. The promotion statistic is reported for the owner's
decision under `direction/specs.py` `LIVE_PROMOTION`, which is not edited.

## Cells and benchmarks

A cell is (method group, event kind, underlying): the underlying is the instrument for F&O forecasts and `*` for
every other asset (an IPO lists once, so a per-issue cell would never hold two events).

| model | benchmark | paired on |
| --- | --- | --- |
| direction L2 logistic (`METHOD_LOGIT`) | market-implied lognormal (`METHOD_IMPLIED`) | instrument, event kind, `inputs.start_date` |
| F&O card real-world PoP (`PLAN_METHOD`) | the card's risk-neutral PoP (`RN_METHOD`) | the card (source, `card_index`) |
| IPO shrinkage blend (`BLEND_METHOD`) | QIB-band base rate (`BASE_RATE_METHOD`) | instrument, IST day logged |
| IPO report verdict (`RUN_METHOD`, asset ipo) | QIB-band base rate | instrument, IST day logged |

Every other cell, and every benchmark cell, has no skill score ("no benchmark"), never 0. A model row without a
resolved benchmark row is counted as unpaired; a pair whose two outcomes differ is counted as mismatched; neither is
scored.

Independent events are the track record's own rule, `signals.ledger.independent_events` with the IPO close dates.
Calibration checks run over them in the order their outcomes were known (`resolved_at`, then id).

## Trigger (MF16)

- Below **100 resolved independent events** in a cell (and no cell drives real-money size today, so
  `real_money_cells` is empty), only calibration-in-the-large and the promotion BSS CI are computed, and the cell
  reads **"insufficient data"**. It is never "ok" for want of outcomes.
- At or above it, the full checks run (below), nightly and on read.
- The promotion statistic is reported only with **>= 50 independent events** (`LIVE_PROMOTION`'s own count);
  below that it reads "insufficient data".

## Skill against the benchmark (the promotion statistic)

- BSS = 1 - Σ(p_m - o)² / Σ(p_b - o)² on the paired rows, in origin order.
- Rows: for window events (the direction model: inputs carry `h`) every paired resolved row, overlapping, with
  block length >= h; for every other cell the paired independent events, h = 1.
- Moving-block bootstrap: L = max(h, ceil(n^(1/3))); blocks start uniformly in [0, n - L]; ceil(n/L) blocks joined
  and cut to n; 2,000 resamples, seed 378. **CI = BSS ± 1.96 × the sd of the resampled BSS** (the percentile
  interval is reported beside it).
- Promotion reading: "CI above 0" when the lower end > 0 with >= 50 independent events; else "CI not above 0" or
  "insufficient data".
- Alarm (full mode only): the CI's upper end < 0.

## Calibration (full mode)

- CITL = mean(o) - mean(p), descriptive.
- Spiegelhalter Z = Σ(o - p)(1 - 2p) / √Σ(1 - 2p)² p(1 - p); **alarm when the two-sided p < 0.01**. Zero variance
  (every p = 0.5) is undefined: the cell reads "unknown", not "ok".
- Murphy decomposition on 10 equal-width bins (`horserace.scores.murphy`), descriptive.
- Risk-adjusted Bernoulli CUSUM, two-sided: W_i = o_i ln R - ln(1 - p_i + R p_i), S_i = max(0, S_i-1 + W_i), with
  R = 2 (odds doubled) and R = 0.5 (odds halved). **h_up = 3.7625, h_down = 3.7629**, set by simulation for an
  in-control average run length of 500 events for the two charts together: p i.i.d. from linspace(0.2, 0.8, 601),
  o ~ Bernoulli(p), 2,000 paths of 5,000 events, seed 378, both charts on the same paths, equal one-sided ARLs
  (`skillmon.stats.calibrate_two_sided`). The h are fixed, not re-fitted per cell. The chart runs over the cell's
  whole record with no reset, so an alarm stays until a new version decides otherwise.

## Acceptance (synthetic; issue #378 (a)-(c), `tests/test_skillmon_acceptance.py`, fresh seeds)

| test | criterion | result |
| --- | --- | --- |
| (a) in-control run length | within ±20 % of 500 | 531.6 (1,000 paths, seed 2026) |
| (b) odds ×2 after event 300 | median delay < 100 events | 44 (odds ×0.5: 45); 574 of 1,000 paths had no false alarm before the shift |
| (c) block-bootstrap CI on 1,000 panels with overlapping outcomes | covers the known BSS in >= 93 % | 93.7 % |

(c)'s panels: n = 500, o_t = 1{x_t + e_t+1 + ... + e_t+5 > 0} (neighbouring outcomes share four shocks),
x_t ~ N(0, 1), model Φ(0.8 x / √5), benchmark Φ(0.4 x / √5); the true BSS (0.0338) by quadrature.

**Recorded honestly:** the interval was first written as the bootstrap percentile interval, which covered 92.9 % on
these panels and failed (c). The normal interval with the bootstrap standard error covered 93.6-93.7 %, and that is
what is registered. Neither reaches the nominal 95 %: about 93-94 % for n = 100-1,000, and about 90 % when the
forecast signal itself is strongly persistent (x AR(1) with coefficient 0.9). The promotion CI is therefore somewhat
narrow; read "CI above 0" as necessary, not sufficient.

(d) (the IPO 80 % interval's coverage in the 2019-2026 walk-forward) is not run in v1: the IPO quantile intervals are
registered below but not wired.

## Registered for later versions (not computed by v1)

Each is wired in its own later `SPEC_VERSION`, with its hash committed before it reads an outcome:

- PSI on decile bins of each logistic feature, live against the training window: 0.1 watch, 0.25 act.
- IPO quantile pinball loss at tau 0.1, 0.5, 0.9; p10-p90 coverage against 80 %; adaptive conformal
  alpha_t+1 = alpha_t + 0.005 (0.2 - err_t).
- Regimes: IV30 percentile tercile; trend sign against the 200-session SMA; crisis = IV30 above the 90th percentile
  of the trailing 3 years; cluster-bootstrap heterogeneity tests; a regime effect counts only if it holds on both
  the vault and 2022-26.
- P&L CUSUM on forward trade P&L against the backtest expectancy (ARL about 250 trades with no decay).
- Risk-model triggers: Kupiec p < 0.05 in 3 consecutive quarters marks a model "not for limits".

## Where it shows

The monitor's nightly pass (`monitor/skill.py`) stores the report in its `alert_eval_slot` row; the morning brief
lists every alarm, every "unknown" cell, and one line counting the cells with insufficient data. `finresearch
forecasts skill` prints the same report on read.

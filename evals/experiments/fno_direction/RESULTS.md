# Results: F&O direction model, first real-data run (post hoc to PREREG.md)

Run 19-Oct-2026 under registration hash `372dabfe1d07…` (committed in d3dfeef before any real-data computation) on
the read-only market-data copy `finresearch_fnohist2`: NSE F&O bhavcopy for NIFTY and BANKNIFTY, 3-Jan-2022 to
16-Oct-2026 (1,188 sessions each), with the derived 30-day IV series (#306).

`finresearch fno direction run --history-url postgresql+psycopg://localhost/finresearch_fnohist2 --no-store --json …`

**Nothing passed.** No logistic cell met the pre-registered out-of-sample criteria; the primary cell (NIFTY, 5
sessions, P(up)) has a Brier skill of −0.013 against the market-implied probability (Holm p = 1.00). The app therefore
shows the market-implied probabilities, labelled "no edge", and the decision layer says NO TRADE.

## Out of sample (1-Jul-2024 to the last session with a known outcome, 15-Oct-2026 for h = 1)

BSS = Brier skill score = 1 − Brier(model) ÷ Brier(reference) on the same days; p = one-sided Diebold-Mariano
(Newey-West lag max(h − 1, 5), HLN-corrected), Holm-adjusted over the 24 cells per comparison. "happened" = the
event's OOS frequency. IS = the in-sample walk-forward (first fit Jan-2023, needs 250 labelled rows, to Jun-2024).

| underlying | h | event | OOS days | happened | Brier implied | Brier logistic | BSS vs implied (Holm p) | BSS vs base rate (Holm p) | base rate vs implied BSS | hit rate | AUC | calib. slope | IS BSS vs implied | verdict |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| NIFTY | 1 | up | 570 | 0.498 | 0.2504 | 0.2502 | +0.0006 (1.00) | +0.0068 (1.00) | -0.0063 | 0.489 | 0.530 | 0.82 | -0.0106 | no edge |
| NIFTY | 1 | up_k | 570 | 0.282 | 0.2041 | 0.2024 | +0.0084 (0.66) | +0.0065 (1.00) | +0.0019 | 0.718 | 0.543 | 1.45 | -0.0063 | no edge |
| NIFTY | 1 | down_k | 570 | 0.281 | 0.2024 | 0.2013 | +0.0052 (1.00) | +0.0050 (1.00) | +0.0002 | 0.719 | 0.544 | 0.84 | +0.0058 | no edge |
| NIFTY | 5 | up | 566 | 0.481 | 0.2523 | 0.2556 | -0.0132 (1.00) | -0.0101 (1.00) | -0.0031 | 0.481 | 0.526 | 1.37 | -0.0029 | no edge |
| NIFTY | 5 | up_k | 566 | 0.283 | 0.2054 | 0.2087 | -0.0160 (1.00) | -0.0111 (1.00) | -0.0048 | 0.717 | 0.509 | 0.02 | -0.0031 | no edge |
| NIFTY | 5 | down_k | 566 | 0.318 | 0.2186 | 0.2194 | -0.0039 (1.00) | +0.0002 (1.00) | -0.0041 | 0.682 | 0.514 | 0.37 | -0.0182 | no edge |
| NIFTY | 10 | up | 561 | 0.460 | 0.2560 | 0.2607 | -0.0186 (1.00) | -0.0096 (1.00) | -0.0090 | 0.460 | 0.519 | 0.98 | -0.0046 | no edge |
| NIFTY | 10 | up_k | 561 | 0.296 | 0.2113 | 0.2110 | +0.0017 (1.00) | +0.0053 (1.00) | -0.0036 | 0.704 | 0.553 | 3.89 | -0.0301 | no edge |
| NIFTY | 10 | down_k | 561 | 0.342 | 0.2306 | 0.2307 | -0.0006 (1.00) | -0.0030 (1.00) | +0.0025 | 0.658 | 0.427 | -3.76 | -0.0374 | no edge |
| NIFTY | 20 | up | 551 | 0.535 | 0.2505 | 0.2540 | -0.0136 (1.00) | +0.0030 (1.00) | -0.0166 | 0.535 | 0.314 | -14.05 | -0.1248 | no edge |
| NIFTY | 20 | up_k | 551 | 0.240 | 0.1972 | 0.1977 | -0.0026 (1.00) | +0.0026 (1.00) | -0.0052 | 0.760 | 0.551 | 2.38 | -0.0541 | no edge |
| NIFTY | 20 | down_k | 551 | 0.294 | 0.2103 | 0.2116 | -0.0061 (1.00) | +0.0052 (1.00) | -0.0113 | 0.706 | 0.326 | -9.96 | -0.0914 | no edge |
| BANKNIFTY | 1 | up | 570 | 0.523 | 0.2497 | 0.2506 | -0.0035 (1.00) | -0.0034 (1.00) | -0.0002 | 0.521 | 0.501 | 0.05 | -0.0012 | no edge |
| BANKNIFTY | 1 | up_k | 570 | 0.284 | 0.2047 | 0.2021 | +0.0129 (1.00) | +0.0095 (0.91) | +0.0034 | 0.716 | 0.539 | 1.20 | +0.0083 | no edge |
| BANKNIFTY | 1 | down_k | 570 | 0.268 | 0.1974 | 0.1982 | -0.0042 (1.00) | -0.0037 (1.00) | -0.0005 | 0.732 | 0.479 | -0.37 | +0.0449 | no edge |
| BANKNIFTY | 5 | up | 566 | 0.512 | 0.2504 | 0.2525 | -0.0086 (1.00) | +0.0025 (1.00) | -0.0111 | 0.512 | 0.483 | -1.01 | +0.0002 | no edge |
| BANKNIFTY | 5 | up_k | 566 | 0.276 | 0.2028 | 0.2026 | +0.0007 (1.00) | +0.0028 (1.00) | -0.0020 | 0.724 | 0.544 | 1.16 | -0.0057 | no edge |
| BANKNIFTY | 5 | down_k | 566 | 0.276 | 0.1998 | 0.2012 | -0.0073 (1.00) | -0.0009 (1.00) | -0.0064 | 0.724 | 0.434 | -4.46 | -0.0046 | no edge |
| BANKNIFTY | 10 | up | 561 | 0.526 | 0.2502 | 0.2515 | -0.0052 (1.00) | +0.0082 (1.00) | -0.0135 | 0.526 | 0.457 | -1.06 | -0.0050 | no edge |
| BANKNIFTY | 10 | up_k | 561 | 0.287 | 0.2079 | 0.2073 | +0.0030 (1.00) | +0.0039 (1.00) | -0.0009 | 0.713 | 0.522 | 2.34 | -0.0109 | no edge |
| BANKNIFTY | 10 | down_k | 561 | 0.294 | 0.2079 | 0.2104 | -0.0118 (1.00) | +0.0064 (1.00) | -0.0184 | 0.706 | 0.390 | -6.75 | -0.0151 | no edge |
| BANKNIFTY | 20 | up | 551 | 0.548 | 0.2491 | 0.2496 | -0.0020 (1.00) | +0.0062 (1.00) | -0.0082 | 0.548 | 0.432 | -1.93 | -0.0427 | no edge |
| BANKNIFTY | 20 | up_k | 551 | 0.327 | 0.2217 | 0.2229 | -0.0058 (1.00) | +0.0132 (1.00) | -0.0193 | 0.673 | 0.442 | -2.84 | -0.0039 | no edge |
| BANKNIFTY | 20 | down_k | 551 | 0.267 | 0.1972 | 0.1971 | +0.0006 (1.00) | +0.0196 (1.00) | -0.0194 | 0.733 | 0.410 | -3.51 | -0.0154 | no edge |

Reading it:
- The logistic model beat the market-implied probability in 8 of 24 cells and the trailing base rate in 17 of 24, all by
  small margins (the best: BANKNIFTY 1-session up > ½σ, +0.013; raw p 0.09). The smallest raw p-values were 0.027
  (NIFTY h = 1 up_k vs implied) and 0.038 (BANKNIFTY h = 1 up_k vs base rate): with 24 cells per comparison, about
  1-2 raw p-values below 0.05 are expected by chance, and none survives the Holm adjustment.
- AUC sits at 0.43-0.55, i.e. no ranking power; calibration slopes swing between −14 and +3.9 because the forecasts
  hardly vary (the slope of a near-constant forecast is noise). At 10 and 20 sessions several AUCs are well below 0.5
  (e.g. NIFTY h = 20 up 0.31): overlapping 20-session windows leave only ~28 independent outcomes in the OOS window,
  so these are not evidence of a reliable inverse signal either.
- The in-sample walk-forward was no better (Brier skill vs implied mostly negative), so this is not an in-sample edge
  that decayed: there was none to begin with.
- The market-implied lognormal is close to calibrated on these events: ±½σ moves happened on 24-34 % of OOS days
  against an implied ~31 %, and the trailing base rate beat it in only 4 of 24 cells. The variance risk premium that
  helps option sellers did not make the implied event probabilities badly wrong at these thresholds.
- Hit rates near 0.72 for the ½σ events only reflect that the events happen on ~28 % of days and the forecasts stay
  below 50 %: they are "it will be a quiet day" calls, not directional skill.

## Data notes

- Index level before 2024: the bhavcopy's underlying value exists only in UDiFF files (2024 on), so 2022-2023 returns
  are the front-month future's (same contract both days). On the 693 days with both, the proxy's daily return has a
  correlation of 0.988 (NIFTY) / 0.989 (BANKNIFTY) with the index's, the same sign on 94.5 % / 94.8 % of days, and an
  RMS difference of 14 / 16 basis points. The whole OOS window uses the true underlying value.
- Participant positioning (FII / Pro) has 7 stored days: dropped by the missing-data rule in every fit.
- No historical RBI / results / Budget calendar is stored: the only event flag is the expiry day.
- Power (stated in PREREG.md, synthetic panels): a planted daily correlation of 0.2 between a feature and the next
  return was not detected under this design; 0.3 was. "No edge" means no edge large enough to measure with ~2.3 years
  of OOS data, not proof that none exists.

## Live scoring from here

Every session the nightly job logs P(up), P(up > ½σ) and P(down > ½σ) for 1, 5, 10 and 20 sessions for NIFTY and
BANKNIFTY (market-implied, plus the logistic as a shadow), and the ledger resolves them on the stored underlying value.
Promotion on live evidence is the owner's decision (specs.LIVE_PROMOTION), never automatic.

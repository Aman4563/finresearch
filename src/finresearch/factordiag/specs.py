"""The registration of the stock signal's factor diagnostics (#506): which component scores are ranked against which
forward returns on the point-in-time Nifty 50, the statistics, the two multiple-testing families and what each outcome
would and would not justify. Hashed by `registration_hash()` and pinned in `prereg.py` (with the sha256 of
evals/experiments/stock_factor_diagnostics/PREREG.md, which restates it in prose; this file wins) before any code that
could read a price existed.

It is a DIAGNOSTIC of the already-registered signal (evals/experiments/stock_pit_universe): it adds no strategy and no
trial, changes no signal, call or card, and makes no edge claim. Every numeric setting is a constant here or read at
call time from the module that owns it (`settings()`), so editing one moves the hash. Never edit a registered value in
place: a change is a new SPEC_VERSION recorded as post hoc in the experiment's ADDENDUM.md (never PREREG.md), with its
hash committed before the changed study reads a price.

Method after the signal-evaluation chapter of S. Jansen, Machine Learning for Trading, 3rd ed. (per-date rank IC, IC
by horizon, quantile spreads, turnover, break-even cost), rewritten; its stock-relabelling null is not used (NULLS).
Papers: Grinold & Kahn (2000), Active Portfolio Management, ch. 6 (the information coefficient); Spearman (1904);
Newey & West (1987), Econometrica 55(3); Ibragimov & Mueller (2010), JBES 28(4) (t on group means); Fisher (1935) and
Efron & Tibshirani (1993) ch. 15 (randomisation tests); Benjamini & Hochberg (1995), JRSS B 57(1).
"""

from __future__ import annotations

import hashlib
import json
from datetime import date

SPEC_VERSION = "stock-factor-diagnostics-v1"
STUDY_ID = "stock_factor_diagnostics"
BASE_EXPERIMENT = "evals/experiments/stock_pit_universe"  # the registered portfolio test this diagnoses
LABEL = "diagnostic of a registered signal on seen data: adopts nothing, changes no signal, claims no edge"

# the committed membership inputs of the base experiment, pinned (prereg.require_registered checks both files)
CHANGES_FILE = "evals/experiments/stock_pit_universe/nifty50_changes.csv"
CHANGES_SHA256 = "f6e043643aa6deaf5559bff9aae5111efe205f0b863979cfaf31b2ef93106c92"
CONSTITUENTS_FILE = "evals/stock_backtest/nifty50_constituents_2026-09-30.csv"
CONSTITUENTS_SHA256 = "f5ba4027d935ae72879d5c24bbebf0e8439ec8ed504ffdf4175fde69206244b3"

# composite v1's price weights, restated from signals/stock.composite (inline literals there; a test keeps them equal)
MOM_POINTS, MOM_SCALE, TREND_POINTS, TREND_SCALE = 40.0, 1.5, 20.0, 0.10
TREND_WINDOW = 200  # the 200-day simple average of composite v1 and of stock_backtest.state_at

HORIZONS = (1, 3, 6, 12)  # months ahead: cumulative forward returns (the families' horizons)
SEGMENTS = ((0, 1), (1, 3), (3, 6), (6, 12))  # (a, b]: the return from month a to month b ahead, for IC decay
BLOCK_MONTHS = {1: 12, 3: 12, 6: 18, 12: 24}  # randomisation block per horizon: at least 12 and at least 2h
NW_LAG = {1: 6, 3: 6, 6: 12, 12: 24}  # Bartlett lags per horizon: max(6, 2h) (6 = the base experiment's lag)
MIN_NAMES = 20  # a month with fewer names holding both a score and a return: its IC is unknown, counted
MIN_MONTHS = 36  # a cell with fewer evaluated months: unknown, with the count
MIN_BLOCKS_CI = 3  # the block t interval needs this many blocks
MIN_BLOCKS_A = 5  # family A's p needs this many blocks (below it the smallest attainable p exceeds 0.05)
MIN_BLOCKS_B = 4  # family B's p needs this many blocks in EACH period
QUANTILES = 5
SPLIT = date(2020, 4, 30)  # sub-periods: decision dates before it and from it on (the base's halves)
FAMILY_B_HORIZONS = (1, 3)
FDR = 0.10  # a cell is "detected" when its Benjamini-Hochberg q is at most this
CI_LEVEL = 0.95
POWER = 0.80  # for the minimum detectable mean IC (descriptive)
EXACT_MAX_BLOCKS = 20  # the sign-flip null is enumerated exactly up to 2^20 sign vectors
EXACT_MAX_COMBOS = 200_000  # family B's block reassignments are enumerated exactly up to this many
DRAWS, SEED = 5000, 506  # the Monte Carlo fallback beyond the exact limits
TOLERANCE = 1e-12  # "at least as extreme" is compared with this slack
PRICE_WINDOW_DAYS = 7  # a month-end price: the bar on the day, else the last one within this many days before
AUDIT_EVERY = 12  # the look-ahead audit checks every 12th decision date and the last

QUESTION = ("The portfolio test of the stock signal's price rule on the point-in-time Nifty 50 found no edge. Why: "
            "no ranking skill in any component, skill at short horizons only, skill in one period only, skill the "
            "top-quintile rule does not capture, or skill smaller than its trading costs?")  # fmt: skip
# every score is known at the close of the decision date t (adjusted closes up to and including t); higher = better
COMPONENTS = {
    "mom_score": "risk-adjusted 12-1 momentum, stock_backtest.State.score = fincalc.signals.momentum_12_1 / "
                 "realised_vol (composite v1's 40-point factor and the sort of B0)",
    "trend_distance": "adjusted close at t / its TREND_WINDOW-day simple average - 1 (composite v1's 20-point factor)",
    "low_vol": "minus the 252-day realised volatility (the sort of V2: a high score is a quiet stock)",
    "mom_lowvol_rank": "minus the mean of two ranks over the names eligible at t: mom_score (1 = highest) and "
                       "volatility (1 = lowest), ties sharing the average rank (the sort of V3)",
    "composite_price": "MOM_POINTS x clip(mom_score / MOM_SCALE, -1, 1) + TREND_POINTS x clip(trend_distance / "
                       "TREND_SCALE, -1, 1): the price part of composite v1, the only part with a point-in-time history",
}  # fmt: skip
UNKNOWN_COMPONENTS = {
    "pe_percentile": "composite v1's 10 points: no point-in-time history of filed results in the price cache",
    "institutional_change": "composite v1's 5 points: no point-in-time shareholding history",
    "promoter_change": "composite v1's 5 points: no point-in-time shareholding history",
    "forensic_flags": "composite v1's -10 each: no point-in-time annual filings",
}  # reported as unknown with this reason; in no family
DATA = {
    "prices": "the one-off harvest the base experiment read (stock_harvest's cache: prices/<symbol>.csv, "
              "actions/<symbol>.json), loaded and back-adjusted for splits and bonuses by stock_backtest.load; "
              "price returns without dividends; no network, no database",
    "calendar": "the trading days of stock_universe.MARKET; decision dates are its month-ends "
                "(stock_backtest.month_ends); t_k is the month-end k months after t",
    "membership": "the Nifty 50 at t = stock_universe.members_on over stock_universe.reconstruct(the pinned "
                  "constituent list, the pinned change log): every change effective on or before t applied and none "
                  "after it, so a name excluded later is still in t's cross-section and a name included later is not",
    "eligible": "a member at t whose data symbol (stock_universe.data_symbol) has a bar on t and for which "
                "stock_backtest.state_at is known at that bar (WARMUP bars, no unexplained jump in them): the base "
                "experiment's rule",
    "price_at": "P(d) = the adjusted close on d, else on the last bar within PRICE_WINDOW_DAYS calendar days before "
                "d. When the series has no bar in that window and none after d (it stopped trading: delisted or "
                "merged), P(d) = its last adjusted close: the position is held in cash at 0 % from then, and the "
                "name-date is counted as `ended`. When the series resumes after d: unknown, counted as `gap`",
    "forward_return": "R(t; a, b) = P(t_b) / P(t_a) - 1 with P(t_0) the close on t itself; unknown, counted as "
                      "`jump`, when an unexplained jump day (Adjusted.anomalies) lies after the bar of t_a and on or "
                      "before the bar of t_b. A date t is evaluated for (a, b] only when t_b is in the calendar",
    "cross_section": "for (component, window) at t: the eligible names with a known score and a known return; "
                     "fewer than MIN_NAMES, or either side constant: the month is unknown and counted",
    "audit": "before any statistic, on every AUDIT_EVERY-th decision date and the last: each score recomputed from "
             "the series cut at t must equal the panel's, and each forward return recomputed after doubling every "
             "close before t must equal the panel's; a mismatch refuses the report (LookAheadError)",
}  # fmt: skip
STATISTICS = {
    "ic": "IC_t = Spearman's rank correlation of score and forward return over the month's cross-section: the "
          "Pearson correlation of the two rank vectors, tied values sharing the average rank",
    "summary": "per (component, horizon): evaluated months n, mean IC, sample sd (n - 1), ICIR = mean / sd (per "
               "month, not annualised), the share of months with IC > 0",
    "newey_west": "t = mean / sqrt(direction.metrics.newey_west_var(IC, NW_LAG[h])) (Bartlett). Reported, never "
                  "used for a p or a q: on overlapping horizons it over-rejects (CALIBRATION)",
    "blocks": "the evaluated months in date order are cut into consecutive blocks of BLOCK_MONTHS[h] months from "
              "the first; a remainder shorter than half a block joins the block before it, else it is a block",
    "interval": "mean IC +/- t(q - 1, CI_LEVEL) x sd(block means, q - 1) / sqrt(q) over the q blocks (Ibragimov & "
                "Mueller 2010); unknown below MIN_BLOCKS_CI blocks. Minimum detectable mean IC (descriptive) = "
                "(t(q - 1, 0.975) + t(q - 1, POWER)) x sd(block means) / sqrt(q)",
    "decay": "per component and segment (a, b] of SEGMENTS: the mean IC against R(t; a, b) with the interval of "
             "horizon b (its blocks). Descriptive: no p, in no family",
    "sub_periods": "decision dates before SPLIT and from SPLIT on: per period the summary and the interval, blocks "
                   "formed inside the period, for every horizon. Descriptive except family B",
    "quintiles": "names ranked by score (average ranks r in 1..n, ascending); quintile = min(QUANTILES, "
                 "floor(QUANTILES x (r - 0.5) / n) + 1), so tied scores share a quintile; Q_k,t = the equal-weight "
                 "mean forward return of quintile k; a month with an empty quintile is unknown for the spreads, "
                 "counted. Reported: the time mean of each Q_k and of the cross-section mean; the spreads "
                 "Q5 - Q1 and Q5 - cross-section mean with their mean, Newey-West t and block interval",
    "monotonicity": "Spearman's correlation of (1..5) with the five time-mean quintile returns, and the number of "
                    "the four adjacent steps that go up; descriptive",
    "turnover": "tau_k,t(h) = 1 - |Q_k(t) and Q_k(t_h)| / |Q_k(t_h)| by data symbol: the share of the quintile "
                "held at t_h that was not in it at t (the one-way turnover of an equal-weight quintile rebalanced "
                "every h months, weight drift ignored). Here Q_k(t) is the score's quintile over every eligible "
                "name with a known score at t (no return needed), for each pair t, t_h in the calendar with at "
                "least MIN_NAMES such names on both dates",
    "break_even": "the one-way cost per side at which the mean gross spread is zero: long-only c* = mean(Q5 - "
                  "cross-section mean) / (2 x mean tau_5); long-short c* = mean(Q5 - Q1) / (2 x (mean tau_5 + "
                  "mean tau_1)). Unknown when the mean spread is not positive (no cost level makes it zero) or the "
                  "turnover is 0 or unknown. Shown beside the base harness's per-side cost (COST_BUY + COST_SELL) "
                  "/ 2; the benchmark's own rebalancing cost is ignored, which lowers c* slightly",
}  # fmt: skip
NULLS = {
    "family_a": "H0: the mean IC is 0. Two-sided sign-flip randomisation over blocks of dates: S_b = the sum of "
                "block b's ICs; p = the share of the 2^q sign vectors e with |sum e_b S_b| >= |sum S_b| - TOLERANCE "
                "(all of them when q <= EXACT_MAX_BLOCKS; else DRAWS random vectors from SEED and p = (1 + count) / "
                "(1 + DRAWS)). Each date's cross-section, and so its IC, is kept intact; whole blocks of dates "
                "change sign together. Unknown below MIN_BLOCKS_A blocks",
    "family_b": "H0: the block means of the two periods are exchangeable (same mean IC before and after SPLIT). "
                "Blocks are formed inside each period; D = mean(period-1 block means) - mean(period-2 block "
                "means); p = the share of the C(q1 + q2, q1) reassignments of whole blocks to the periods with "
                "|D*| >= |D| - TOLERANCE (all of them when at most EXACT_MAX_COMBOS; else DRAWS random ones from "
                "SEED, p = (1 + count) / (1 + DRAWS)). Unknown unless each period has MIN_BLOCKS_B blocks",
    "assumption": "valid when block sums are symmetric about 0 (A) or exchangeable (B) and independent across "
                  "blocks under H0; forward windows overlap across a block boundary by up to h - 1 months, which "
                  "is why a block is at least 2h months. No normality is assumed for a p",
    "not_used": "the source's null (one relabelling of the stocks per block of dates) is not used: it breaks the "
                "link between a persistent score and market beta, so its permuted ICs are far less variable than "
                "real ones when returns share a market factor (CALIBRATION: it rejected a true null in 21-25 % of "
                "panels). Issue #506 item 5 asks for a block permutation of dates with cross-sections intact: the "
                "mean IC is unchanged by reordering dates, so the date-axis randomisations that give a null are "
                "the block sign-flip (A) and the block reassignment between periods (B)",
}  # fmt: skip
FAMILIES = {
    "A": "COMPONENTS x HORIZONS, full sample: 5 x 4 = 20 tests of NULLS.family_a",
    "B": "COMPONENTS x FAMILY_B_HORIZONS: 5 x 2 = 10 tests of NULLS.family_b (longer horizons leave too few "
         "blocks per period for any p below 0.05; their sub-period figures are descriptive)",
    "q_values": "Benjamini-Hochberg within each family separately: q_(i) = min over j >= i of m x p_(j) / j, capped "
                "at 1, with m the registered family size (20, 10) whatever the number of unknown cells; an unknown "
                "p has an unknown q and still counts in m. Detected = q <= FDR. Nothing else gets a q",
}  # fmt: skip
OUTPUT = ("One read-only command (finresearch eval factor-diagnostics): it reads the price cache and the two pinned "
          "membership files, opens no database and no network connection, and writes results.json and RESULTS.md: "
          "statistics, counts, the monthly IC and spread series and the symbols counted as `ended`; no price and no "
          "per-name return. It refuses to replace an existing results.json unless told to; a re-run is deterministic")  # fmt: skip
# What each outcome would and would not justify, fixed before any price is read. Per component; "detected at h" is
# family A's q <= FDR at that horizon. The labels are the report's wording.
READING = {
    "always": "Whatever the outcome: signals/stock.py, CALLS_ENABLED, the card, the forecast ledger, the inventory "
              "status of the stock composite and the base experiment's trial count stay as they are. An IC detected "
              "on this seen panel is not out-of-sample evidence and is never called an edge. Any follow-up is a "
              "new variant with its own PREREG, which must disclose this diagnostic's 30 tests as earlier looks",
    "unknown": "a cell below MIN_MONTHS or MIN_BLOCKS_A, or a detected cell whose spread interval or break-even "
               "cost is unknown: no reading for it (unknown, with the counts)",
    "none_detected": "no horizon detected: 'no detectable ranking skill on this panel'. Justifies leaving the "
                     "component as it is and treating a re-weighting of the same price inputs on the Nifty 50 as "
                     "unpromising. Does not justify saying the IC is zero (the minimum detectable IC is printed), "
                     "removing the component from the informational card, or another look at the same panel",
    "detected_negative": "detected with a negative mean IC: 'ranks the wrong way on this panel'. Justifies one "
                         "registered test of a composite without that component, or with it reversed. Does not "
                         "justify reversing or dropping it in the live composite",
    "not_captured": "detected positive at h, and the block interval of the long-only spread (Q5 - cross-section "
                    "mean) at h is not wholly above 0: 'ranking skill the top quintile does not capture'. Justifies one "
                    "registered test of a different portfolio rule for that component (rank-weighted or terciles). "
                    "Does not justify changing the live signal. The plain Q5 here is not B0's rule (B0 adds the "
                    "trend gate and cash slots), so this says nothing about those two features",
    "eaten_by_costs": "detected positive at h, the long-only spread's interval is above 0, and the long-only "
                      "break-even cost is below the harness's per-side cost: 'skill smaller than its trading cost'. "
                      "Justifies one registered test of a lower-turnover construction (longer holding period or "
                      "buffer bands). Does not justify any live change",
    "disagrees": "detected positive at h, the long-only spread's interval is above 0 and the break-even cost is at "
                 "or above the harness's: 'the diagnostic and the portfolio test disagree'. Justifies an audit of "
                 "what differs between the plain quintile and B0 (trend gate, cash slots, k rounding, drifted "
                 "weights, the benchmark's costs) and then one registered re-test. Does not justify adoption",
    "short_lived": "detected at 1 or 3 months and not at 12, with the (6, 12] segment's interval including 0: "
                   "'skill at short horizons only'. Supports registering a follow-up at a shorter holding period "
                   "than the signal's 12-month event. Does not justify changing the live signal's horizon or event; "
                   "'not detected at 12' is weak on its own (few blocks) and is never read as 'zero at 12'",
    "period_difference": "family B's q <= FDR: 'the IC differs before and after April 2020'. Supports labelling "
                         "the earlier evidence regime-specific in a follow-up's PREREG. Does not justify fitting "
                         "on the better period or leaving the other out. It decomposes a difference already seen "
                         "in the base RESULTS.md (KNOWN), so it is not fresh evidence of decay",
}  # fmt: skip
KNOWN = {
    "read_before": "evals/experiments/stock_pit_universe/PREREG.md and RESULTS.md (all four registered rules trail "
                   "the equal-weight point-in-time universe over 151 months; every one is positive in the first "
                   "half, to April 2020, and negative in the second) and evals/stock_backtest's v1 result",
    "split": "SPLIT is that published half boundary, chosen for comparability, so family B tests a difference "
             "whose sign at the portfolio level is already known",
    "not_read": "no price file, no cached market data and no database row was read for this registration or by "
                "the code that produced CALIBRATION (generated arrays only); no IC had been computed on these "
                "prices before the registration was pushed",
}  # fmt: skip
# Size of the registered procedures on a synthetic NULL with a market factor, measured before registration on
# generated arrays only (no file read). The committed generator (factordiag/synth.py) follows this recipe draw for
# draw, so `python -m finresearch.factordiag.synth 2000 506` reprints the table.
CALIBRATION = {
    "recipe": "numpy default_rng(seed); per panel, in this order: beta ~ U(0.6, 1.5) for 50 names; sigma = 0.05 + "
              "0.04 (beta - 0.6) / 0.9 + N(0, 0.005); market m ~ N(0, 0.055) for 176 months (13 before the first of "
              "151 decision months, 12 after the last); r = beta m + sigma N(0, 1), so E[r] = 0 and no score has "
              "skill; a_0 ~ N(0, 0.15), a_t = 0.95 a_(t-1) + N(0, 0.15 sqrt(1 - 0.95^2)); noise ~ N(0, 1) per "
              "decision month and name. Scores at decision month d: lowvol = -(beta + a_d) (persistent, tied to "
              "beta); mom = (sum of r over the 11 months d-11..d-1) / sigma; iid = noise. Forward return over h "
              "months = sum of r over d+1..d+h, on the first 152 - h decision months; family B splits after the "
              "75th decision month",
    "panels": 2000, "seed": 506, "nominal": 0.05, "horizons": HORIZONS,
    # the share of panels with p <= 0.05 (or the 95 % interval missing 0), per score, in HORIZONS order
    "sign_flip": {"lowvol": (0.0460, 0.0580, 0.0445, 0.0455), "mom": (0.0465, 0.0595, 0.0560, 0.0415),
                  "iid": (0.0505, 0.0460, 0.0500, 0.0310)},
    "interval_misses_zero": {"lowvol": (0.0460, 0.0595, 0.0510, 0.0635), "mom": (0.0490, 0.0580, 0.0570, 0.0585),
                             "iid": (0.0505, 0.0460, 0.0510, 0.0475)},
    "newey_west_normal": {"lowvol": (0.0640, 0.0835, 0.1045, 0.1525), "mom": (0.0605, 0.0870, 0.1015, 0.1345),
                          "iid": (0.0615, 0.0605, 0.0775, 0.1065)},
    "family_b": {"lowvol": (0.0495, 0.0615), "mom": (0.0570, 0.0640), "iid": (0.0485, 0.0495)},  # h = 1, 3
    "sign_flip_floor": "at 12 months 140 months make 6 blocks, so no p is below 2 / 2^6 = 0.031: a 12-month cell "
                       "can reach q <= FDR only together with at least six other cells of family A (BH needs "
                       "20 p / rank <= 0.10, so rank >= 7)",
    "relabelling_not_used": "one relabelling of names per block of max(12, 3h) months, 199 draws, 400 panels, seed "
                            "507: p <= 0.05 in 21.3 to 24.8 % of panels for lowvol, 4.0 to 6.0 % for mom and iid",
}  # fmt: skip


def settings() -> dict:
    """Every numeric setting, read at call time from the module that owns it (so editing one moves the hash)."""
    from finresearch.evals import stock_backtest as bt
    from finresearch.evals import stock_universe as su
    from finresearch.fincalc import signals as sg

    return {
        "study": {"horizons": HORIZONS, "segments": SEGMENTS, "block_months": BLOCK_MONTHS, "nw_lag": NW_LAG,
                  "min_names": MIN_NAMES, "min_months": MIN_MONTHS, "min_blocks_ci": MIN_BLOCKS_CI,
                  "min_blocks_a": MIN_BLOCKS_A, "min_blocks_b": MIN_BLOCKS_B, "quantiles": QUANTILES,
                  "split": SPLIT, "family_b_horizons": FAMILY_B_HORIZONS, "fdr": FDR, "ci_level": CI_LEVEL,
                  "power": POWER, "exact_max_blocks": EXACT_MAX_BLOCKS, "exact_max_combos": EXACT_MAX_COMBOS,
                  "draws": DRAWS, "seed": SEED, "tolerance": TOLERANCE, "price_window_days": PRICE_WINDOW_DAYS,
                  "audit_every": AUDIT_EVERY},
        "composite": {"mom_points": MOM_POINTS, "mom_scale": MOM_SCALE, "trend_points": TREND_POINTS,
                      "trend_scale": TREND_SCALE, "trend_window": TREND_WINDOW},
        "membership": {"changes_file": CHANGES_FILE, "changes_sha256": CHANGES_SHA256,
                       "constituents_file": CONSTITUENTS_FILE, "constituents_sha256": CONSTITUENTS_SHA256,
                       "market": su.MARKET, "data_symbol": su.DATA_SYMBOL},
        "harness": {"warmup": bt.WARMUP, "cost_buy": bt.COST_BUY, "cost_sell": bt.COST_SELL,
                    "trading_days": sg.TRADING_DAYS, "skip_days": sg.SKIP_DAYS},
    }  # fmt: skip


def canonical(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def registration() -> dict:
    return {"version": SPEC_VERSION, "study": STUDY_ID, "base_experiment": BASE_EXPERIMENT, "label": LABEL,
            "question": QUESTION, "components": COMPONENTS, "unknown_components": UNKNOWN_COMPONENTS, "data": DATA,
            "statistics": STATISTICS, "nulls": NULLS, "families": FAMILIES, "output": OUTPUT, "reading": READING,
            "known": KNOWN, "calibration": CALIBRATION, "settings": settings()}  # fmt: skip


def registration_hash() -> str:
    return hashlib.sha256(canonical(registration()).encode()).hexdigest()

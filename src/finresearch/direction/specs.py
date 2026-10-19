"""PRE-REGISTERED specification of the F&O direction model (#327): targets, features, models, walk-forward, metrics,
acceptance criteria and the decision layer's gate.

This file is the registration. Its hash (`registration_hash()`: everything in `registration()` as canonical JSON) is
recorded in `direction/prereg.py` in a commit made before any evaluation on real data; `evaluate.require_registered`
refuses an out-of-sample run whose hash differs. Changing anything here afterwards is a NEW VARIANT: add it under a
new key, bump VARIANTS_TRIED and record it in evals/experiments/fno_direction/PREREG.md as post hoc.

Prior, stated before any result: directional forecasts of index returns mostly fail out of sample (Welch & Goyal
2008, "A Comprehensive Look at the Empirical Performance of Equity Premium Prediction", RFS 21(4) [U]). The expected
outcome is "no edge", in which case the app says so and shows market-implied probabilities only.

Choices (none fitted to data):
- Underlyings: NIFTY and BANKNIFTY, the two index option markets with a full IV history in the bhavcopy store.
- Horizons h = 1, 5, 10, 20 sessions. Events at the close of session t + h against the close of session t, with
  R = ln(S[t+h] / S[t]) and σ_h = IV30[t] · √(h / 252) (the 30-day constant-maturity ATM IV of day t, annual):
  "up": R > 0;  "up_k": R > +K·σ_h;  "down_k": R < −K·σ_h.  K = 0.5 (events of roughly 25-35 % frequency, so the
  test has power; K = 1 would leave ~10 % events and few OOS positives at h = 20).
- The index level S: the bhavcopy's underlying value where stored (UDiFF files, 2024 on, so the whole OOS window),
  else (2022-2023) the daily log return of the front-month future (fincalc.fno_history.front_future_returns, each
  return within one contract) chained in as the index's return. A stored index close table (`index_eod`), when the
  history database has one, replaces both. Every feature and target is built from daily log returns, so the series'
  absolute level never matters.
- Features (FEATURES), each known at the close of day t. Participant positioning has only days of history: it is
  dropped by the missing-data rule until it covers half the training window.
- Models: (a) trailing base rate (the benchmark), (b) market-implied lognormal, (c) L2-regularised logistic regression.
  Gradient boosting is not run: no boosting library is a dependency and adding one is out of scope.
- Walk-forward: expanding window from TRAIN_START; refit on the first session of each calendar month using only rows
  whose outcome was known by that session (t + h ≤ the refit session); predictions for every session of that month.
  In-sample = predictions dated to IS_END, out-of-sample = from OOS_START to the last session whose outcome is known.
- Significance: Diebold-Mariano on the per-day Brier-score differential, Newey-West (Bartlett) variance with
  DM_LAG(h) = max(h − 1, 5) lags (overlapping h-day outcomes are serially correlated), the Harvey-Leybourne-Newbold
  small-sample correction, a one-sided p-value from Student's t with n − 1 degrees of freedom.
- Acceptance (CRITERIA), per logistic cell (underlying × horizon × event, 24 cells): OOS n ≥ min_oos_n; Brier skill
  > 0 against BOTH the implied model and the base rate, each with a Holm-adjusted one-sided DM p < alpha across the
  24 cells (one family per comparison); calibration slope (logistic recalibration of the outcome on logit p) within
  [slope_low, slope_high]. PRIMARY names the one cell read first. Beating the implied model alone is not enough: the
  variance risk premium (realised volatility below implied on average) lets a constant beat the implied model on the
  ±K·σ events without any directional information.
- Decision (DECISION): the trade with the highest EV after costs under the model's probabilities, only when its cell
  is accepted, EV_model − EV_implied > 0 and EV_model after costs ≥ max(margin_frac_max_loss × max loss,
  min_ev_over_costs × costs); else "NO TRADE: no measured edge".
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import date

SPEC_VERSION = "2026-10-19.1"
UNDERLYINGS = ("NIFTY", "BANKNIFTY")
HORIZONS = (1, 5, 10, 20)
K = 0.5
EVENTS = ("up", "up_k", "down_k")
EVENT_TEXT = {
    "up": "closes above today's close",
    "up_k": f"rises more than {K:g}·σ (σ = 30-day implied volatility scaled to the horizon)",
    "down_k": f"falls more than {K:g}·σ (σ = 30-day implied volatility scaled to the horizon)",
}
TRAIN_START = date(2022, 1, 1)
IS_END = date(2024, 6, 30)
OOS_START = date(2024, 7, 1)  # to the last session whose outcome is known
YEAR_SESSIONS = 252
RATE = 0.065  # the monitor's constant rate (fincalc.fno_history.RATE); the implied model's drift
PRIMARY = ("NIFTY", 5, "up")

# name: what it is (all at day t's close; returns are log returns, % = ×100)
FEATURES: dict[str, str] = {
    "ret_5": "5-session return",
    "ret_20": "20-session return",
    "ret_60": "60-session return",
    "dist_ma20": "close vs its 20-session average, %",
    "dist_ma50": "close vs its 50-session average, %",
    "dist_ma200": "close vs its 200-session average, %",
    "adx_14": "Wilder ADX(14) on the front future's high/low/close, ratio-adjusted forward at each roll",
    "rv_20": "20-session realised volatility, annual %",
    "rv_60": "60-session realised volatility, annual %",
    "iv_30": "30-day constant-maturity ATM IV, annual %",
    "iv_rank": "IV30 rank in its trailing 252 sessions (min 60)",
    "iv_rv_spread": "IV30 − RV20, vol points",
    "term_slope": "next-expiry ATM IV − near-expiry ATM IV, vol points",
    "skew_25d": "near expiry 25-delta put IV − call IV, vol points",
    "pcr_oi": "put OI ÷ call OI over every stored option of the underlying",
    "pcr_chg_5": "change of pcr_oi over 5 sessions",
    "opt_oi_chg_5": "5-session log change of total option OI",
    "fut_oi_chg_5": "5-session log change of total futures OI",
    "fii_fut_net": "FII index futures (long − short) ÷ (long + short)",
    "fii_opt_net": "FII index options (call long − call short − put long + put short) ÷ all their index option contracts",
    "pro_fut_net": "Pro index futures (long − short) ÷ (long + short)",
    "pro_opt_net": "Pro index options, as fii_opt_net",
    "dte_near": "calendar days to the nearest expiry",
    "expiry_day": "1 when day t is an expiry day of this underlying",
    "dow_mon": "Monday",
    "dow_tue": "Tuesday",
    "dow_wed": "Wednesday",
    "dow_thu": "Thursday",
}
UNAVAILABLE_FEATURES = {
    "event flags (RBI policy, results season, Budget)": "no historical event calendar is stored; expiry day is the "
    "only event flag",
}


@dataclass(frozen=True)
class BaseRate:
    key: str = "base_rate"
    name: str = "Trailing base rate"
    role: str = "benchmark"
    window: int = 500  # the last this-many resolved outcomes (t' + h ≤ t)
    min_labels: int = 60  # fewer: no prediction


@dataclass(frozen=True)
class Implied:
    key: str = "implied"
    name: str = "Market-implied lognormal (30-day IV)"
    role: str = "benchmark"
    rate: float = RATE  # drift r − σ²/2 per year, dividend yield 0 (stated; the index yield is ~1-1.5 %)
    sigma: str = "iv_30"


@dataclass(frozen=True)
class Logistic:
    key: str = "logistic"
    name: str = "L2-regularised logistic regression"
    role: str = "candidate"
    # minimise mean log loss + (alpha·h / 2)·‖w‖² on z-scored features (h = the horizon in sessions: overlapping
    # h-session outcomes carry about n/h independent observations, so the penalty per row grows with h); intercept
    # not penalised; Newton's method. 0.5 was chosen on synthetic panels (a null and a planted signal), before any
    # real-data run: it keeps the null's out-of-sample Brier skill near 0 at h = 1-5 and still finds a planted edge
    alpha: float = 0.5
    max_missing: float = (
        0.5  # a feature missing on more than this share of training rows is left out of the fit
    )
    z_clip: float = 5.0  # z-scores clipped to ±this; a missing value is 0 (the training mean)
    min_train: int = 250  # fewer labelled rows at a refit: no prediction until the next refit


@dataclass(frozen=True)
class Criteria:
    min_oos_n: int = 250
    alpha: float = 0.05  # Holm-adjusted, one-sided, per comparison family (24 logistic cells)
    # below 0.5 the probabilities are too extreme to price trades with; above 1 they are too timid, which only
    # understates an edge (the L2 penalty shrinks toward timid: a planted edge in the synthetic panels gave 1.5-2.7)
    slope_low: float = 0.5
    slope_high: float = 3.0
    must_beat: tuple[str, ...] = ("implied", "base_rate")
    dm_lag_floor: int = 5
    reliability_bins: int = 10


@dataclass(frozen=True)
class Decision:
    margin_frac_max_loss: float = 0.05
    min_ev_over_costs: float = 1.0
    width_pct: float = 1.0  # spread wing / credit-spread short distance, % of spot (as the /fno presets)
    candidates: tuple[str, ...] = ("long_call", "long_put", "bull_call", "bear_put", "bull_put_credit",
                                   "bear_call_credit")  # fmt: skip


BASE, IMPLIED, LOGIT = BaseRate(), Implied(), Logistic()
MODELS = (BASE, IMPLIED, LOGIT)
CRITERIA, DECISION = Criteria(), Decision()
VARIANTS_TRIED = (
    1  # candidate models ever evaluated on real data (the logistic); the benchmarks are not candidates
)
LIVE_PROMOTION = ("A model not accepted out of sample is logged daily as a shadow forecast. Promoting it on live "
                  "evidence is a decision for the owner, not automatic: it needs at least 50 independent resolved "
                  "events per cell (non-overlapping windows) and a live Brier skill above 0 against the implied "
                  "model with a 95 % CI above 0.")  # fmt: skip


def dm_lag(h: int) -> int:
    return max(h - 1, CRITERIA.dm_lag_floor)


def canonical(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def registration() -> dict:
    return {"version": SPEC_VERSION, "underlyings": UNDERLYINGS, "horizons": HORIZONS, "k": K, "events": EVENTS,
            "event_text": EVENT_TEXT, "train_start": TRAIN_START, "is_end": IS_END, "oos_start": OOS_START,
            "year_sessions": YEAR_SESSIONS, "rate": RATE, "primary": PRIMARY, "features": FEATURES,
            "unavailable": UNAVAILABLE_FEATURES, "models": [asdict(m) for m in MODELS], "criteria": asdict(CRITERIA),
            "decision": asdict(DECISION), "variants_tried": VARIANTS_TRIED, "refit": "monthly, first session",
            "dm_lag": {h: dm_lag(h) for h in HORIZONS}, "live_promotion": LIVE_PROMOTION}  # fmt: skip


def registration_hash() -> str:
    return hashlib.sha256(canonical(registration()).encode()).hexdigest()


def model(key: str):
    for m in MODELS:
        if m.key == key:
            return m
    raise KeyError(key)

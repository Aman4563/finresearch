"""PRE-REGISTERED strategy specifications, book settings and pass criteria of the F&O backtest (#307).

This file is the registration. Its hash (`registration_hash()`: the specs, the book, the slippage model and the pass
criteria as canonical JSON) is recorded in `backtest/prereg.py` in a commit made before any out-of-sample evaluation
on real data; the evaluator refuses an out-of-sample run whose hash does not match (`evaluate.require_registered`).
Changing anything here afterwards is a NEW VARIANT: add it with a new key and bump VARIANTS_TRIED, never edit a
registered spec in place, and record the change in evals/experiments/fno_backtest/PREREG.md as post hoc.

Choices (stated before any result was seen; no parameter was fitted to data):
- Few variants on purpose (8; data-mining multiplies false discoveries, Bailey & López de Prado 2014, below).
- Index strategies on NIFTY, the most liquid index option; one BANKNIFTY monthly iron condor as a second index.
  NIFTY is the index that kept weekly expiries after SEBI's 1-Oct-2024 circular (one weekly benchmark per exchange).
- Premium sellers follow the common retail recipe (≈16-20 delta shorts, 30-45 days, take profit at 50 % of the
  credit, stop at a loss of 2× the credit, exit before expiry day), not an optimised one.
- Debit spreads are CONTROLS (option buying; SEBI FY26: 97 % of traders were mainly buyers and most lost) and hold to
  expiry so that STT on exercised in-the-money longs is paid where it applies.
- Covered call / protective put are OVERLAYS on a stock already held: their P&L is the option leg's alone, sized one
  lot; the stock is the stock underlying with the most option contracts traded in the in-sample period among those
  stored (a rule, so no holding is named here). Stock options settle physically, so both exit before expiry day.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import date

SPEC_VERSION = "2026-10-12.1"
IS_START, IS_END = date(2022, 1, 1), date(2024, 6, 30)
OOS_START = date(2024, 7, 1)  # to the latest stored day
IV_WARMUP_DAYS = 400  # calendar days of IV history read before a window starts (IV percentile needs 60+ days)


@dataclass(frozen=True)
class Spec:
    key: str
    name: str
    kind: (
        str  # put_credit | call_credit | iron_condor | debit_call | debit_put | covered_call | protective_put
    )
    underlying: str  # an index, or "STOCK_RULE" (see the module docstring)
    role: str  # candidate | control | overlay
    expiry_kind: str  # monthly | weekly
    dte_min: int
    dte_max: int
    anchor_delta: float  # |delta| of the short leg (sellers, overlays) or of the bought leg (debit spreads)
    width_pct: float | None  # wing distance as % of the forward (spreads); None = single leg
    max_concurrent: int = 2
    max_per_expiry: int = 2
    min_days_between: int = 5  # calendar days between two entries of the same spec
    iv_pct_min: float | None = (
        None  # IV percentile (0..1) of the 30-day IV over the past year must be >= this
    )
    profit_take_pct: float | None = None  # close when the gain >= this % of the credit (or of the debit)
    stop_mult: float | None = None  # close when the loss >= this × the credit (or this fraction of the debit)
    exit_dte: int | None = None  # close at the first close with days to expiry <= this
    hold_to_expiry: bool = False  # else: close on the last session before expiry day at the latest
    rationale: str = ""


SELL = dict(profit_take_pct=50.0, stop_mult=2.0, iv_pct_min=0.30)
SPECS: tuple[Spec, ...] = (
    Spec("nifty_pcs_m", "NIFTY put credit spread, monthly", "put_credit", "NIFTY", "candidate", "monthly", 30, 45,
         0.20, 2.0, exit_dte=7, **SELL, rationale="Collects the index put skew / variance risk premium; bullish-neutral."),
    Spec("nifty_ccs_m", "NIFTY call credit spread, monthly", "call_credit", "NIFTY", "candidate", "monthly", 30, 45,
         0.20, 2.0, exit_dte=7, **SELL, rationale="Bearish-neutral twin; index call skew is flat, so a weaker premium."),
    Spec("banknifty_ic_m", "BANKNIFTY iron condor, monthly", "iron_condor", "BANKNIFTY", "candidate", "monthly",
         30, 45, 0.16, 2.5, exit_dte=7, **SELL, rationale="Neutral premium sale on the second index (monthly only "
         "since SEBI's 2024 circular)."),
    Spec("nifty_ic_w", "NIFTY short iron condor, weekly", "iron_condor", "NIFTY", "candidate", "weekly", 4, 9,
         0.15, 1.0, max_concurrent=1, max_per_expiry=1, min_days_between=3, exit_dte=None, **SELL,
         rationale="The weekly expiry that remains after SEBI's 2024 circular; gamma risk is highest here."),
    Spec("nifty_dcs_m", "NIFTY debit call spread, monthly (control)", "debit_call", "NIFTY", "control", "monthly",
         30, 45, 0.50, 2.0, hold_to_expiry=True, rationale="Option buying as a control: expected to lose its costs."),
    Spec("nifty_dps_m", "NIFTY debit put spread, monthly (control)", "debit_put", "NIFTY", "control", "monthly",
         30, 45, 0.50, 2.0, hold_to_expiry=True, rationale="Option buying as a control: expected to lose its costs."),
    Spec("stock_cc_m", "Covered call on a held stock, monthly", "covered_call", "STOCK_RULE", "overlay", "monthly",
         20, 35, 0.25, None, max_concurrent=1, max_per_expiry=1, profit_take_pct=50.0,
         rationale="Sells upside of a stock already held for premium; the overlay P&L is the call's alone."),
    Spec("stock_pp_m", "Protective put on a held stock, monthly", "protective_put", "STOCK_RULE", "overlay",
         "monthly", 20, 35, 0.25, None, max_concurrent=1, max_per_expiry=1,
         rationale="Insurance: expected to cost money; judged by how much of the stock's drawdown it removes."),
)  # fmt: skip
VARIANTS_TRIED = 8  # every spec ever evaluated on real data (registered or not); the deflated Sharpe's N


@dataclass(frozen=True)
class Book:
    capital: float = 1_000_000.0  # ₹: the margin pool; returns and drawdowns are on it
    max_loss_pct: float = 5.0  # sizing: lots = floor(this % of capital ÷ the defined max loss per lot)
    max_lots: int = 20
    brokerage_per_order: float = 20.0  # ₹ (Groww's F&O price per executed order; fincalc.charges default)
    rate: float = 0.065  # Black-76 discounting (fincalc.fno_history.RATE)
    min_contracts_anchor: int = 10  # the leg chosen by delta must have traded at least this many contracts
    # slippage per leg as a fraction of the reference price by contracts traded that day [U: assumptions; the
    # bhavcopy has no bid/ask, so this stands for half the spread plus impact], with a 2-tick (₹0.10) minimum
    slippage_tiers: tuple[tuple[int, float], ...] = (
        (1000, 0.01),
        (100, 0.02),
        (10, 0.04),
        (1, 0.08),
        (0, 0.15),
    )
    min_slippage: float = 0.10
    slippage_mult: float = 1.0  # the stress run uses 2.0


@dataclass(frozen=True)
class Criteria:
    """All must hold on the out-of-sample window for a spec to become a 'candidate'."""

    min_oos_trades: int = 30
    min_t_stat: float = 2.0  # expectancy after costs, expiry-clustered standard error
    bootstrap_ci: float = (
        0.95  # and the expiry-cluster bootstrap CI of the expectancy must exclude 0 (above it)
    )
    max_drawdown_pct: float = 20.0  # of capital, on the daily marked equity
    min_deflated_sharpe: float = 0.95  # Bailey & López de Prado 2014, N = VARIANTS_TRIED
    in_sample_expectancy_positive: bool = True
    stress_slippage_mult: float = 2.0  # expectancy must stay > 0 with slippage × this
    bootstrap_draws: int = 5000
    bootstrap_seed: int = 307


BOOK, CRITERIA = Book(), Criteria()
DEFLATED_SHARPE_SOURCE = ("Bailey, D. H. & López de Prado, M. (2014), The Deflated Sharpe Ratio: Correcting for "
                          "Selection Bias, Backtest Overfitting and Non-Normality, Journal of Portfolio Management "
                          "40(5), 94-107, https://doi.org/10.3905/jpm.2014.40.5.094")  # fmt: skip


@dataclass(frozen=True)
class Registration:
    specs: tuple[Spec, ...] = SPECS
    book: Book = BOOK
    criteria: Criteria = CRITERIA
    windows: dict = field(default_factory=lambda: {"is": [IS_START.isoformat(), IS_END.isoformat()],
                                                   "oos_from": OOS_START.isoformat()})  # fmt: skip


def canonical(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def spec_hash(spec: Spec) -> str:
    return hashlib.sha256(canonical(asdict(spec)).encode()).hexdigest()


def registration_hash(reg: Registration | None = None) -> str:
    reg = reg or Registration()
    body = {"version": SPEC_VERSION, "variants_tried": VARIANTS_TRIED, **asdict(reg)}
    return hashlib.sha256(canonical(body).encode()).hexdigest()


def by_key(key: str) -> Spec:
    for s in SPECS:
        if s.key == key:
            return s
    raise KeyError(f"no pre-registered spec {key!r}; one of {[s.key for s in SPECS]}")

"""A Strategy Lab strategy from building blocks (#318): validated here, replayed by the #307 engine as a `LabSpec`.

Only defined-risk structures (credit and debit spreads, the iron condor): sizing is by maximum loss, so a leg set
whose loss has no bound (a naked short, a strangle) cannot be sized and is refused. Covered calls and protective puts
stay in the pre-registered library (they are overlays on a held stock, not sized by loss).

Readings of each block:
- entry: an expiry `dte_min`-`dte_max` calendar days away (the nearest; `monthly` = one with a future, else any);
  on the weekdays chosen; IV filter on the 30-day IV's percentile or rank over the past year (fincalc.volatility,
  0-100); trend filter = the underlying's price above / below its 50- or 200-session simple average (stratlab.filters).
- legs: the short (credit) or bought (debit) leg by |Black-76 delta| or by % out of the money from the expiry's
  forward; the wing `width_pct` % of the forward farther out.
- sizing: lots = floor(max_loss_pct % of capital ÷ (max loss + entry costs) per lot), at most max_lots, and within
  the approximate margin (engine).
- management: profit-take at a % of the credit (or of the debit paid); stop at a loss of `stop_mult` × the credit
  (or that fraction of the debit); exit at `exit_dte` days to expiry; exit on the last session before expiry day
  (else held and settled, index only: stock options settle physically); exit before results (stocks; see
  `results_note`).
A version's identity is the hash of its canonical spec (`spec_hash`): a name or note change is not a new variant.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from finresearch.backtest.engine import INDEXES
from finresearch.backtest.specs import BOOK, Book, Spec, canonical

STRUCTURES = {
    "put_credit": "Put credit spread (bull put)",
    "call_credit": "Call credit spread (bear call)",
    "iron_condor": "Iron condor",
    "debit_call": "Call debit spread (bull call)",
    "debit_put": "Put debit spread (bear put)",
}
CREDIT = {"put_credit", "call_credit", "iron_condor"}
TRENDS = {
    "none": "no trend filter",
    "above_ma50": "price above its 50-session average",
    "above_ma200": "price above its 200-session average",
    "below_ma50": "price below its 50-session average",
    "below_ma200": "price below its 200-session average",
}
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri")
MAX_DTE = 70  # backtest.history.MAX_DTE_LOADED (75) less room: farther expiries are not loaded
RESULTS_NOTE = ("Exit before results is applied in paper trading only: the stored F&O history has no past results "
                "calendar, so the backtest cannot apply it and its trades may span a results day.")  # fmt: skip


class StrategySpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    underlying: str = Field(min_length=1, max_length=30, pattern=r"^[A-Za-z0-9&\-]+$")
    structure: Literal["put_credit", "call_credit", "iron_condor", "debit_call", "debit_put"]
    expiry: Literal["monthly", "any"] = "monthly"
    dte_min: int = Field(30, ge=1, le=MAX_DTE)
    dte_max: int = Field(45, ge=1, le=MAX_DTE)
    weekdays: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4], min_length=1, max_length=5)
    iv_measure: Literal["percentile", "rank"] = "percentile"
    iv_min: float | None = Field(None, ge=0, le=100)
    iv_max: float | None = Field(None, ge=0, le=100)
    trend: Literal["none", "above_ma50", "above_ma200", "below_ma50", "below_ma200"] = "none"
    strike_by: Literal["delta", "otm_pct"] = "delta"
    delta: float | None = Field(None, ge=0.05, le=0.6)
    otm_pct: float | None = Field(None, ge=0, le=20)
    width_pct: float = Field(2.0, gt=0, le=10)
    max_loss_pct: float = Field(5.0, gt=0, le=10)
    capital: float = Field(1_000_000.0, ge=100_000, le=100_000_000)
    max_lots: int = Field(20, ge=1, le=50)
    max_concurrent: int = Field(2, ge=1, le=5)
    max_per_expiry: int = Field(2, ge=1, le=5)
    min_days_between: int = Field(5, ge=1, le=30)
    profit_take_pct: float | None = Field(None, gt=0, le=500)
    stop_mult: float | None = Field(None, gt=0, le=5)
    exit_dte: int | None = Field(None, ge=0, le=MAX_DTE)
    exit_before_expiry_day: bool = True
    exit_before_results: bool = False

    @field_validator("underlying")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.strip().upper()

    @field_validator("weekdays")
    @classmethod
    def _days(cls, v: list[int]) -> list[int]:
        if any(d not in range(5) for d in v) or len(set(v)) != len(v):
            raise ValueError("weekdays are distinct numbers 0 (Mon) to 4 (Fri)")
        return sorted(v)

    @model_validator(mode="after")
    def _consistent(self) -> StrategySpec:
        credit = self.structure in CREDIT
        if self.dte_min > self.dte_max:
            raise ValueError("the entry window's minimum days to expiry is above its maximum")
        if self.strike_by == "delta" and (self.delta is None or self.otm_pct is not None):
            raise ValueError("legs by delta: give delta (and no otm_pct)")
        if self.strike_by == "otm_pct" and (self.otm_pct is None or self.delta is not None):
            raise ValueError("legs by % out of the money: give otm_pct (and no delta)")
        if credit and self.delta is not None and self.delta > 0.5:
            raise ValueError("a credit spread sells an out-of-the-money option: short delta at most 0.5")
        if self.iv_min is not None and self.iv_max is not None and self.iv_min >= self.iv_max:
            raise ValueError("the IV filter's minimum must be below its maximum")
        if credit and self.profit_take_pct is not None and self.profit_take_pct >= 100:
            raise ValueError("a credit spread can gain at most its credit: profit-take below 100 %")
        if not credit and self.stop_mult is not None and self.stop_mult > 1:
            raise ValueError("a debit spread can lose at most its debit: stop at most 1 (× the debit)")
        if self.exit_dte is not None and self.exit_dte >= self.dte_min:
            raise ValueError("the days-to-expiry exit must be below the entry window's minimum (else it exits at once)")
        if self.max_per_expiry > self.max_concurrent:
            raise ValueError("positions per expiry cannot exceed positions open at once")
        index = self.underlying in INDEXES
        if not index and not self.exit_before_expiry_day:
            raise ValueError("stock options settle physically: a stock strategy must exit before expiry day")
        if index and self.exit_before_results:
            raise ValueError("an index has no results date: exit before results applies to stocks only")
        return self


def spec_hash(s: StrategySpec) -> str:
    return hashlib.sha256(canonical(s.model_dump()).encode()).hexdigest()


@dataclass(frozen=True)
class LabSpec(Spec):
    """The engine's Spec plus the lab's entry filters (read by stratlab.filters and engine.choose_legs). A subclass, so
    the pre-registered specs and their registration hash are untouched."""

    otm_pct: float | None = None
    weekdays: tuple[int, ...] = (0, 1, 2, 3, 4)
    iv_measure: str = "percentile"
    iv_min: float | None = None
    iv_max: float | None = None
    trend: str = "none"


def to_lab_spec(s: StrategySpec, key: str, name: str) -> LabSpec:
    return LabSpec(
        key=key, name=name, kind=s.structure, underlying=s.underlying, role="lab", expiry_kind=s.expiry,
        dte_min=s.dte_min, dte_max=s.dte_max, anchor_delta=s.delta if s.delta is not None else 0.0,
        width_pct=s.width_pct, max_concurrent=s.max_concurrent, max_per_expiry=s.max_per_expiry,
        min_days_between=s.min_days_between, iv_pct_min=None, profit_take_pct=s.profit_take_pct,
        stop_mult=s.stop_mult, exit_dte=s.exit_dte, hold_to_expiry=not s.exit_before_expiry_day,
        otm_pct=s.otm_pct if s.strike_by == "otm_pct" else None, weekdays=tuple(s.weekdays),
        iv_measure=s.iv_measure, iv_min=s.iv_min, iv_max=s.iv_max, trend=s.trend,
    )  # fmt: skip


def book_for(s: StrategySpec, base: Book = BOOK) -> Book:
    return replace(base, capital=s.capital, max_loss_pct=s.max_loss_pct, max_lots=s.max_lots)


def describe(s: StrategySpec) -> str:
    """One line in words, for lists and the paper book's label."""
    legs = f"{s.delta:g} delta" if s.strike_by == "delta" else f"{s.otm_pct:g}% OTM"
    parts = [f"{s.underlying} {STRUCTURES[s.structure].lower()}", f"{s.dte_min}-{s.dte_max} DTE {s.expiry}",
             f"{legs}, wings {s.width_pct:g}%"]  # fmt: skip
    if s.iv_min is not None or s.iv_max is not None:
        parts.append(f"IV {s.iv_measure} {s.iv_min or 0:g}-{100 if s.iv_max is None else s.iv_max:g}")
    if s.trend != "none":
        parts.append(TRENDS[s.trend])
    if len(s.weekdays) < 5:
        parts.append("on " + "/".join(WEEKDAYS[d] for d in s.weekdays))
    return "; ".join(parts)


def n_filters(s: StrategySpec) -> int:
    """Entry filters switched on (each one is a degree of freedom to overfit with)."""
    return (s.iv_min is not None) + (s.iv_max is not None) + (s.trend != "none") + (len(s.weekdays) < 5)

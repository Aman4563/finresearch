"""Investor profile and personal rules.

Rules are evaluated by Python against live exchange data, ledger facts and the profile, never by the model. A rule
reads "if <metric> <op> <value> then <action>":
- skip: the suggestion is forced to SKIP when the rule fires, and becomes conditional while the metric is unknown;
- warn: the warning is shown with the suggestion.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

Metric = Literal[
    "qib_times",  # combined NSE+BSE subscription, qualified institutional buyers (ex-anchor)
    "nii_times",  # non-institutional investors
    "rii_times",  # retail individual investors
    "total_times",
    "price_band_upper",
    "lot_size",
    "lot_cost",  # lot_size x upper price band, INR
    "max_lots_by_capital",
    "bidding_days_left",
    "gate_ok",  # 1 when the report passed the publish gate
    "p_listing_gain",  # P(listing-day open > issue price), 0..1, from signals.ipo (base-rate table or model)
]
Op = Literal["<", "<=", ">", ">=", "==", "!="]


class Rule(BaseModel):
    id: str = Field(min_length=1, max_length=40)
    description: str = ""
    metric: Metric
    op: Op
    value: Decimal
    action: Literal["skip", "warn"] = "skip"


AlertKind = Literal["ipo", "stock", "fund", "bond", "fno", "portfolio"]
Channel = Literal["ntfy", "telegram", "macos"]
Priority = Literal["min", "low", "default", "high", "urgent"]


class AlertRule(BaseModel):
    """A notify-only rule for any asset kind (finresearch.alerts): "if <metric> of <instrument> <op> <value>, alert me".

    `instrument` None means every watch of the kind (watched IPOs and stocks, funds and bonds added to the app). The
    monitor evaluates it on the metric's cadence; it fires once when the condition becomes true, not again until it
    has cleared and `cooldown_h` has passed. `channels` empty = in-app only. Unlike `Rule`, it never changes a
    suggestion."""

    id: str = Field(min_length=1, max_length=40)
    kind: AlertKind
    metric: str = Field(min_length=1, max_length=40)
    op: Op = "<"
    value: Decimal = Decimal(0)
    instrument: str | None = Field(None, max_length=40)
    params: dict[str, str] = Field(default_factory=dict)
    channels: list[Channel] = Field(default_factory=list)
    priority: Priority = "default"
    cooldown_h: Decimal = Field(Decimal(24), ge=0, le=720)
    enabled: bool = True
    description: str = Field("", max_length=300)

    @field_validator("instrument")
    @classmethod
    def _inst(cls, v: str | None) -> str | None:
        v = (v or "").strip().upper()
        return v or None

    @field_validator("channels")
    @classmethod
    def _chan(cls, v: list[str]) -> list[str]:
        return list(dict.fromkeys(v))

    @model_validator(mode="after")
    def _known_metric(self) -> AlertRule:
        from finresearch.alerts.registry import spec

        m = spec(self.kind, self.metric)
        if m is None:
            raise ValueError(f"unknown {self.kind} metric {self.metric!r}")
        if m.event:  # a change flag: always "fires when it changes"
            self.op, self.value = "==", Decimal(1)
        if self.kind == "portfolio":
            self.instrument = "PORTFOLIO"
        elif self.instrument is None and not m.allows_all:
            raise ValueError(f"{m.label} needs an instrument (it cannot apply to all watches)")
        missing = [p for p in m.params if not str(self.params.get(p, "")).strip()]
        if missing:
            raise ValueError(f"{m.label} needs {', '.join(missing)}")
        return self


class Holding(BaseModel):
    symbol: str
    sector: str | None = None
    value_inr: Decimal | None = None


AvatarColor = Literal["brand", "accent", "gain", "loss", "warn", "info"]  # dashboard colour tokens
Landing = Literal["/", "/ipos", "/stocks", "/funds", "/bonds", "/fno", "/runs", "/monitor", "/journal"]


ChartRange = Literal["1D", "5D", "1M", "3M", "6M", "1Y", "3Y", "5Y"]
ChartInterval = Literal["1m", "5m", "15m", "30m", "1h", "1D"]
INTRADAY_INTERVALS: dict[str, tuple[str, ...]] = {
    "1D": ("1m", "5m", "15m", "30m", "1h"),
    "5D": ("5m", "15m", "30m", "1h"),
}
# IPO subscription checks: any of these bidding-hour times; the 17:15 check after the 17:00 close always runs (it
# records the final book)
IPO_CHECK_CHOICES = (*(f"{h:02d}:{m:02d}" for h in range(10, 17) for m in (0, 30)), "16:45")
IPO_FINAL_CHECK = "17:15"
STOCK_DAILY_CHOICES = ("16:00", "16:30", "17:00", "18:00", "19:00", "20:00", "21:00")
_HHMM = r"^([01]\d|2[0-3]):[0-5]\d$"


class ChartDefaults(BaseModel):
    """Default chart for a page: range (how much time), interval (candle size) and type. An interval that does not fit
    the range is replaced by the range's default (1D -> 5m, 5D -> 15m, longer ranges -> daily bars)."""

    range: ChartRange = "1Y"
    interval: ChartInterval = "1D"
    type: Literal["line", "candle"] = "line"

    @model_validator(mode="after")
    def _fit(self) -> ChartDefaults:
        allowed = INTRADAY_INTERVALS.get(self.range, ("1D",))
        if self.interval not in allowed:
            self.interval = {"1D": "5m", "5D": "15m"}.get(self.range, "1D")  # type: ignore[assignment]
        return self


class TimeFrames(BaseModel):
    """Chart defaults and live refresh cadences. Refresh is never faster than the source: the stock quote is cached
    15 s by the API (NSE's quote is about a minute behind), the option chain and IPO book 60 s; 0 = no auto-refresh."""

    stock: ChartDefaults = Field(default_factory=ChartDefaults)
    index: ChartDefaults = Field(default_factory=lambda: ChartDefaults(range="1D", interval="5m"))
    quote_refresh_s: Literal[0, 15, 30, 60] = 30
    fno_refresh_s: Literal[0, 60, 120, 300] = 60
    ipo_book_refresh_s: Literal[0, 60, 120, 300] = 60


class WatchWindows(BaseModel):
    """When the monitor checks and when alerts stay quiet (IST). Defaults are the monitor's original schedule."""

    ipo_check_times: list[str] = Field(default_factory=lambda: ["10:30", "12:00", "13:30", "15:00", "16:00"],
                                       max_length=len(IPO_CHECK_CHOICES))  # fmt: skip
    stock_daily_time: Literal["16:00", "16:30", "17:00", "18:00", "19:00", "20:00", "21:00"] = "16:30"
    quiet_start: str | None = Field(
        None, pattern=_HHMM
    )  # alerts badge stays quiet from ... (the alerts are kept)
    quiet_end: str | None = Field(None, pattern=_HHMM)  # ... until (may cross midnight)

    @field_validator("ipo_check_times")
    @classmethod
    def _slots(cls, v: list[str]) -> list[str]:
        bad = [t for t in v if t not in IPO_CHECK_CHOICES]
        if bad:
            raise ValueError(
                f"IPO check times must be bidding-hour slots ({', '.join(IPO_CHECK_CHOICES)}); got {bad}"
            )
        return sorted(set(v))

    @model_validator(mode="after")
    def _quiet_pair(self) -> WatchWindows:
        if (self.quiet_start is None) != (self.quiet_end is None):
            raise ValueError("quiet hours need both a start and an end")
        if self.quiet_start is not None and self.quiet_start == self.quiet_end:
            raise ValueError("quiet hours start and end must differ")
        return self

    def subscription_times(self) -> tuple[tuple[int, int], ...]:
        """(hour, minute) of each bidding-day check, ending with the fixed final check."""
        hm = [tuple(int(x) for x in t.split(":")) for t in [*self.ipo_check_times, IPO_FINAL_CHECK]]
        return tuple(hm)  # type: ignore[return-value]

    def stock_time(self) -> tuple[int, int]:
        h, m = self.stock_daily_time.split(":")
        return int(h), int(m)


class Preferences(BaseModel):
    """Dashboard preferences: how the app looks, chart defaults and refresh cadences, and the monitor's watch windows
    (check times and quiet hours). Suggestions never read them."""

    default_landing: Landing = "/"
    number_format: Literal["lakh_crore", "million"] = "lakh_crore"
    compact_tables: bool = False
    reduce_motion: bool = False
    time_frames: TimeFrames = Field(default_factory=TimeFrames)
    watch: WatchWindows = Field(default_factory=WatchWindows)


class Household(BaseModel):
    """Household facts for /wealth (emergency fund, insurance, debt ratios, glide path, goals). All optional: a check
    whose input is missing says so instead of guessing. Personal financial data: kept out of the advisor (LLM)
    prompt (PRIVATE_FIELDS) and used only by finresearch.wealth."""

    age: int | None = Field(None, ge=16, le=100)
    retirement_age: int = Field(60, ge=30, le=85)
    monthly_income_inr: Decimal | None = Field(None, ge=0)  # net take-home, all earners
    monthly_expenses_inr: Decimal | None = Field(
        None, ge=0
    )  # essential household expenses (no EMIs, no SIPs)
    dependants: int = Field(0, ge=0, le=20)
    earners: int = Field(1, ge=1, le=10)
    emergency_months_target: Decimal | None = Field(
        None, gt=0, le=60
    )  # None = the rule of thumb (6 / 9 / 12)
    support_years: int | None = Field(
        None, ge=0, le=60
    )  # years dependants need support; None = to retirement
    tax_regime: Literal["new", "old"] = "new"
    target_equity_pct: Decimal | None = Field(None, ge=0, le=100)  # None = the age rule of thumb


class Profile(BaseModel):
    capital_per_ipo_inr: Decimal = Field(Decimal(15000), ge=0)
    risk_appetite: Literal["low", "medium", "high"] = "medium"
    horizon: Literal["listing", "short", "long"] = "listing"
    tax_slab_pct: Decimal = Field(Decimal(30), ge=0, le=50)
    category: Literal["retail", "shni", "bhni"] = "retail"
    holdings: list[Holding] = Field(default_factory=list)
    rules: list[Rule] = Field(default_factory=list)
    # the most of the portfolio one stock may take (percent); None = by risk appetite (low 5, medium 8, high 10)
    max_position_pct: Decimal | None = Field(None, gt=0, le=100)
    notes: str = ""
    # F&O analysis (the /fno page and the F&O signal); the signal never proposes a position whose maximum loss is
    # above fno_max_loss_pct of fno_capital_inr, and by default only defined-risk strategies
    fno_capital_inr: Decimal = Field(
        Decimal(0), ge=0
    )  # 0 = not set: the F&O signal says so and makes no call
    fno_max_loss_pct: Decimal = Field(Decimal(2), gt=0, le=100)  # per strategy, % of fno_capital_inr
    fno_brokerage_per_order_inr: Decimal = Field(Decimal(20), ge=0, le=1000)  # flat per executed order
    fno_defined_risk_only: bool = True
    fno_experience: Literal["none", "some", "experienced"] = "none"
    # identity and dashboard preferences (optional; profiles saved before they existed load with these defaults)
    display_name: str = Field("", max_length=60)
    avatar_color: AvatarColor | None = None
    preferences: Preferences = Field(default_factory=Preferences)
    # notify-only alert rules for every asset kind (roadmap item 10); delivery settings live outside the profile
    alert_rules: list[AlertRule] = Field(default_factory=list, max_length=200)
    # household finances for /wealth (finresearch.wealth); private, never in the advisor prompt
    household: Household = Field(default_factory=Household)

    @field_validator("display_name")
    @classmethod
    def _strip_name(cls, v: str) -> str:
        return " ".join(v.split())


# personal financial data: never sent to an LLM
PRIVATE_FIELDS = {"household"}
# not investment inputs (or private): kept out of the advisor prompt
UI_FIELDS = {"display_name", "avatar_color", "preferences", "alert_rules", *PRIVATE_FIELDS}


DEFAULT_RULES = [
    Rule(
        id="qib-floor",
        description="Skip if the QIB book is below 1x",
        metric="qib_times",
        op="<",
        value=Decimal(1),
    ),
    Rule(
        id="gate",
        description="Skip if the report did not pass the publish gate",
        metric="gate_ok",
        op="<",
        value=Decimal(1),
    ),
    Rule(
        id="one-lot",
        description="Skip if one lot costs more than my capital",
        metric="max_lots_by_capital",
        op="<",
        value=Decimal(1),
    ),
]


def default_profile() -> Profile:
    return Profile(rules=list(DEFAULT_RULES))

"""Investor profile and personal rules.

Rules are evaluated by Python against live exchange data, ledger facts and the profile, never by the model. A rule
reads "if <metric> <op> <value> then <action>":
- skip: the suggestion is forced to SKIP when the rule fires, and becomes conditional while the metric is unknown;
- warn: the warning is shown with the suggestion.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, field_validator

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
]
Op = Literal["<", "<=", ">", ">=", "==", "!="]


class Rule(BaseModel):
    id: str = Field(min_length=1, max_length=40)
    description: str = ""
    metric: Metric
    op: Op
    value: Decimal
    action: Literal["skip", "warn"] = "skip"


class Holding(BaseModel):
    symbol: str
    sector: str | None = None
    value_inr: Decimal | None = None


AvatarColor = Literal["brand", "accent", "gain", "loss", "warn", "info"]  # dashboard colour tokens
Landing = Literal["/", "/ipos", "/stocks", "/funds", "/bonds", "/fno", "/runs", "/monitor", "/journal"]


class Preferences(BaseModel):
    """Dashboard preferences. They only change how the app looks; suggestions never read them."""

    default_landing: Landing = "/"
    number_format: Literal["lakh_crore", "million"] = "lakh_crore"
    compact_tables: bool = False
    reduce_motion: bool = False


class Profile(BaseModel):
    capital_per_ipo_inr: Decimal = Field(Decimal(15000), ge=0)
    risk_appetite: Literal["low", "medium", "high"] = "medium"
    horizon: Literal["listing", "short", "long"] = "listing"
    tax_slab_pct: Decimal = Field(Decimal(30), ge=0, le=50)
    category: Literal["retail", "shni", "bhni"] = "retail"
    holdings: list[Holding] = Field(default_factory=list)
    rules: list[Rule] = Field(default_factory=list)
    notes: str = ""
    # identity and dashboard preferences (optional; profiles saved before they existed load with these defaults)
    display_name: str = Field("", max_length=60)
    avatar_color: AvatarColor | None = None
    preferences: Preferences = Field(default_factory=Preferences)

    @field_validator("display_name")
    @classmethod
    def _strip_name(cls, v: str) -> str:
        return " ".join(v.split())


# not investment inputs: kept out of the advisor prompt
UI_FIELDS = {"display_name", "avatar_color", "preferences"}


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

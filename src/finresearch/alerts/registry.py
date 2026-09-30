"""The alert-metric registry: for each asset kind, the metrics the app can actually compute, with a plain-English
phrase, a unit, a source and the cadence the monitor checks it on.

Only metrics with a working data path are listed. Left out on purpose (not computable today): promoter *pledge*
(the shareholding XBRL parser reads category holdings, not encumbrance), P/E percentile alerts (needs 120 days of
daily P/E; shown on the signal instead), fund category-rank (no ranking source), bond spread over G-sec and rating
*actions* from agencies (only NSE's rating string is read, so a rating change is detected when NSE's list changes).

Cadences:
- "intraday": every 15 minutes while NSE's cash market is open (09:15-15:30 IST on trading days), from cheap
  sources (one quote, NSE's bond list, the monitor's own subscription snapshots);
- "daily": once each trading day at the profile's after-close check time (Profile → watch windows), from heavier
  sources (three years of history, results, AMFI files, the IV history table).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

Kind = Literal["ipo", "stock", "fund", "bond", "fno", "portfolio"]
KINDS: tuple[str, ...] = ("ipo", "stock", "fund", "bond", "fno", "portfolio")
Unit = Literal["inr", "pct", "pp", "times", "days", "count", "flag", "score", "points"]
Cadence = Literal["intraday", "daily"]

OP_PHRASE = {"<": "is below", "<=": "is at most", ">": "is above", ">=": "is at least", "==": "equals",
             "!=": "is not"}  # fmt: skip

NSE_QUOTE = "https://www.nseindia.com/get-quotes/equity"
NSE_BONDS = "https://www.nseindia.com/market-data/bonds-traded-in-capital-market"
NSE_CHAIN = "https://www.nseindia.com/option-chain"
AMFI_NAV = "https://www.amfiindia.com/spages/NAVAll.txt"
AMFI_HISTORY = "https://portal.amfiindia.com/DownloadNAVHistoryReport_Po.aspx"
AMFI_TER = "https://www.amfiindia.com/ter-of-mf-schemes"


@dataclass(frozen=True)
class MetricSpec:
    key: str
    kind: str
    label: str
    phrase: str  # noun phrase: "<instrument>: <phrase> <op> <value>"
    unit: str
    cadence: str
    description: str
    source: str
    event: bool = False  # a 0/1 change flag: the rule is always "== 1" ("fires when it changes")
    event_text: str = ""  # sentence for an event metric: "<instrument>: <event_text>"
    rebase: str | None = (
        None  # change metrics: "always" (compare with the last check) | "on_fire" (since last alert)
    )
    params: tuple[str, ...] = ()  # rule params the metric needs
    default_op: str = "<"
    default_value: str = "0"
    allows_all: bool = True  # can attach to "all watches of this kind"

    def json(self) -> dict[str, Any]:
        return {k: (list(v) if isinstance(v, tuple) else v) for k, v in asdict(self).items()}


def _m(kind: str, key: str, label: str, phrase: str, unit: str, cadence: str, description: str, source: str,
       **kw: Any) -> MetricSpec:  # fmt: skip
    return MetricSpec(key=key, kind=kind, label=label, phrase=phrase, unit=unit, cadence=cadence,
                      description=description, source=source, **kw)  # fmt: skip


_SUB = "the monitor's latest subscription snapshot (NSE combined book; BSE for a BSE SME issue)"

METRICS: list[MetricSpec] = [
    # ------------------------------------------------------------------ IPO (watched issues; notify only)
    _m(
        "ipo",
        "qib_times",
        "QIB subscription",
        "QIB subscription",
        "times",
        "intraday",
        "How many times the qualified institutional buyers' portion (ex-anchor) is subscribed.",
        _SUB,
        default_op=">=",
        default_value="10",
    ),
    _m(
        "ipo",
        "nii_times",
        "NII subscription",
        "NII (HNI) subscription",
        "times",
        "intraday",
        "Subscription of the non-institutional portion (bids above ₹2 lakh).",
        _SUB,
        default_op=">=",
        default_value="10",
    ),
    _m(
        "ipo",
        "rii_times",
        "Retail subscription",
        "retail subscription",
        "times",
        "intraday",
        "Subscription of the retail portion; very high means slim allotment odds.",
        _SUB,
        default_op=">=",
        default_value="20",
    ),
    _m(
        "ipo",
        "total_times",
        "Total subscription",
        "total subscription",
        "times",
        "intraday",
        "Subscription of the whole issue.",
        _SUB,
        default_op=">=",
        default_value="5",
    ),
    _m(
        "ipo",
        "bidding_days_left",
        "Bidding days left",
        "bidding days left",
        "days",
        "daily",
        "Exchange working days to the close, both included (1 on the last day).",
        "fincalc: exchange business days from today to the watch's close date",
        default_op="<=",
        default_value="1",
    ),
    # ------------------------------------------------------------------ listed stocks (NSE symbol or BSE:<code>)
    _m(
        "stock",
        "price",
        "Price",
        "price",
        "inr",
        "intraday",
        "Last traded price in ₹ (about a minute behind the exchange). Use it for a buy zone or a stop level.",
        f"NSE quote (BSE quote for a BSE-only stock): {NSE_QUOTE}",
        default_op="<",
        default_value="0",
    ),
    _m(
        "stock",
        "day_change_pct",
        "Move on the day",
        "move on the day",
        "pct",
        "intraday",
        "Last price against the previous close, in %. Negative for a fall.",
        "NSE quote: (last price / previous close - 1) x 100",
        default_op="<=",
        default_value="-5",
    ),
    _m(
        "stock",
        "pct_from_52w_high",
        "Distance from the 52-week high",
        "distance from the 52-week high",
        "pct",
        "intraday",
        "How far the price is below its 52-week high, in % (0 at a new high, -20 = 20 % below).",
        "NSE quote: (last price / 52-week high - 1) x 100",
        default_op="<=",
        default_value="-20",
    ),
    _m(
        "stock",
        "pct_from_52w_low",
        "Distance from the 52-week low",
        "distance from the 52-week low",
        "pct",
        "intraday",
        "How far the price is above its 52-week low, in % (0 at a new low).",
        "NSE quote: (last price / 52-week low - 1) x 100",
        default_op="<=",
        default_value="2",
    ),
    _m(
        "stock",
        "pct_vs_200dma",
        "Price vs 200-day average",
        "price vs its 200-day average",
        "pct",
        "daily",
        "Last price against its 200-day simple moving average of split-adjusted closes, in %. Below 0 = below the "
        "200-DMA (the trend factor of the stock signal).",
        "signals.stock features: price / SMA200 - 1 (NSE daily history)",
        default_op="<",
        default_value="0",
    ),
    _m(
        "stock",
        "signal_action_changed",
        "Signal action changed",
        "the signal's action",
        "flag",
        "daily",
        "Fires when the stock signal's action (BUY / ACCUMULATE / HOLD / REDUCE / SELL) differs from the last check.",
        "finresearch.signals.stock (composite v1; the forecast ledger is not written by these checks)",
        event=True,
        event_text="the stock signal's action changed",
        rebase="always",
        default_op="==",
        default_value="1",
    ),
    _m(
        "stock",
        "signal_score",
        "Signal score",
        "signal score",
        "score",
        "daily",
        "The stock signal's composite score, -100 to +100 (rule-based, pre-registered weights).",
        "finresearch.signals.stock composite v1",
        default_op="<=",
        default_value="-20",
    ),
    _m(
        "stock",
        "forensic_red_flags",
        "Forensic red flags",
        "number of forensic red flags",
        "count",
        "daily",
        "Red flags among the five forensic screening scores (Beneish, Altman Z''-EM, Piotroski, accruals, "
        "CFO/EBITDA) for the latest fiscal year. Screening flags only, not validated on Indian data.",
        "signals.stock.forensic (annual Integrated Filing XBRL)",
        default_op=">=",
        default_value="1",
    ),
    _m(
        "stock",
        "days_to_ex_date",
        "Days to the next ex-date",
        "days to the next ex-date",
        "days",
        "daily",
        "Calendar days to the next corporate-action ex-date (dividend, split, bonus). Buy before it to be entitled.",
        "NSE corporate actions (BSE for a BSE-only stock)",
        default_op="<=",
        default_value="7",
    ),
    _m(
        "stock",
        "days_since_results",
        "Days since results",
        "days since the last results filing",
        "days",
        "daily",
        "Calendar days since the latest quarterly results were filed (0 on the filing day).",
        "NSE financial-results filings (BSE Integrated Filing for a BSE-only stock)",
        default_op="<=",
        default_value="1",
    ),
    _m(
        "stock",
        "promoter_change_pp",
        "Promoter holding change",
        "promoter holding change over the last quarter",
        "pp",
        "daily",
        "Promoter and promoter-group holding in the latest shareholding pattern minus the quarter "
        "before, in percentage points. A fall of a point or more is worth a look.",
        "shareholding-pattern XBRL (NSE/BSE)",
        default_op="<=",
        default_value="-1",
    ),
    # ------------------------------------------------------------------ mutual funds (AMFI scheme code)
    _m(
        "fund",
        "nav",
        "NAV",
        "NAV",
        "inr",
        "daily",
        "The latest net asset value per unit, in ₹.",
        f"AMFI NAVAll: {AMFI_NAV}",
        default_op="<",
        default_value="0",
    ),
    _m(
        "fund",
        "nav_change_pct",
        "NAV change on the day",
        "NAV change since the previous NAV",
        "pct",
        "daily",
        "The latest NAV against the one before it, in %. Negative for a fall.",
        f"AMFI NAV history (last 10 days): {AMFI_HISTORY}",
        default_op="<=",
        default_value="-3",
    ),
    _m(
        "fund",
        "hit_rate_3y_pct",
        "3-year hit rate vs category",
        "share of 3-year windows beating the category median",
        "pct",
        "daily",
        "In how many quarter-end 3-year windows the fund's return was at or above its SEBI category's median, in %.",
        "signals.fund.analyse (AMFI NAV history, same-plan growth peers)",
        default_op="<",
        default_value="40",
    ),
    _m(
        "fund",
        "excess_3y_pp",
        "Latest 3-year return vs category",
        "latest 3-year return minus the category median",
        "pp",
        "daily",
        "The fund's latest quarter-end 3-year annualised return minus its category median, in percentage points.",
        "signals.fund.analyse (AMFI NAV history)",
        default_op="<",
        default_value="-2",
    ),
    _m(
        "fund",
        "ter_pct",
        "Expense ratio (TER)",
        "expense ratio",
        "pct",
        "daily",
        "The plan's total expense ratio from AMFI's TER file, % a year.",
        f"AMFI TER file: {AMFI_TER}",
        default_op=">",
        default_value="1",
    ),
    _m(
        "fund",
        "ter_change_pp",
        "Expense ratio change",
        "change in the expense ratio since the last alert",
        "pp",
        "daily",
        "TER now minus the TER when the rule was created or last fired, in percentage points.",
        f"AMFI TER file: {AMFI_TER}",
        rebase="on_fire",
        default_op=">=",
        default_value="0.1",
    ),
    _m(
        "fund",
        "signal_action_changed",
        "Signal action changed",
        "the fund signal's action",
        "flag",
        "daily",
        "Fires when the fund signal's action differs from the last check.",
        "finresearch.signals.fund",
        event=True,
        event_text="the fund signal's action changed",
        rebase="always",
        default_op="==",
        default_value="1",
    ),
    # ------------------------------------------------------------------ bonds (ISIN)
    _m(
        "bond",
        "price",
        "Price",
        "price",
        "inr",
        "intraday",
        "NSE's last traded price in ₹ per bond, as listed (compare it with the face value on the bond page).",
        f"NSE bonds list: {NSE_BONDS}",
        default_op="<",
        default_value="0",
    ),
    _m(
        "bond",
        "ytm_pct",
        "Yield to maturity",
        "yield to maturity",
        "pct",
        "intraday",
        "Pre-tax YTM at the last price, % a year, from coupon, maturity and the coupon frequency (verified in a "
        "research run, else assumed yearly, as the bond signal does).",
        "fincalc.bonds.ytm on NSE's list row",
        default_op=">=",
        default_value="10",
    ),
    _m(
        "bond",
        "rating_changed",
        "Rating changed",
        "the credit rating",
        "flag",
        "daily",
        "Fires when the rating string in NSE's bond list differs from the last check (for example AA to AA-).",
        f"NSE bonds list rating column: {NSE_BONDS}",
        event=True,
        event_text="its credit rating changed",
        rebase="always",
        default_op="==",
        default_value="1",
    ),
    _m(
        "bond",
        "days_to_coupon",
        "Days to the next coupon",
        "days to the next coupon",
        "days",
        "daily",
        "Calendar days to NSE's next interest date.",
        f"NSE bonds list: {NSE_BONDS}",
        default_op="<=",
        default_value="7",
    ),
    _m(
        "bond",
        "signal_action_changed",
        "Signal action changed",
        "the bond signal's action",
        "flag",
        "daily",
        "Fires when the bond signal's action differs from the last check.",
        "finresearch.signals.bond",
        event=True,
        event_text="the bond signal's action changed",
        rebase="always",
        default_op="==",
        default_value="1",
    ),
    # ------------------------------------------------------------------ F&O (underlying: NIFTY, BANKNIFTY, a stock)
    _m(
        "fno",
        "spot",
        "Underlying level",
        "underlying level",
        "points",
        "intraday",
        "The underlying's level from NSE's option chain (index points, or ₹ for a stock).",
        f"NSE option chain: {NSE_CHAIN}",
        default_op=">=",
        default_value="0",
        allows_all=False,
    ),
    _m(
        "fno",
        "atm_iv",
        "ATM implied volatility",
        "ATM implied volatility",
        "pct",
        "daily",
        "At-the-money IV recorded after the close (nearest expiry at least a week away), % a year.",
        "iv_history table (monitor.iv, from NSE's option chain)",
        default_op=">=",
        default_value="20",
        allows_all=False,
    ),
    _m(
        "fno",
        "iv_percentile",
        "IV percentile",
        "IV percentile",
        "pct",
        "daily",
        "Share of the last year's recorded days with a lower ATM IV, in %. Needs 60 recorded days.",
        "fincalc.volatility.iv_stats over iv_history",
        default_op=">=",
        default_value="80",
        allows_all=False,
    ),
    _m(
        "fno",
        "days_to_expiry",
        "Days to expiry",
        "days to the nearest expiry",
        "days",
        "daily",
        "Calendar days to the nearest listed expiry (or to the rule's expiry param).",
        f"NSE option-chain contract info: {NSE_CHAIN}",
        default_op="<=",
        default_value="2",
        allows_all=False,
    ),
    _m(
        "fno",
        "strategy_pnl_pct_of_max_loss",
        "Strategy P&L vs max loss",
        "strategy P&L as a share of its maximum loss",
        "pct",
        "intraday",
        "Mark-to-market P&L of your legs (entry premiums you type) at NSE's last prices, as % of the strategy's "
        "maximum loss at expiry. -50 = you have lost half of what the strategy can lose. Costs are not included.",
        "fincalc.options.profile + NSE option chain last prices",
        params=("legs", "expiry"),
        default_op="<=",
        default_value="-50",
        allows_all=False,
    ),
    # ------------------------------------------------------------------ portfolio (needs finresearch.portfolio)
    _m(
        "portfolio",
        "allocation_drift_pp",
        "Allocation drift",
        "largest allocation drift from target",
        "pp",
        "daily",
        "The largest gap between an asset class's weight and your target weight, in percentage points.",
        "finresearch.portfolio (holdings valued from AMFI/NSE)",
        default_op=">=",
        default_value="5",
        allows_all=False,
    ),
    _m(
        "portfolio",
        "ltcg_headroom_inr",
        "LTCG exemption headroom",
        "unused LTCG exemption this financial year",
        "inr",
        "daily",
        "How much of the ₹1.25 lakh yearly exemption on equity long-term gains is still unused.",
        "finresearch.portfolio tax view (realised gains this financial year)",
        default_op=">=",
        default_value="50000",
        allows_all=False,
    ),
    _m(
        "portfolio",
        "drawdown_pct",
        "Portfolio drawdown",
        "drawdown from the portfolio's peak value",
        "pct",
        "daily",
        "Current value against the highest recorded value, in % (0 at a new high).",
        "finresearch.portfolio value history",
        default_op="<=",
        default_value="-10",
        allows_all=False,
    ),
]

REGISTRY: dict[tuple[str, str], MetricSpec] = {(m.kind, m.key): m for m in METRICS}


def spec(kind: str, key: str) -> MetricSpec | None:
    return REGISTRY.get((kind, key))


def metrics_for(kind: str) -> list[MetricSpec]:
    return [m for m in METRICS if m.kind == kind]


def fmt_value(unit: str, v: Any) -> str:
    """A value in its unit, the way a sentence shows it: ₹1,400, -5%, 12.5x, 3 days, +1.2 pp."""
    from decimal import Decimal

    if v is None:
        return "unknown"
    d = Decimal(str(v))
    n = d.normalize()
    s = format(n, "f") if abs(n) < 10**12 else str(n)
    if unit == "inr":
        from finresearch.fincalc.numbers import group_indian

        q = abs(d).quantize(Decimal("0.01"))
        whole, _, frac = format(q, "f").partition(".")
        frac = "" if frac == "00" else frac
        return ("-" if d < 0 else "") + "₹" + group_indian(whole) + (f".{frac}" if frac else "")
    if unit == "pct":
        return f"{d:.2f}".rstrip("0").rstrip(".") + "%"
    if unit == "pp":
        return f"{d:+.2f}".rstrip("0").rstrip(".") + " pp"
    if unit == "times":
        return f"{d:.2f}".rstrip("0").rstrip(".") + "x"
    if unit == "days":
        return f"{s} day" + ("" if n == 1 else "s")
    return s


def subject(kind: str, instrument: str | None) -> str:
    if instrument:
        return "Portfolio" if kind == "portfolio" else instrument
    return {"ipo": "Any watched IPO", "stock": "Any watched stock", "fund": "Any tracked fund",
            "bond": "Any tracked bond"}.get(kind, "Any")  # fmt: skip


def sentence(kind: str, metric: str, op: str, value: Any, instrument: str | None = None) -> str:
    """The rule in plain English: "INFY: price is below ₹1,400"."""
    m = spec(kind, metric)
    who = subject(kind, instrument)
    if m is None:
        return f"{who}: {metric} {OP_PHRASE.get(op, op)} {value}"
    if m.event:
        return f"{who}: {m.event_text}"
    return f"{who}: {m.phrase} {OP_PHRASE.get(op, op)} {fmt_value(m.unit, value)}"


@dataclass(frozen=True)
class Template:
    id: str
    kind: str
    title: str
    why: str
    metric: str
    op: str
    value: str
    priority: str = "default"
    params: dict[str, str] = field(default_factory=dict)


TEMPLATES: list[Template] = [
    Template(
        "ipo-qib-strong",
        "ipo",
        "Institutions piling in",
        "Tell me when QIBs are 10x or more.",
        "qib_times",
        ">=",
        "10",
    ),
    Template(
        "ipo-last-day",
        "ipo",
        "Last bidding day",
        "A nudge to approve the UPI mandate in time.",
        "bidding_days_left",
        "<=",
        "1",
        "high",
    ),
    Template(
        "stock-big-fall",
        "stock",
        "Big fall today",
        "A 5 % drop in a day on a watched stock.",
        "day_change_pct",
        "<=",
        "-5",
        "high",
    ),
    Template(
        "stock-near-high",
        "stock",
        "Near the 52-week high",
        "Within 2 % of the 52-week high.",
        "pct_from_52w_high",
        ">=",
        "-2",
    ),
    Template(
        "stock-below-200dma",
        "stock",
        "Below the 200-day average",
        "The trend turned down.",
        "pct_vs_200dma",
        "<",
        "0",
    ),
    Template(
        "stock-signal-change",
        "stock",
        "Signal changed",
        "The stock signal moved to another action.",
        "signal_action_changed",
        "==",
        "1",
    ),
    Template(
        "stock-red-flag",
        "stock",
        "Forensic red flag",
        "Any forensic screening score raises a red flag.",
        "forensic_red_flags",
        ">=",
        "1",
    ),
    Template(
        "stock-ex-date",
        "stock",
        "Ex-date this week",
        "Dividend or split going ex within 7 days.",
        "days_to_ex_date",
        "<=",
        "7",
    ),
    Template(
        "stock-results",
        "stock",
        "Results filed",
        "New quarterly results are out.",
        "days_since_results",
        "<=",
        "1",
    ),
    Template(
        "stock-promoter-sells",
        "stock",
        "Promoters selling",
        "Promoter holding fell 1 pp or more.",
        "promoter_change_pp",
        "<=",
        "-1",
        "high",
    ),
    Template(
        "fund-nav-drop",
        "fund",
        "NAV drop",
        "The NAV fell 3 % or more in a day.",
        "nav_change_pct",
        "<=",
        "-3",
    ),
    Template(
        "fund-lagging",
        "fund",
        "Lagging its category",
        "Beat the category median in under 40 % of 3-year windows (switch review).",
        "hit_rate_3y_pct",
        "<",
        "40",
    ),
    Template(
        "fund-ter-up",
        "fund",
        "Expense ratio went up",
        "TER rose 0.10 pp or more.",
        "ter_change_pp",
        ">=",
        "0.1",
    ),
    Template(
        "bond-rating",
        "bond",
        "Rating changed",
        "NSE's rating string changed.",
        "rating_changed",
        "==",
        "1",
        "high",
    ),
    Template(
        "bond-coupon",
        "bond",
        "Coupon this week",
        "The next interest date is within 7 days.",
        "days_to_coupon",
        "<=",
        "7",
        "low",
    ),
    Template(
        "fno-iv-high",
        "fno",
        "IV is high",
        "ATM IV in the top 20 % of its recorded year.",
        "iv_percentile",
        ">=",
        "80",
    ),
    Template(
        "fno-expiry",
        "fno",
        "Expiry approaching",
        "Two days or less to the nearest expiry.",
        "days_to_expiry",
        "<=",
        "2",
    ),
    Template(
        "fno-stop",
        "fno",
        "Strategy stop",
        "The legs have lost half of their maximum loss.",
        "strategy_pnl_pct_of_max_loss",
        "<=",
        "-50",
        "urgent",
    ),
    Template(
        "pf-drift",
        "portfolio",
        "Rebalance",
        "An asset class drifted 5 pp from its target.",
        "allocation_drift_pp",
        ">=",
        "5",
    ),
    Template(
        "pf-drawdown",
        "portfolio",
        "Portfolio drawdown",
        "The portfolio is 10 % below its peak.",
        "drawdown_pct",
        "<=",
        "-10",
        "high",
    ),
]


def registry_json() -> dict[str, Any]:
    return {"kinds": list(KINDS), "metrics": [m.json() for m in METRICS],
            "templates": [asdict(t) for t in TEMPLATES], "ops": OP_PHRASE}  # fmt: skip

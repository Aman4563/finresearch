"""The alert-metric registry: for each asset kind, the metrics the app can actually compute, with a plain-English
phrase, a unit, a source and the cadence the monitor checks it on.

Only metrics with a working data path are listed. Left out on purpose (not computable today): P/E percentile alerts
(needs 120 days of daily P/E; shown on the signal instead) and bond spread over G-sec. Fund category rank is
computable since issue #177: signals.fund_rank ranks every scheme within its SEBI category from AMFI month-end NAVs
once a day (monitor.fund_ranks), and `category_rank_drop` reads that stored ranking. Promoter pledge, surveillance stages, the F&O ban, insider trades, bulk/block deals, credit-rating actions
and SEBI orders come from finresearch.disclosures (stored by the monitor's daily disclosure refresh; the checks read
the database only, so they run on the cheap intraday cadence).
Portfolio risk metrics that need a reconstructed value history, fund look-through or household data (volatility,
beta, VaR, underperformance vs an index, fund overlap, business-group share, emergency-fund months, cash share,
stale manual entries) are left for the portfolio-analytics, look-through and wealth work that computes them.

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
_DISC = "https://www.nseindia.com/companies-listing/"


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


_PF = "the monitor's daily portfolio pass"
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
        "signals.fund.analyse (AMFI NAV history; same-plan growth peers, one per scheme, in the SEBI category key)",
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
        "category_rank_drop",
        "Category rank drop",
        "fall in category percentile since the last alert",
        "pp",
        "daily",
        "How many percentile points the fund's rank within its SEBI category fell since the rule was created or "
        "last fired (positive = it fell; 20 = from beating 80 % of its category to beating 60 %). Ranked on the "
        "3-year CAGR unless the rule's `basis` param names another metric (cagr_1y, cagr_5y, consistency_3y, "
        "sortino_3y, max_drawdown_3y, ter). Direct-growth plans, as of the last month-end; a regular plan is "
        "ranked through its scheme's direct plan. Ranks are among surviving schemes.",
        "signals.fund_rank (AMFI month-end NAVs, NAVAll categories, TER file), computed daily by the monitor",
        rebase="on_fire",
        default_op=">=",
        default_value="20",
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
        "Rating text changed (legacy)",
        "the credit rating",
        "flag",
        "daily",
        "Legacy: fires when the rating string in NSE's bond list differs from the last check (for example AA to AA-). "
        'Prefer "Adverse rating action", which reads the agency\'s action and outlook.',
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
    # the daily layer: read from what the monitor's daily portfolio pass stores (monitor.portfolio_daily)
    _m(
        "portfolio",
        "holding_day_move_pct",
        "Holding moved a lot",
        "largest one-day move of a holding (either way)",
        "pct",
        "daily",
        "The biggest price change of any holding since the previous daily valuation, in % and "
        "ignoring the sign (a 6 % fall reads 6). The alert names the holding and the direction.",
        f"{_PF}: holding prices on consecutive valuation days",
        default_op=">=",
        default_value="5",
        allows_all=False,
    ),
    _m(
        "portfolio",
        "holding_signal_changed",
        "Signal changed on a holding",
        "a holding's signal",
        "flag",
        "daily",
        "Fires when the signal of a stock or fund you hold moves to another action (for example HOLD to REDUCE). "
        "Signals are computed once a day and never written to the forecast ledger by this check.",
        f"{_PF}: signals.stock / signals.fund",
        event=True,
        event_text="a holding's signal action changed",
        default_op="==",
        default_value="1",
        allows_all=False,
    ),
    _m(
        "portfolio",
        "reduce_signal_weight_pct",
        "Weight under REDUCE/SELL signals",
        "share of the portfolio whose signal says REDUCE or SELL",
        "pct",
        "daily",
        "The part of your portfolio's value in holdings whose current signal is REDUCE or SELL. The stock signal "
        "showed no edge over an equal-weight Nifty 50 backtest: treat this as risk hygiene, not a forecast.",
        f"{_PF}: holdings' signals and weights",
        default_op=">=",
        default_value="10",
        allows_all=False,
    ),
    _m(
        "portfolio",
        "days_to_next_lt_lot",
        "Lot turning long-term",
        "days until a lot in profit turns long-term",
        "days",
        "daily",
        "Days until the next open lot with a gain crosses the long-term holding period (12 months "
        "for listed equity, 24 for other listed securities). Selling after that date is usually taxed less.",
        f"{_PF}: open FIFO lots + fincalc.tax holding periods",
        default_op="<=",
        default_value="7",
        allows_all=False,
    ),
    _m(
        "portfolio",
        "days_to_elss_unlock",
        "ELSS units unlock",
        "days until locked ELSS units unlock",
        "days",
        "daily",
        "Days until the next lot of an ELSS (tax-saver) fund you hold finishes its 3-year lock-in. Every SIP "
        "instalment and IDCW reinvestment is locked 3 years from its own allotment (ELSS Scheme 2005); units can "
        "be redeemed from the day after the third anniversary. The fund is recognised by AMFI's category, else by "
        "its name.",
        "open FIFO lots of your funds + portfolio.elss (AMFI category from the daily portfolio pass)",
        default_op="<=",
        default_value="7",
        allows_all=False,
    ),
    _m(
        "portfolio",
        "lt_wait_tax_saved_inr",
        "Tax saved by waiting",
        "tax saved by waiting for lots to turn long-term",
        "inr",
        "daily",
        "For lots in profit that turn long-term within 30 days: the tax a sale would cost today minus "
        "the tax after the date, at today's price, after this year's set-off and exemption. FIFO applies: older lots "
        "of the same holding sell first.",
        f"{_PF}: fincalc.tax.tax_delta",
        default_op=">=",
        default_value="2000",
        allows_all=False,
    ),
    _m(
        "portfolio",
        "ltcg_used_pct",
        "LTCG exemption used",
        "share of this year's LTCG exemption used",
        "pct",
        "daily",
        "How much of the ₹1.25 lakh yearly exemption on equity long-term gains your realised gains have used.",
        "finresearch.portfolio tax view (realised gains this financial year)",
        default_op=">=",
        default_value="80",
        allows_all=False,
    ),
    _m(
        "portfolio",
        "max_position_pct",
        "Largest position",
        "largest single position",
        "pct",
        "daily",
        "The biggest holding's share of the portfolio (a fund counts as one holding: its stocks are not looked "
        "through). The suggested threshold is your single-stock limit (portfolio.limits.position_limit: the "
        "profile's max position, else 5 / 8 / 10 % by risk appetite), the same one /portfolio flags.",
        f"{_PF}: holding values",
        default_op=">=",
        default_value="8",
        allows_all=False,
    ),
    _m(
        "portfolio",
        "max_sector_pct",
        "Largest sector",
        "largest sector of directly held stocks",
        "pct",
        "daily",
        "The biggest NSE industry among stocks you hold directly, as a share of the whole portfolio (funds not looked "
        "through). The default 25 % is a rule of thumb.",
        f"{_PF}: NSE industry per stock",
        default_op=">=",
        default_value="25",
        allows_all=False,
    ),
    _m(
        "portfolio",
        "n_effective",
        "Effective number of holdings",
        "effective number of holdings",
        "count",
        "daily",
        "1 / (sum of squared weights): how many equal-sized holdings would be as concentrated. Ten holdings "
        "where one is 60 % count as about 2.4.",
        f"{_PF}: holding values",
        default_op="<",
        default_value="5",
        allows_all=False,
    ),
    _m(
        "portfolio",
        "sip_missed",
        "SIP missed",
        "SIPs missed or stopped",
        "count",
        "daily",
        "Mutual-fund SIPs (inferred from regular monthly purchases) with no instalment for more than 35 days.",
        "portfolio.sip over your fund transactions",
        default_op=">=",
        default_value="1",
        allows_all=False,
    ),
    _m(
        "portfolio",
        "dividend_received",
        "Dividend received",
        "a dividend",
        "flag",
        "daily",
        "Fires when a dividend on one of your holdings is recorded (imported or entered) since the previous daily pass.",
        f"{_PF}: dividend transactions",
        event=True,
        event_text="a dividend was recorded",
        default_op="==",
        default_value="1",
        allows_all=False,
    ),
    _m(
        "portfolio",
        "days_to_holding_ex_date",
        "Ex-date on a holding",
        "days to the next ex-date of a holding",
        "days",
        "daily",
        "Calendar days to the next dividend, split or bonus ex-date of a stock you hold.",
        f"{_PF}: NSE corporate actions",
        default_op="<=",
        default_value="7",
        allows_all=False,
    ),
    _m(
        "portfolio",
        "days_to_holding_results_meeting",
        "Results date of a holding",
        "days to the next results board meeting of a holding",
        "days",
        "daily",
        "Calendar days to the next board meeting that will consider financial results, for a stock you hold.",
        f"{_PF}: NSE board meetings (https://www.nseindia.com/api/corporate-board-meetings)",
        default_op="<=",
        default_value="3",
        allows_all=False,
    ),
    _m(
        "portfolio",
        "days_since_holding_results",
        "Results filed by a holding",
        "days since a holding last filed results",
        "days",
        "daily",
        "Calendar days since the most recent financial-results filing by any stock you hold (0 on the filing day).",
        f"{_PF}: NSE financial-results filings",
        default_op="<=",
        default_value="1",
        allows_all=False,
    ),
    _m(
        "portfolio",
        "fund_ter_change_pp",
        "Fund TER changed",
        "change in a held fund's expense ratio",
        "pp",
        "daily",
        "The largest change in the TER of a fund you hold, on the day AMFI's file shows it (your plan: "
        "direct or regular). Positive = more expensive.",
        f"{_PF}: AMFI TER file ({AMFI_TER})",
        default_op=">=",
        default_value="0.1",
        allows_all=False,
    ),
    _m(
        "portfolio",
        "advance_tax_due_inr",
        "Advance tax estimate",
        "advance tax due by the next instalment on this year's gains and dividends",
        "inr",
        "daily",
        "The schedule's cumulative share (15/45/75/100 % by 15-Jun/Sep/Dec/Mar) of the tax on capital gains realised "
        "and dividends recorded this year; 0 below the ₹10,000 threshold. Salary, TDS and tax already paid are "
        "unknown to the app. [unverified] schedule: check incometax.gov.in each year.",
        "fincalc.tax_calendar (dated rule table)",
        default_op=">=",
        default_value="10000",
        allows_all=False,
    ),
    _m(
        "portfolio",
        "regular_plan_value_inr",
        "Money in regular plans",
        "value held in regular fund plans",
        "inr",
        "daily",
        "The value of mutual funds held in a regular plan (with distributor commission) rather than direct. "
        "Switching is a redemption: tax and exit load apply, so check the break-even first.",
        f"{_PF}: plan from the scheme name",
        default_op=">",
        default_value="0",
        allows_all=False,
    ),
    _m(
        "portfolio",
        "unpriced_holdings",
        "Holdings without a fresh price",
        "holdings without a fresh price",
        "count",
        "daily",
        "Holdings with no price, a price older than four days or only a statement NAV (data health).",
        f"{_PF}: price sources",
        default_op=">=",
        default_value="1",
        allows_all=False,
    ),
    # ------------------------------------------------------------------ disclosures (finresearch.disclosures)
    # Read from the database (monitor.disclosures refreshes it after each close and before the open); a feed with no
    # recent good read makes the value unknown, so nothing fires on a failure.
    _m(
        "stock",
        "pledge_pct",
        "Promoter pledge",
        "promoter shares pledged (% of promoter holding)",
        "pct",
        "intraday",
        "Promoter shares encumbered (pledged or otherwise, SEBI SAST Regulation 31) at the latest quarter end, as a % "
        "of the promoter holding, recomputed from NSE's share counts. High pledges can force sales if the price falls.",
        f"NSE pledged data: {_DISC}corporate-filings-pledged-data",
        default_op=">=",
        default_value="10",
    ),
    _m(
        "stock",
        "pledge_change_pp",
        "Promoter pledge change",
        "change in promoter pledge over the last quarter",
        "pp",
        "intraday",
        "The pledged share of the promoter holding in the latest quarter minus the quarter before, in percentage "
        "points (unknown until two quarters are recorded).",
        f"NSE pledged data (quarters recorded by the monitor): {_DISC}corporate-filings-pledged-data",
        default_op=">",
        default_value="0",
    ),
    _m(
        "stock",
        "surveillance_stage",
        "Surveillance stage changed",
        "the NSE surveillance stage",
        "flag",
        "intraday",
        "Fires when the stock enters, changes stage on or leaves NSE's ASM or GSM list (including IBC and ESM "
        "markers). These lists mean higher margins or trade-for-trade settlement, which make an exit costlier.",
        "NSE ASM and GSM reports: https://www.nseindia.com/reports/asm",
        event=True,
        event_text="its NSE surveillance stage changed",
        rebase="always",
        default_op="==",
        default_value="1",
    ),
    _m(
        "stock",
        "in_fno_ban",
        "In the F&O ban",
        "in the F&O ban period (1 = yes)",
        "flag",
        "intraday",
        "1 while the stock is in NSE's F&O ban list for today's trade date (open interest above 95 % of the market-"
        "wide position limit: positions can only be reduced).",
        "NSE securities in ban period: https://nsearchives.nseindia.com/content/fo/fo_secban.csv",
        default_op="==",
        default_value="1",
    ),
    _m(
        "stock",
        "insider_net_buy_90d",
        "Net insider buying (90 days)",
        "net insider buying over 90 days",
        "inr",
        "intraday",
        "Open-market purchases minus sales of the company's shares by insiders (promoters, directors, KMP, designated "
        "persons) in the last 90 days, in ₹ as filed under SEBI PIT Regulation 7(2). ESOP, gift, inter-se and "
        "off-market transfers are excluded. Unknown while any filing in the window is unread. Context, not a signal.",
        f"NSE insider-trading filings (XBRL): {_DISC}corporate-filings-insider-trading",
        default_op=">",
        default_value="0",
    ),
    _m(
        "stock",
        "bulk_block_deals_5d",
        "Bulk or block deals",
        "bulk and block deals in the last 5 days",
        "count",
        "intraday",
        "Bulk deals (a client trading over 0.5 % of the shares in a day) and block deals in the stock on NSE over the "
        "last 5 calendar days.",
        "NSE bulk and block deals: https://www.nseindia.com/report-detail/display-bulk-and-block-deals",
        default_op=">=",
        default_value="1",
    ),
    _m(
        "stock",
        "rating_action",
        "Adverse rating action",
        "an adverse credit-rating action on the company",
        "flag",
        "intraday",
        "Fires on a new downgrade, negative watch, negative outlook, default or issuer-not-cooperating rating on any "
        "instrument of the company (matched by ISIN issuer code), from the issuer's own rating filings on NSE.",
        f"NSE credit-rating filings: {_DISC}corporate-filings-credit-rating",
        event=True,
        event_text="a credit-rating agency took an adverse action",
        rebase="always",
        default_op="==",
        default_value="1",
    ),
    _m(
        "stock",
        "sebi_order",
        "SEBI order",
        "a SEBI order naming the company",
        "flag",
        "intraday",
        "Fires on a new SEBI enforcement order (adjudication, settlement, final, recovery) whose title names the "
        "company's legal name: a possible match, open the order to confirm.",
        "SEBI RSS: https://www.sebi.gov.in/sebirss.xml",
        event=True,
        event_text="a SEBI order may name it",
        rebase="always",
        default_op="==",
        default_value="1",
    ),
    _m(
        "bond",
        "rating_action",
        "Adverse rating action",
        "an adverse credit-rating action",
        "flag",
        "intraday",
        "Fires on a new downgrade, negative watch, negative outlook, default or issuer-not-cooperating rating on this "
        "bond or another instrument of its issuer (ISIN issuer code), from the issuer's rating filings on NSE: the "
        "agency's action and outlook, not just a changed rating string.",
        f"NSE credit-rating filings: {_DISC}corporate-filings-credit-rating",
        event=True,
        event_text="a credit-rating agency took an adverse action",
        rebase="always",
        default_op="==",
        default_value="1",
    ),
    _m(
        "fno",
        "in_ban",
        "In the F&O ban",
        "in the F&O ban period (1 = yes)",
        "flag",
        "intraday",
        "1 while the underlying is in NSE's F&O ban list for today's trade date: new positions are not allowed, "
        "existing ones can only be reduced.",
        "NSE securities in ban period: https://nsearchives.nseindia.com/content/fo/fo_secban.csv",
        default_op="==",
        default_value="1",
    ),
    _m(
        "portfolio",
        "holdings_red_flags",
        "Red flags on holdings",
        "number of held stocks with a red flag",
        "count",
        "intraday",
        "Held stocks on NSE's ASM or GSM list, in the F&O ban, or with a promoter pledge that rose last quarter. "
        "Unknown when a list could not be read.",
        "finresearch.disclosures (NSE ASM/GSM, F&O ban, pledged data)",
        default_op=">=",
        default_value="1",
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
        "stock-surveillance",
        "stock",
        "Surveillance stage changed",
        "The stock entered, moved within or left NSE's ASM/GSM lists.",
        "surveillance_stage",
        "==",
        "1",
        "high",
    ),
    Template(
        "stock-pledge-up",
        "stock",
        "Promoter pledge rising",
        "Promoters pledged more of their holding last quarter.",
        "pledge_change_pp",
        ">",
        "0",
    ),
    Template(
        "stock-insider-buying",
        "stock",
        "Insiders buying",
        "Net open-market insider buying over 90 days.",
        "insider_net_buy_90d",
        ">",
        "0",
        "low",
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
        "Rating downgraded or on watch",
        "An agency downgraded the issuer, put it on negative watch or turned the outlook negative.",
        "rating_action",
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
        "pf-holding-move",
        "portfolio",
        "A holding moved 5 %",
        "Any holding up or down 5 % or more in a day.",
        "holding_day_move_pct",
        ">=",
        "5",
    ),
    Template(
        "pf-signal-change",
        "portfolio",
        "Signal changed on a holding",
        "A stock or fund you hold changed signal.",
        "holding_signal_changed",
        "==",
        "1",
    ),
    Template(
        "pf-lt-soon",
        "portfolio",
        "Lot turning long-term",
        "A lot in profit turns long-term within a week.",
        "days_to_next_lt_lot",
        "<=",
        "7",
    ),
    Template(
        "pf-elss-unlock",
        "portfolio",
        "ELSS units unlock",
        "Locked ELSS units finish their 3-year lock-in within a week.",
        "days_to_elss_unlock",
        "<=",
        "7",
    ),
    Template(
        "pf-sip-missed",
        "portfolio",
        "SIP missed",
        "A monthly SIP has no instalment for over 35 days.",
        "sip_missed",
        ">=",
        "1",
        "high",
    ),
    Template(
        "pf-ter-up",
        "portfolio",
        "Fund got costlier",
        "A held fund's TER rose 0.10 pp or more.",
        "fund_ter_change_pp",
        ">=",
        "0.1",
    ),
    Template(
        "pf-concentration",
        "portfolio",
        "Position too big",
        "One holding is over your single-stock limit (by default 8 %, the medium-risk rule of thumb).",
        "max_position_pct",
        ">=",
        "8",
    ),
    Template(
        "pf-red-flags",
        "portfolio",
        "Red flag on a holding",
        "A held stock is under surveillance, in the F&O ban or its promoters pledged more.",
        "holdings_red_flags",
        ">=",
        "1",
        "high",
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


def registry_json(limit: Any = None) -> dict[str, Any]:
    """The catalogue for the rule builder. `limit` (a portfolio.limits.PositionLimit, from the profile) fills the
    "Largest position" default and the "Position too big" template with the one single-stock limit (#194)."""
    metrics = [m.json() for m in METRICS]
    templates = [asdict(t) for t in TEMPLATES]
    if limit is not None:
        v = f"{limit.pct:g}"
        for m in metrics:
            if m["key"] == "max_position_pct" and m["kind"] == "portfolio":
                m["default_value"] = v
        for t in templates:
            if t["id"] == "pf-concentration":
                t["value"] = v
                t["why"] = f"One holding is over your single-stock limit ({limit.rule})."
    return {"kinds": list(KINDS), "metrics": metrics, "templates": templates, "ops": OP_PHRASE}

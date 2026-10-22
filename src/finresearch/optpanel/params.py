"""Every numeric setting of the stock-option panel and its study (P1.12, #374), registered in optpanel/specs.py and
pinned by optpanel/prereg.HASH before any panel code existed. The panel and ban modules read these at call time, so
editing one moves the registration hash and the loader refuses to run until a new SPEC_VERSION is registered
(evals/experiments/stock_option_vrp/ADDENDUM.md, never PREREG.md). Engine settings the panel reuses unchanged
(fincalc.fno_history's ATM band, minimum contracts, rate, 30-day target, RV window; backtest.specs.BOOK's slippage
tiers) are hashed from their own modules by specs.settings()."""

# ---- panel
# index options added to the panel next to every stock option (the bhavcopy store keeps NIFTYNXT50 futures only)
INDEX_UNDERLYINGS: tuple[str, ...] = ("NIFTYNXT50",)
ROWS_LN_BAND = 0.2  # full contract rows kept only for |ln(K/F)| <= this ...
ROWS_EXPIRIES = 2  # ... and the two nearest expiries on or after the day
LIQ_BAND = 0.05  # liquidity: option contracts traded with |K/F - 1| <= this
BUCKET_EDGES: tuple[float, ...] = (-0.10, -0.03, 0.03, 0.10)  # ln(K/F) edges of the OI and volume buckets

# ---- ban period (NSE's market-wide position limit rule; see optpanel/ban.py for the source)
BAN_ENTER = 0.95  # a stock enters the ban when its combined OI exceeds this fraction of MWPL ...
BAN_EXIT = 0.80  # ... and leaves it only once it falls below this fraction

# ---- the study
SAMPLE_START = "2022-01-01"  # the first formation day (the stored history starts in January 2022)
SAMPLE_END = "2026-10-22"  # cycles whose expiry is on or before the registration day
SPLIT = "2024-06-30"  # the two halves the decision rule reads (formation day on or before / after)
COVERAGE_SESSIONS = 60  # eligible only with a fitted ATM IV on >= MIN_COVERAGE of these stored sessions
MIN_COVERAGE = 0.5
MIN_CROSS_SECTION = 50  # a cycle counts only with at least this many eligible stocks
QUANTILES: tuple[int, ...] = (10, 5)  # deciles (primary), quintiles (secondary); Holm across both
ALPHA = 0.05  # one-sided, Holm-adjusted
STRESS_SLIPPAGE_MULT = 2.0  # the decision also needs a positive mean spread with slippage x this
FORWARD_MIN_CYCLES = 24  # the forward confirmation is read once this many post-registration cycles exist

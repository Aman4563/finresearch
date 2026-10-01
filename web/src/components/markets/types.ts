// Shapes of the market-data API behind the Stocks, Mutual funds and Bonds pages (src/finresearch/api/markets.py).

export type Exchange = "NSE" | "BSE";

/** A search hit across NSE and BSE (matched by ISIN). `key` is the page / API key: the NSE symbol, or "BSE:<code>"
 * for a stock that trades only on BSE. */
export type StockHit = {
  key: string; symbol: string; name: string; series: string | null; listed: string | null; isin: string; slug: string | null;
  exchange: "NSE" | "BSE" | "both"; exchanges: Exchange[]; nse_symbol: string | null; bse_code: string | null;
  bse_symbol: string | null; bse_group: string | null; market_cap_cr: number | null; bse_error: string | null;
  /** where market_cap_cr comes from (BSE's scrip master, read up to a day ago) and when it was read */
  market_cap_source?: string | null; market_cap_as_of?: string | null;
};

/** Where a stock trades: NSE, BSE or both (same ISIN). */
export type StockListing = {
  isin: string; exchange: "NSE" | "BSE" | "both"; exchanges: Exchange[]; nse_symbol: string | null; bse_code: string | null;
  bse_symbol: string | null; bse_group: string | null; bse_key: string | null; name: string;
};

export type StockQuote = {
  symbol: string;
  exchange?: Exchange;
  scrip_code?: string | null;
  isin?: string | null;
  company: string | null;
  industry: string | null;
  status: string | null;
  listing_date: string | null;
  as_of: string | null;
  /** the display price: last traded in session, the official close after it (fincalc/price.py) */
  price?: string | null;
  price_kind?: "last_traded" | "official_close" | "previous_close";
  price_label?: string;
  session?: "open" | "closing" | "closed";
  /** the last traded price (after the close it can differ from the official close) */
  last_price: string | null;
  official_close?: string | null;
  last_differs?: boolean;
  open: string | null;
  previous_close: string | null;
  /** what the day change is measured from: the exchange's adjusted base price on an ex-date, else the previous close */
  reference_price?: string | null;
  reference_kind?: string | null;
  reference_label?: string | null;
  change: string | null;
  change_pct: number | null;
  price_notes?: string[];
  quality?: QualityFlag[];
  week52_high: string | null;
  week52_low: string | null;
  week52_position: number | null;
  issued_shares: string | null;
  market_cap: string | null;
  market_cap_basis?: string | null;
};

/** Two published figures that disagree beyond tolerance: both are shown with their sources, never one picked silently. */
export type QualityFlag = { field: string; severity: "warn" | "info"; message: string; values: { value: string | null; source: string }[] };

export type StockOverview = {
  /** parts the exchange could not serve just now (network / refused); retried, never cached long */
  unreachable?: string[];
  symbol: string;
  exchange?: Exchange;
  key?: string;
  scrip_code?: string | null;
  listing?: StockListing | null;
  fetched_at: string;
  /** citation: the stock page, else the exact quote request */
  source: string;
  /** the exchange's human stock page (NSE get-quotes / BSE stock-share-price); null when BSE gives none */
  quote_page?: string | null;
  /** the exact API request behind each part (quote, shareholding, corporate_actions, announcements) */
  sources?: Record<string, string>;
  quote: StockQuote | null;
  dividends: { ttm_per_share: string | null; ttm_yield: number | null };
  shareholding: { as_of: string | null; promoter_pct: number | null; public_pct: number | null; employee_trusts_pct: number | null; xbrl: string | null }[];
  corporate_actions: { subject: string; ex_date: string | null; record_date: string | null; dividend_per_share: string | null; upcoming: boolean }[];
  announcements: { at: string | null; category: string; text: string; attachment: string | null; results_period_end: string | null }[];
  errors: string[];
};

export type ShareholdingQuarter = {
  as_of: string;
  submitted: string | null;
  xbrl: string;
  taxonomy: string | null;
  categories: Record<string, number | null>;
  groups: Record<string, number | null>;
  shareholders: Record<string, number>;
  dr_pct_of_total_shares: number | null;
  warnings: string[];
};

export type StockShareholding = {
  /** parts the exchange could not serve just now (network / refused); retried, never cached long */
  unreachable?: string[];
  symbol: string;
  exchange?: "NSE" | "BSE";
  fetched_at: string;
  basis: string;
  category_labels: { key: string; label: string; group: string }[];
  group_labels: { key: string; label: string }[];
  quarters: ShareholdingQuarter[];
  errors: string[];
  source: string;
};

export type SeriesStats = {
  return?: number;
  annualised_volatility?: number;
  max_drawdown?: number;
  drawdown_peak?: string;
  drawdown_trough?: string;
  points?: number;
};

export type StockHistory = {
  /** parts the exchange could not serve just now (network / refused); retried, never cached long */
  unreachable?: string[];
  symbol: string;
  days: number;
  partial: boolean;
  bars: { date: string; close: number; open: number | null; high: number | null; low: number | null; volume: number | null }[];
  week52_high: string | null;
  week52_low: string | null;
  stats: SeriesStats;
};

export type ResultPeriod = {
  label: string; // "Q1 FY27" (quarters) or "FY26" (years)
  period_start: string | null;
  period_end: string;
  consolidated: boolean;
  audited: boolean | null;
  filed_at: string | null;
  revised: boolean;
  source: "nse_integrated_filing" | "nse_financial_results";
  source_url: string;
  bank: boolean; // banking taxonomy: revenue is interest earned
  revenue_basis: "revenue_from_operations" | "interest_earned" | "net_premium_income" | "premium_earned" | null;
  revenue: number | null;
  other_income: number | null;
  total_income: number | null;
  total_expenses: number | null;
  exceptional_items: number | null;
  profit_before_tax: number | null;
  tax: number | null;
  net_profit: number | null;
  profit: number | null; // attributable to owners
  eps: number | null;
  eps_diluted: number | null;
  margin: number | null;
  pbt_margin: number | null;
  xbrl: string;
  ixbrl: string | null;
  growth: Partial<Record<"revenue_qoq" | "revenue_yoy" | "profit_qoq" | "profit_yoy" | "eps_qoq" | "eps_yoy", number | null>>;
};

export type StockResults = {
  /** parts the exchange could not serve just now (network / refused); retried, never cached long */
  unreachable?: string[];
  symbol: string;
  unit: string;
  quarters: ResultPeriod[];
  annual: ResultPeriod[];
  latest_quarter: Pick<ResultPeriod, "label" | "period_end" | "filed_at" | "source" | "source_url" | "consolidated" | "xbrl" | "ixbrl"> | null;
  as_of: string;
  sources: { name: string; url: string; note: string }[];
  errors: string[];
  source: string;
};

export type Scheme = {
  scheme_code: string;
  name: string;
  plan: string | null;
  option: string | null;
  category: string | null;
  amc: string | null;
  nav: string | null;
  nav_date: string | null;
  slug?: string | null;
  isin?: string | null;
};

export type Rolling = {
  count: number;
  min: number;
  p25: number;
  median: number;
  p75: number;
  max: number;
  share_positive: number;
  share_above_rf: number;
  histogram: { from: number; to: number; count: number }[];
  series: { date: string; return: number }[];
} | null;

export type FundAnalytics = {
  scheme: Scheme;
  years: number;
  source: string;
  navs: { date: string; nav: number }[];
  trailing: { "1y"?: number | null; "3y"?: number | null; "5y"?: number | null; since_start?: number | null };
  rolling: { "1y"?: Rolling; "3y"?: Rolling };
  risk: SeriesStats & { risk_free_annual?: number; sharpe?: number | null; sortino?: number | null };
};

export type SipResult = {
  scheme_code: string;
  amount: string;
  years: number;
  day: number;
  start: string;
  end: string;
  sip: { invested: number; value: number; units: number; gain: number; xirr: number; instalments: number };
  lump_sum: { invested: number; value: number; gain: number; cagr: number | null };
  path: { date: string; invested: number; value: number; lump_sum: number | null }[];
};

export type PeerPeriod = {
  count: number;
  scheme: number | null;
  p25: number | null;
  median: number | null;
  p75: number | null;
  best: number | null;
  rank: number | null;
  percentile: number | null;
};

export type FundPeers = { category: string | null; peers: number; as_of: string; periods: Record<"1y" | "3y" | "5y", PeerPeriod> };

export type Bond = {
  symbol: string;
  series: string | null;
  isin: string;
  coupon_pct: string | null;
  face_value: string | null;
  last_price: string | null;
  close?: string | null;
  maturity: string | null;
  next_interest_date?: string | null;
  rating: string | null;
  rating_agency: string | null;
  traded_value?: string | null;
  as_of?: string | null;
  warnings: string[];
  slug: string | null;
};

export type BondAnalytics = {
  bond: Bond;
  warnings: string[];
  freq: number;
  /** where the frequency came from: the user's choice, a verified claim of the bond's research, or an assumption */
  freq_source: { kind: "chosen" } | { kind: "assumed" } | { kind: "verified"; claim_id: number; run_id: number };
  basis: "dirty" | "clean";
  tax_slab_pct: string;
  tax_rate: number;
  source: string;
  error: string | null;
  analytics: {
    settlement: string;
    price: string;
    clean_price: number;
    dirty_price: number;
    accrued_interest: number;
    ytm: number;
    current_yield: number;
    after_tax_ytm: number;
    macaulay_duration: number;
    modified_duration: number;
    convexity: number;
    years_to_maturity: number;
    premium_pct: number;
    coupon_per_payment: number;
    cash_flows: { date: string; coupon: number; principal: number; coupon_after_tax: number; periods: number }[];
    totals: { coupons: number; principal: number; received: number; paid_today: number };
    curve: { bp: number; yield: number; dirty: number; clean: number }[];
    sensitivity: { bp: number; price: number; change_pct: number; duration_estimate_pct: number }[];
    conventions: string;
  } | null;
};

/** Fund category rank (GET /api/funds/{code}/category-rank; signals.fund_rank, computed daily by the monitor). */
export type RankMetricKey = "cagr_1y" | "cagr_3y" | "cagr_5y" | "consistency_3y" | "sortino_3y" | "max_drawdown_3y" | "ter";
export type RankMetric = { key: RankMetricKey; label: string; higher_is_better: boolean; unit: "fraction" | "ratio" | "pct" };
export type RankedFund = {
  code: string; name: string; amc: string | null;
  values: Record<RankMetricKey, number | null>;
  ranks: Record<RankMetricKey, number | null>;
  percentiles: Record<RankMetricKey, number | null>;
  no_downside: boolean;
  missing: Partial<Record<RankMetricKey, string>>;
};
export type FundCategoryRank = {
  status: "ok" | "not_computed" | "not_ranked" | "excluded" | "unknown";
  message?: string; reason?: string; label?: string | null; via?: string | null;
  as_of?: string; generated_at?: string; sampling?: string; mar?: number; ter_day?: string | null; ter_error?: string | null;
  metrics?: RankMetric[]; caveats?: string[]; sources?: string[];
  fund?: RankedFund; size?: number; counts?: Record<RankMetricKey, number>;
  category?: {
    key: string; label: string; raw_labels: string[]; size: number;
    counts: Record<RankMetricKey, number>; ranked: Record<RankMetricKey, boolean>;
    funds: RankedFund[]; excluded: { code: string; name: string; reason: string }[];
  };
};
export type RankSummary = {
  code: string; status: FundCategoryRank["status"]; metric: RankMetricKey; as_of: string | null; label: string | null;
  via: string | null; reason: string | null; rank: number | null; of: number | null; percentile: number | null;
  value: number | null; category_size: number | null;
};
export type CategoryRanks = { status: "ok" | "not_computed"; message?: string; as_of?: string; metric?: RankMetricKey; caveats?: string[]; ranks: Record<string, RankSummary> };

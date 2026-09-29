// Shapes of the market-data API behind the Stocks, Mutual funds and Bonds pages (src/finresearch/api/markets.py).

export type StockHit = { symbol: string; name: string; series: string; listed: string | null; isin: string; slug: string | null };

export type StockQuote = {
  symbol: string;
  company: string | null;
  industry: string | null;
  status: string | null;
  listing_date: string | null;
  as_of: string | null;
  last_price: string | null;
  open: string | null;
  previous_close: string | null;
  change: string | null;
  change_pct: number | null;
  week52_high: string | null;
  week52_low: string | null;
  week52_position: number | null;
  issued_shares: string | null;
  market_cap: string | null;
};

export type StockOverview = {
  symbol: string;
  fetched_at: string;
  source: string;
  quote: StockQuote | null;
  dividends: { ttm_per_share: string | null; ttm_yield: number | null };
  shareholding: { as_of: string | null; promoter_pct: number | null; public_pct: number | null; employee_trusts_pct: number | null; xbrl: string | null }[];
  corporate_actions: { subject: string; ex_date: string | null; record_date: string | null; dividend_per_share: string | null; upcoming: boolean }[];
  announcements: { at: string | null; category: string; text: string; attachment: string | null; results_period_end: string | null }[];
  errors: string[];
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
  symbol: string;
  days: number;
  partial: boolean;
  bars: { date: string; close: number; open: number | null; high: number | null; low: number | null; volume: number | null }[];
  week52_high: string | null;
  week52_low: string | null;
  stats: SeriesStats;
};

export type StockResults = {
  symbol: string;
  quarters: {
    period_start: string | null;
    period_end: string;
    consolidated: boolean;
    audited: boolean | null;
    filed_at: string | null;
    revenue: number | null;
    total_income: number | null;
    profit: number | null;
    eps: number | null;
    margin: number | null;
    xbrl: string;
  }[];
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

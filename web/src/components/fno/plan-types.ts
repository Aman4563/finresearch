// Trade-plan cards and the market-context panel (#317): the shapes of POST /api/fno/plan, GET /api/fno/{symbol}/suggest
// and GET /api/fno/{symbol}/context.

/** PAPER (#350): every check passes but no matched spec passed out of sample: paper only, 0 real-money lots */
export type PlanAction = "ENTER" | "PAPER" | "WAIT" | "AVOID";

export type Levels = { below: number | null; above: number | null };
export type ExitLevel = { rupees: number; premium_per_share: number | null; underlying_now: Levels; underlying_at_expiry: Levels };

export type EntryLeg = {
  right: string; strike: number; side: "buy" | "sell"; lots: number; bid: number | null; ask: number | null; last: number | null;
  mid: number | null; limit_from: number | null; limit_to: number | null; spread_pct: number | null; slippage: number | null; status: "ok" | "unknown";
};

export type PlanEvidence = {
  spec_key: string | null; spec_name: string | null; match: "exact" | "related" | null; status: string | null;
  /** #355: harmful (t ≤ −2 and CI below 0, delta-neutral recipes only) → AVOID; directional = one-sided or debit structures, whose backtest reflects the index path */
  level: "proven" | "harmful" | "inconclusive" | "directional" | "none"; raw_level: string; text: string; url: string;
};

export type PaperRecord = {
  trades: number; open: number; closed: number; wins: number; losses: number; total_net: number | null;
  expectancy: number | null; note: string;
};

export type ExitRuleSpec = { kind: "profit" | "stop" | "dte" | "delta" | "event"; threshold: number; why: string };

export type TradePlan = {
  title: string; symbol: string; expiry?: string; days_to_expiry?: number; spot?: number; lot_size?: number; as_of?: string | null;
  source?: string; kind: string; action: PlanAction; qualifier: string | null; label: string; failed: string[]; reasons: string[];
  spec_key?: string;
  legs: { right: "call" | "put" | "future"; strike: number; side: "buy" | "sell"; lots: number; qty?: number; last?: number }[];
  size?: { multiple: number; suggested: number; /** real-money lots (#350): 0 unless the matched spec is a candidate */ real_lots?: number;
    /** #358: the open-book cap: budget = 8% of F&O capital, open_risk = what open paper and Groww positions risk (null = unknown) */
    open_book?: { budget: number; budget_pct: number; open_risk: number | null; open_same: number; max_concurrent: number | null; mtd_paper: number | null; month_stop_pct: number; notes: string[] };
    budget: number; risk_per_set: number | null; reason: string; capital: number; max_loss_pct: number };
  entry?: { legs: EntryLeg[]; status: "ok" | "unknown"; net_mid: number | null; net_fill_now: number | null; notes: string[]; levels_priced_at: string;
    /** "bid/ask: NSE chain at HH:MM" (#324: NSE's chain when the plan's has no bid/ask), or "unknown" */
    source?: string; slippage_inr?: number | null };
  max_profit?: number | null; max_loss?: number | null; breakevens?: number[]; net_premium?: number; costs?: number | null;
  probability?: { real_world: number | null; real_world_window: string | null; risk_neutral: number | null; note: string };
  exits?: {
    source: string; rule_of_thumb: string | null; stop: ExitLevel | null; target: ExitLevel | null; entry_net: number; credit: boolean;
    per_share_unit: number | null; notes: string[]; time: { dte: number; by: string; text: string }; profit_pct: number | null; stop_pct: number | null;
  };
  exit_rules?: ExitRuleSpec[];
  adjustments?: { rules: string[]; note: string };
  invalidation?: string[];
  evidence: PlanEvidence;
  paper?: PaperRecord;
  notes?: string[];
  disclaimer?: string;
};

export type SuggestResponse = { symbol: string; today: string; cards: TradePlan[]; note: string };

export type Evidence = { measures: string; level: string; evidence: string };

export type MarketContext = {
  symbol: string; today: string; not_a_signal: string;
  /** the chain's expiry; defaulted (#324) to the nearest that is not today's when none was asked for */
  expiry?: string; expiry_defaulted?: boolean; expiry_note?: string | null;
  trend: {
    close: number | null; closes: number; source: string; trend: string; strength: string; note: string | null;
    averages: { days: number; sma: number | null; slope_pct_10d: number | null; distance_pct: number | null }[];
    adx: { adx: number; plus_di: number; minus_di: number; bars: number; on: string } | null;
    evidence: Evidence; adx_evidence: Evidence;
  };
  volatility: {
    atm_iv: number | null; realised_20d: number | null; realised_60d: number | null; iv_minus_rv20: number | null;
    iv_rank: number | null; iv_percentile: number | null; iv_days: number; iv_status: string; iv_source: string; skew_25d: number | null;
    evidence: Evidence; rank_evidence: Evidence; skew_evidence: Evidence;
  };
  term: { status?: string; note?: string; near_iv?: number | null; next_iv?: number | null; near_expiry?: string | null; next_expiry?: string | null; shape?: string | null; day?: string; evidence: Evidence };
  chain: { pcr_oi: number | null; pcr_volume: number | null; max_pain: number | null; spot: number | null; source: string; as_of: string | null; pcr_evidence: Evidence; max_pain_evidence: Evidence };
  buildup: {
    futures: { day: string; previous_day: string | null; rows: { expiry: string; close: number | null; price_change: number | null; oi: number | null; oi_change: number | null; buildup: string }[]; source: string } | null;
    options: { status: "ok" | "unknown"; note?: string; source: string; rows: { strike: number; call: BuildupCell; put: BuildupCell }[] } | null;
    evidence: Evidence; method: string;
  };
  participants: {
    status?: string; note?: string; day?: string; previous_day?: string | null; source?: string | null; url: string; evidence: Evidence;
    rows?: { participant: string; net: Record<string, number>; net_change: Record<string, number> | null }[];
  };
  events: { status?: string; items?: { day: string; kind: string; title: string }[]; rbi?: { status: string; note?: string }; evidence: Evidence };
};

export type BuildupCell = { price_change: number | null; oi_change: number | null; oi: number | null; buildup: string };

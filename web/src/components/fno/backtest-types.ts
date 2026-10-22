// Shapes of /api/fno/backtests (#307): the pre-registration, the latest saved evaluation and candidates' setups.

export type WindowStats = {
  from: string; to: string; trades: number; expiries: number; wins: number; losses: number;
  win_rate: number | null; expectancy: number | null; avg_win: number | null; avg_loss: number | null;
  worst_trade: number | null; best_trade: number | null; cvar_5: number | null; sd: number | null; total_net: number | null;
  t_stat: number | null; ci_low: number | null; ci_high: number | null; costs: number | null;
  return_on_margin_annual: number | null; trades_without_margin: number;
  days: number; cagr: number | null; max_drawdown_pct: number | null; max_drawdown?: number | null;
  sharpe: number | null; sortino: number | null; daily_sr: number | null; deflated_sharpe?: number | null;
};

export type Criterion = { criterion: string; value: number | null; pass: boolean; rule: string };

export type Status = "candidate" | "fails" | "overlay_reported" | "in_sample_only" | "no_data" | "in_sample_result"; // in_sample_result: a Strategy Lab pass (#361)

export type StrategyResult = {
  key: string; name: string; role: "candidate" | "control" | "overlay" | "lab"; kind: string; underlying: string | null;
  spec: Record<string, unknown>; spec_hash: string; status: Status; reasons: string[];
  windows?: { is: WindowStats; oos?: WindowStats; oos_stress?: WindowStats };
  criteria?: Criterion[]; equity?: [string, number][]; skips?: Record<string, number>; notes?: Record<string, number>;
  by_year?: Record<string, WindowStats>; by_iv_regime?: Record<string, WindowStats>;
  overlay?: { periods: number; stock_total: number; with_overlay_total: number; stock_worst: number;
    with_overlay_worst: number; stock_cvar_5: number | null; with_overlay_cvar_5: number | null; note: string } | null;
  open_at_end?: { entry_day: string; expiry: string; marked_gross: number }[]; n_trades?: number;
  criteria_v2?: CriteriaV2; opportunity?: Opportunity;
};

/** #381: the strategy's out-of-sample net P&L after the owner's tax beside holding NIFTY on the same capital and days. */
export type Opportunity = {
  capital: number; fno_net: number; trades: number; slab_rate: number | null; fno_after_tax: number | null;
  index_return: number | null; index_tax_rate: number | null; index_after_tax: number | null;
  index_per_trade_after_tax: number | null; excess_after_tax: number | null; reason: string | null;
  benchmark: string; window: [string, string];
};

/** #381: criteria v2, a registered shadow verdict that never changes the status. */
export type CriteriaV2 = {
  status: "shadow_not_adopted"; verdict: "would_pass" | "would_fail"; reasons: string[]; criteria: Criterion[];
  registration_hash: string; note: string;
  dsr: { rule: string; n_trials: number; v_null: number | null; v_cand: number | null; v_used: number | null; dsr: number | null };
  stats: { lo_sharpe?: number | null; posterior_sharpe?: number | null; max_drawdown_peak_pct?: number | null } & Record<string, unknown>;
};

export type SetupLeg = { right: string; strike: number; side: string; ref_price: number; fill_assumed: number };
export type Setup = {
  status: "entry" | "wait" | "no_data" | "unknown"; reason?: string; as_of?: string; symbol?: string; expiry?: string;
  dte?: number; lots?: number; lot?: number; legs?: SetupLeg[]; premium?: number; max_loss?: number | null;
  margin_approx?: number | null; entry_costs?: number; iv_percentile?: number | null; note?: string;
};

export type BacktestRun = {
  id: number; created_at: string | null; version: string; registration_hash: string; registered_hash: string;
  oos_evaluated: boolean; source: string; variants_tried: number; stock_rule_symbol: string | null;
  windows: { is: [string, string]; oos: [string, string] | null }; generated_at: string;
  book: Record<string, unknown>; criteria: Record<string, unknown>; deflated_sharpe_source: string;
  strategies: StrategyResult[]; note: string;
  candidates: { key: string; name: string; oos: WindowStats | null; criteria: Criterion[] | null; setup: Setup | null }[];
};

export type Registration = {
  version: string; hash: string; registered_on: string; matches_spec: boolean; matches_code?: boolean; hash_covers: string; note: string; variants_tried: number;
  specs: Record<string, unknown>[]; book: Record<string, unknown>; criteria: Record<string, unknown>;
  windows: { is: [string, string]; oos_from: string }; doc: string; deflated_sharpe_source: string;
};

export type BacktestsResponse = { registration: Registration; run: BacktestRun | null };

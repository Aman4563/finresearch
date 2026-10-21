// Shapes of /api/fno/lab (#318): user strategies with their locked split, versions, background runs, the variants
// count, the library comparison and paper rule sets.

import type { Criterion, StrategyResult, WindowStats } from "./backtest-types";

export type Structure = "put_credit" | "call_credit" | "iron_condor" | "debit_call" | "debit_put";
export type Trend = "none" | "above_ma50" | "above_ma200" | "below_ma50" | "below_ma200";

export type LabSpec = {
  underlying: string; structure: Structure; expiry: "monthly" | "any"; dte_min: number; dte_max: number;
  weekdays: number[]; iv_measure: "percentile" | "rank"; iv_min: number | null; iv_max: number | null; trend: Trend;
  strike_by: "delta" | "otm_pct"; delta: number | null; otm_pct: number | null; width_pct: number;
  max_loss_pct: number; capital: number; max_lots: number; max_concurrent: number; max_per_expiry: number;
  min_days_between: number; profit_take_pct: number | null; stop_mult: number | null; exit_dte: number | null;
  exit_before_expiry_day: boolean; exit_before_results: boolean;
};

export type Brief = Pick<WindowStats, "trades" | "expectancy" | "win_rate" | "sharpe" | "max_drawdown_pct" | "deflated_sharpe" | "total_net">;

export type RunStatus = "queued" | "running" | "done" | "failed" | "timed_out";

export type LabRunRow = {
  id: number; version_id: number; oos: boolean; status: RunStatus; progress: number; progress_note: string | null;
  created_at: string | null; started_at: string | null; finished_at: string | null; error: string | null;
  n_trials: number | null; counted: boolean; n_trades: number;
  summary?: { status: string | null; is: Brief | null; oos: Brief | null; warnings: number; oos_label?: string | null };
};

export type LabResult = StrategyResult & {
  oos: boolean; warnings: string[]; n_trials: number; sr_trials_variance: number | null;
  windows_dates: { is: [string, string]; oos: [string, string] | null }; book: { capital: number } & Record<string, unknown>;
  criteria?: Criterion[];
  // #361: the lab's "out-of-sample" window is already published; a pass reads "in_sample_result", never "candidate"
  oos_label?: string; oos_note?: string;
};

export type LibraryRow = { key: string; name: string; underlying: string | null; status: string; is: Brief | null; oos: Brief | null };

export type LabRunView = LabRunRow & {
  result: LabResult | null; strategy: { id: number; name: string; version: number; underlying: string };
  library?: { run_id: number; oos_evaluated: boolean; variants_tried: number; strategies: LibraryRow[] } | null;
  bar_now?: { n_trials: number; deflated_sharpe: number | null; note: string };
};

export type LabVersion = {
  id: number; version: number; created_at: string | null; spec: LabSpec; spec_hash: string; note: string; describe: string;
  counted_variant: boolean; runs: LabRunRow[]; paper_rules: { id: number; status: string }[];
};

export type LabStrategy = {
  id: number; name: string; underlying: string; created_at: string | null; oos_versions: number;
  split: { is: [string, string]; oos_from: string; data: [string, string]; rule: string; oos_label?: string }; versions: LabVersion[];
};

export type LabResponse = {
  strategies: LabStrategy[]; underlyings: string[]; variants: { library: number; lab: number; n: number };
  blocks: { structures: Record<Structure, string>; trends: Record<Trend, string>; weekdays: string[]; max_dte: number;
    run_cap_minutes: number; min_span_days: number; results_note: string; deflated_sharpe_source: string };
};

export type PaperSignal = {
  id: number; day: string; status: "pending" | "filled" | "missed" | "void"; expiry: string; lots: number; lot: number;
  legs: { right: string; strike: number; side: string; ref: number; fill_assumed: number }[]; premium_at_signal: number;
  paper_id: number | null; paper_status: string | null; mtm_gross: number | null; net_pnl: number | null;
  exit_reason: string | null; note: string | null;
};

export type PaperRule = {
  id: number; version_id: number; version: number; strategy_id: number; strategy: string; underlying: string;
  status: "active" | "stopped"; created_at: string; start_after: string; last_day: string | null; note: string | null;
  signals: PaperSignal[]; method: string;
  results: { closed: number; wins: number; total_net: number; expectancy: number | null; max_drawdown: number };
};

export type LabTrade = {
  entry_day: string; exit_day: string; expiry: string; dte: number; days_held: number; lots: number; lot: number;
  premium: number; max_loss: number | null; entry_costs: number; exit_costs: number; exercise_stt: number; gross: number;
  net: number; reason: string; iv_pct: number | null; legs: { right: string; strike: number; side: string; entry: number; exit: number | null }[];
};

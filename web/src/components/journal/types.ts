// Types for the decision journal for every trade, the pre-trade checklist and the behaviour report (feature #5).
// Kept here (not in lib/api.ts) so this feature's changes stay in its own files.

export type NoteStatus = "draft" | "planned" | "active" | "reviewed" | "cancelled";
export type Verdict = "right" | "wrong" | "mixed" | "too_early";

export type TradeNote = {
  id: number;
  status: NoteStatus;
  source: "auto" | "manual" | "pretrade";
  side: "buy" | "sell";
  asset_type: string;
  instrument: string | null;
  name: string;
  holding_id: number | null;
  txn_ids: number[];
  trade_day: string | null;
  quantity: number | null;
  price: number | null;
  thesis: string | null;
  expected_holding_days: number | null;
  invalidation: string | null;
  confidence_pct: number | null;
  review_on: string | null;
  checklist: Partial<Checklist>;
  outcome_verdict: Verdict | null;
  outcome_notes: string | null;
  outcome: Record<string, unknown>;
  reviewed_at: string | null;
};

export type NotesResponse = { notes: TradeNote[]; due: number[]; synced: { drafts: number; matched: number }; today: string; privacy: string };

export type CheckStatus = "ok" | "warn" | "block" | "info" | "unknown";
export type CheckItem = { key: string; label: string; status: CheckStatus; value: unknown; detail: string; source: string } & Record<string, unknown>;
export type Checklist = {
  side: "buy" | "sell"; instrument: string; name: string; holding_id: number | null; asset_type: string;
  quantity: number; price: number; value: number | null; day: string; status: "ok" | "warn" | "block"; items: CheckItem[]; note: string;
};

export type HoldingLite = { id: number; name: string; account: string; asset_type: string; units: number | null; price: number | null; closed: boolean; nse_symbol?: string | null };

type Side = { n: number; median_days: number | null; mean_days: number | null };
export type Disposition = {
  realised_gains: number; paper_gains: number; realised_losses: number; paper_losses: number;
  pgr: number | null; plr: number | null; difference: number | null; se: number | null; t: number | null; ratio: number | null;
  sale_days: number; single_stock_days: number; unpriced: number; unknown_cost: number; reading: string; source: string;
  scope: string; method: string; portfolio?: string;
  days: { day: string; realised_gains: string[]; realised_losses: string[]; paper_gains: string[]; paper_losses: string[]; neither: string[]; not_counted: string[] }[];
};
export type Behaviour = {
  from: string; to: string; trades: number; trades_by_side: { buy: number; sell: number };
  disposition: Disposition; disposition_funds: Disposition;
  after_sale: null | { horizon_trading_days: number; winners_sold: { n: number; mean_excess: number | null }; losers_kept: { n: number; mean_excess: number | null }; difference: number | null; excess_over: string; source: string };
  turnover: { months: { month: string; value: number | null; sales: number | null; purchases: number | null; turnover: number | null; unpriced: string[] }[]; mean_monthly: number | null; annual: number | null; annual_sales: number | null; n_months: number; source: string; note: string };
  holding_periods: { realised: { winners: Side; losers: Side; skipped: number; reading: string | null }; open: { in_profit: Side; in_loss: Side; unknown: number }; weighting: string };
  frequency_vs_returns: { fy: number; label: string; from: string; to: string; trades: number; turnover_annual: number | null; twr: number | null; benchmark: number | null; excess: number | null }[];
  churn: { charges: number; tax: number; tax_short_term: number; total: number; drag_pct_a_year: number | null; near_long_term: { name: string; sold: string; long_term_from: string; days_short: number; gain: number }[]; how: string };
  warnings: string[]; privacy: string; disclaimer: string;
};

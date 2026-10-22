// GET /api/fno/review (#383): the monthly post-trade review. Every figure may be null: unknown, never 0.
export type ReviewShortfall = {
  paper_id: number; card_log_id: number | null; price: number | null; fees: number | null; total: number | null;
  base: number | null; pct: number | null; why: string | null;
};

export type ReviewDay = {
  position: string; day: string; previous: string; actual: number | null; max_loss: number | null;
  within: boolean | null; why: string | null;
  greeks: { delta: number; gamma: number; vega: number; theta: number; explained: number; residual: number; unexplained: number | null } | null;
  revaluation: { spot: number; vol: number; time: number; model: number; residual: number } | null;
};

export type FnoReview = {
  month: string; start: string; end: string;
  adherence: {
    closed: number; counts: Record<string, number>; rate: number | null; with_stop: number;
    beyond_stop_count: number; mean_beyond_stop: number | null; window_hours: number;
  };
  shortfall: { trades: ReviewShortfall[]; known: number; mean_total: number | null; mean_pct: number | null };
  mix: { trades: number; premium_sold_share: number | null; same_day_expiry_share: number | null; mean_hold_share: number | null };
  explain: {
    days: ReviewDay[];
    summary: { position_days: number; unknown: number; decided: number; within: number; share_within: number | null; target_share: number; meets_target: boolean | null };
  };
  deferred: { closed_trades: number; starts_at: number; status: "deferred" | "due" };
  notes: string[];
};

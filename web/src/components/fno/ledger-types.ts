// GET /api/fno/card-ledger (#376): the card ledger's registered scoring report, the override log and the fixed
// probability panel. Every statistic may be null: unknown (undefined or too few events), never 0.
export type LedgerBin = { n: number; p_low: number; p_high: number; mean_p: number; observed: number; ci: [number, number] | null };

export type LedgerModel = {
  model: string; label: string; n: number; brier: number | null; log: number | null; mean_p: number | null;
  base_rate: number | null; reliability: LedgerBin[];
  pit: { n: number; bins: number[]; clipped: number; no_dots: number };
  p10: { n: number; below: number; share: number | null; expected: number; p: number | null };
};

export type LedgerSection = {
  events: number; independent: number;
  bss: { n: number; bss: number | null; lo: number | null; hi: number | null; se: number | null; block: number | null; reps: number };
  cohort: {
    delta: number | null; se: number | null; t: number | null; p: number | null; ci: [number, number] | null; clusters: number;
    n_pass: number; n_fail: number; unknown_class: number; mean_pass: number | null; mean_fail: number | null;
  };
  models: LedgerModel[];
  claims: { underlying: string; n_independent: number; claim_allowed: boolean }[];
};

export type OverrideRow = {
  id: number; created_at: string | null; card_log_id: number; card_index: number; decision: "traded" | "skipped";
  verdict: string | null; override: boolean | null; note: string | null;
};

export type OverrideGroup = {
  decisions: number; resolved: number; mean_pnl: number | null; open: number; void: number; no_forecast: number; clusters: number;
};

export type PanelGroup = {
  underlying: string; expiry_kind: string; event: string; model: string; n: number; brier: number | null; log: number | null;
  mean_p: number | null; base_rate: number | null; claim_allowed: boolean; events: number; open: number; void: number; unknown_p: number;
};

export type CardLedger = {
  prereg: string; addendum: string; claim_min: number; registered: string;
  cards: Record<string, number>;
  before: LedgerSection; after: LedgerSection; pooled: LedgerSection;
  overrides: {
    decisions: number; overrides: number; unknown_class: number; rate: number | null; rows: number;
    pnl: Record<"traded_against" | "traded_with" | "skipped_enter" | "skipped_other", OverrideGroup>;
    recent: OverrideRow[];
  };
  panel: {
    start: string; rows: number; sessions: number; first_session: string | null; last_session: string | null; claim_min: number;
    groups: PanelGroup[];
    pit: { underlying: string; expiry_kind: string; model: string; bins: number[]; n: number; unknown: number }[];
  };
};

// Types for the joint risk view (#397): GET /api/portfolio/analytics/joint-risk and its pre-trade POST.

export type Delta = {
  holdings: number | null; fno: number | null; total: number | null;
  per_pct: { holdings: number | null; fno: number | null; total: number | null };
  complete: boolean; reasons: string[];
};
export type Loss = {
  move: number; holdings: number | null; fno: number | null; total: number | null;
  fno_share: number | null; share_reason: string | null; reasons: string[];
};
export type JointReplay = {
  day: string; label: string; nifty_move: number; iv30_change: number;
  holdings: number | null; fno: number | null; total: number | null; reasons: string[]; notes: string[];
};
export type JointRisk = {
  as_of: string; assume_beta1: boolean; disclaimer: string; privacy: string; method: string; beta_note: string; notes: string[];
  holdings: { key: string; name: string; value: number; beta: number | null; how: string }[]; holdings_reason: string | null;
  book: { known: boolean; legs: number | null; reason: string | null };
  d_total: Delta; losses: Loss[];
  hedge_trigger: { share: number | null; fired: boolean | null; threshold: number; reasons: string[]; note: string };
  replays: JointReplay[];
  forward_test: { qualifying_days: number; needed: number; status: string; since: string };
};
export type Pretrade = {
  as_of: string; notes: string[]; move: number;
  d_before: Delta; d_after: Delta; l_before: Loss; l_after: Loss;
  d_change: number | null; l_change: number | null; trade_delta: number | null; trade_l: number | null; trade_reasons: string[];
};

// GET /api/fno/scenarios (#394): the open F&O book's exposures, spot × vol × time grid, replays, reverse stress,
// historical scan with its ES97.5, multi-week episodes, gap and expiry-day scenarios and the liquidity term.
// Every figure may be null: unknown (with the reasons), never 0.

import type { RateInfo } from "./model";

export type Exposure = { rs_delta: number; rs_gamma_1pct: number; vega_pt: number; theta_day: number };

export type ScenarioCell = {
  move: number; dvol: number; horizon: string; at: string; pnl: number | null;
  floored: number; intrinsic: number; ends: Record<string, "lo" | "hi">;
};

export type BookBeta = {
  underlying: string; beta: number | null; se: number | null; lo: number | null; hi: number | null; n: number | null;
  missing: number | null; window_start: string | null; as_of: string | null; half_life: number | null; z: number;
  floor: number; reason: string | null;
};

export type ScanDay = { start: string; end: string; move: number; dvol: number; pnl: number; mapped: string[] };

export type Scan = {
  h: number; n: number; skipped: number; first: string | null; last: string | null; loss_99: number | null;
  es: number | null; worst: ScanDay[]; reasons: string[]; es_reasons: string[]; mapped: Record<string, number>; flags: string[];
};

export type PathPoint = { day: string; d: number; move: number; dvol: number; pnl: number; intrinsic: number; mapped: string[] };

export type EpisodeView = {
  key: string; label: string; window: [string, string]; source: string; start: string | null;
  path: PathPoint[]; worst: PathPoint | null; candidates: number; reasons: string[];
};

export type ShockView = { kind: string; label: string; move: number | null; at: string; pnl: number | null; n: number; reasons: string[] };

/** #480: one expiry's forward, the carry its scenarios roll it at (b = ln(F/S)/T, a year; null = unknown, with
 * `carry_reason`: then its scenarios are unknown, never rolled at the rate), the dividend yield r - b it implies, and
 * its own discount rate (#366/#385: FBIL's par yield at the expiry's tenor, or a labelled fallback). */
export type ForwardView = {
  underlying: string; expiry: string; forward: number | null; source: string; spot: number | null; t_years: number | null;
  carry: number | null; carry_source: string | null; carry_reason: string | null; implied_q: number | null;
  rate: number; rate_info: RateInfo;
};

export type BookScenarios = {
  as_of: string; notes: string[];
  legs: { underlying: string; expiry: string; right: string; strike: number; qty: number; mark: number | null;
    mark_kind: string | null; iv: number | null; forward: number | null; source: string; reason: string | null }[];
  betas: BookBeta[]; beta_rule: string;
  forwards: ForwardView[];
  exposures: {
    complete: boolean; reasons: string[]; total: Exposure | null; flags: string[]; nifty_delta: number | null;
    nifty_delta_reasons: string[]; rows: { underlying: string; bucket: string; legs: number; exposure: Exposure | null }[];
  };
  grid: {
    complete: boolean; reasons: string[]; moves: number[]; vol_shifts: number[]; cells: ScenarioCell[];
    vol_response: { down_per_pct: number; up_per_pct: number };
  };
  replays: { day: string; label: string; move: number; iv30_change: number; source: string; pnl: number | null; reasons: string[]; notes: string[] }[];
  reverse: { limit: number | null; reason: string | null; rows: { horizon: string; complete: boolean; reasons: string[]; cell: ScenarioCell | null }[] };
  history: {
    stored: [string, string] | null; scan: Scan[];
    es: { value: number | null; h: number; n: number; limit: number | null; ratio: number | null; reasons: string[]; note: string };
    episodes: EpisodeView[]; episode_h: number; gaps: ShockView[]; expiry_day: ShockView[]; scenario_note: string; skew_note: string;
  };
  liquidity: { stressed: number | null; reason: string; quoted_exit_cost: number | null; reasons: string[]; note: string };
};

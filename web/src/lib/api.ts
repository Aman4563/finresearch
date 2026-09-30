// Typed client for the FinResearch local API (finresearch serve, always on 127.0.0.1).
"use client";

import { useCallback, useEffect, useState } from "react";

export const API_URL = process.env.NEXT_PUBLIC_FINRESEARCH_API ?? "http://127.0.0.1:8710";

export type Citation = {
  document_id: number | null;
  document_title: string | null;
  page: number | null;
  line_start: number | null;
  line_end: number | null;
  quote: string | null;
  quote_found: boolean | null;
  url: string | null;
  accessed_at: string | null;
};

export type Claim = {
  id: number;
  run_id: number;
  stream: string;
  statement: string;
  claim_type: string;
  metric: string | null;
  value: string | null;
  unit: string | null;
  period: string | null;
  importance: string;
  status: string;
  verifier_note: string | null;
  checks?: { source_language?: string; translation_marked?: boolean } & Record<string, unknown>;
  corrects_claim_id: number | null;
  citations: Citation[];
};

export type Step = {
  id: number;
  key: string;
  stage: string;
  role: string | null;
  status: string;
  attempts: number;
  tier: string | null;
  model: string | null;
  error: string | null;
  num_turns: number | null;
  duration_s: number | null;
  five_hour_before: number | null;
  five_hour_after: number | null;
  started_at: string | null;
  finished_at: string | null;
};

export type Worker = { pid: number; alive: boolean; log: string; started_at: string } | null;

export type RunSummary = {
  id: number;
  kind: string;
  status: string;
  company: string | null;
  company_name: string | null;
  /** the listed stock's key (NSE symbol or "BSE:<code>") and exchange, when the company is a listed stock */
  key?: string | null;
  exchange?: "NSE" | "BSE" | null;
  created_at: string | null;
  finished_at: string | null;
  resume_after: string | null;
  final_gate: { ok: boolean; blocking: string[] } | null;
  worker: Worker;
  /** why the run failed (set when a step or the pipeline failed; cleared when it is resumed) */
  last_error?: { message: string; type?: string; at?: string } | null;
  /** why the run is paused, and whether for the plan budget/limit or a transient Claude CLI failure */
  pause_reason?: string | null;
  pause_kind?: "budget" | "limit" | "transient" | null;
  /** marked running but its worker has exited: nothing is working on it until it is resumed */
  stalled?: { reason: string; since: string | null } | null;
  steps: Record<string, number>;
  has_report?: boolean;
};

export type RunDetail = Omit<RunSummary, "steps"> & {
  steps: Step[];
  claims: Record<string, number>;
  usage: { five_hour_used: number; turns: number; minutes: number };
};

export type Report = {
  run_id: number;
  kind: string;
  markdown: string;
  published: boolean;
  gate: { ok: boolean; blocking: string[]; warnings: string[] };
  claims: Record<string, Claim>;
};

export type Company = {
  slug: string;
  name: string;
  nse_symbol: string | null;
  /** BSE scrip code (set for BSE-only stocks; also noted for dual-listed ones added by code) */
  bse_code?: string | null;
  isin?: string | null;
  /** where the listed stock is read: NSE, or BSE for a BSE-only stock; null for IPOs without a symbol, funds, bonds */
  exchange?: "NSE" | "BSE" | null;
  /** the stock page / API key: the NSE symbol, or "BSE:<code>" */
  key?: string | null;
  documents: number;
  latest_run: number | null;
  kind: ResearchKind;
};

export type ResearchKind = "ipo_report" | "stock_report" | "fund_report" | "bond_report";

export type Issue = {
  phase: "open" | "upcoming" | "closed" | "current";
  exchange: "NSE" | "BSE";
  symbol: string;
  company: string;
  series: string | null;
  issue_start: string | null;
  issue_end: string | null;
  price_band: string | null;
  times_subscribed: string | null;
  status: string | null;
  slug: string | null;
  latest_run: number | null;
  latest_run_status: string | null;
  bse_ipo_no?: number; // BSE SME rows only
  // lot and application amounts: NSE's issue page first, BSE's issue details as cross-check / fallback
  lot_size?: number | null;
  min_lots?: number | null;
  min_bid_shares?: number | null;
  application?: IssueApplication | null;
  lot_source?: LotSource | null;
  lot_note?: string | null;
  price_band_note?: string | null;
  price_band_list?: string | null;
};

/** Rupee amounts at the upper price band. `null` lots = the category cannot be reached with whole lots. */
export type IssueApplication = {
  price: number;
  lot_cost: number;
  min_investment: number;
  retail_max_lots: number | null;
  retail_max_amount: number | null;
  shni_min_lots: number | null;
  shni_min_amount: number | null;
  bhni_min_lots: number | null;
  bhni_min_amount: number | null;
  retail_cap: number;
  basis: string;
};

export type LotSource = {
  label: string;
  url: string | null;
  as_of: string | null;
  min_lots_basis: string | null;
  check: string | null;
};

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${API_URL}${path}`, {
      ...init,
      // X-FinResearch marks the request as the dashboard's (the API refuses unsafe requests without it)
      headers: { "Content-Type": "application/json", "X-FinResearch": "1", ...(init?.headers ?? {}) },
      cache: "no-store",
    });
  } catch {
    throw new ApiError(0, `FinResearch API not reachable at ${API_URL} — run \`uv run finresearch serve\``);
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = (await res.json()).detail ?? detail;
    } catch {}
    throw new ApiError(res.status, typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return res.json() as Promise<T>;
}

/** Fetch once (and on `reload`), optionally polling every `pollMs`. Polling pauses while the tab is hidden and
 * catches up when it is shown again. `updatedAt` is when the last successful response arrived. */
export function useApi<T>(path: string | null, pollMs?: number) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null);
  const [tick, setTick] = useState(0);
  const reload = useCallback(() => setTick((t) => t + 1), []);

  useEffect(() => {
    if (!path) return;
    let alive = true;
    let last = 0;
    const load = () => {
      last = Date.now();
      return api<T>(path)
        .then((d) => {
          if (alive) {
            setData(d);
            setError(null);
            setUpdatedAt(new Date());
          }
        })
        .catch((e: Error) => alive && setError(e.message));
    };
    load();
    const timer = pollMs ? setInterval(() => !document.hidden && load(), pollMs) : undefined;
    const onShow = () => pollMs && !document.hidden && Date.now() - last >= pollMs && load();
    document.addEventListener("visibilitychange", onShow);
    return () => {
      alive = false;
      if (timer) clearInterval(timer);
      document.removeEventListener("visibilitychange", onShow);
    };
  }, [path, pollMs, tick]);

  return { data, error, reload, updatedAt };
}

/** Tell other components (e.g. the header's alert badge) that alerts changed. */
export const ALERTS_CHANGED = "finresearch:alerts-changed";

export const pct = (x: number | null | undefined) => (x == null ? "" : `${Math.round(x * 100)}%`);
export const when = (iso: string | null | undefined) =>
  iso ? new Date(iso).toLocaleString("en-IN", { dateStyle: "medium", timeStyle: "short" }) : "";
/** A calendar date (YYYY-MM-DD) without inventing a time or shifting it by the viewer's time zone. */
export const day = (iso: string | null | undefined) =>
  iso ? new Date(`${iso.slice(0, 10)}T00:00:00Z`).toLocaleDateString("en-IN", { dateStyle: "medium", timeZone: "UTC" }) : "";

export type Rule = { id: string; description: string; metric: string; op: string; value: string; action: "skip" | "warn" };

// ------------------------------------------------------------------ alert rules for every asset (finresearch.alerts)
export type AlertKind = "ipo" | "stock" | "fund" | "bond" | "fno" | "portfolio";
export type Channel = "ntfy" | "telegram" | "macos";
export type Priority = "min" | "low" | "default" | "high" | "urgent";
export type AlertRule = {
  id: string; kind: AlertKind; metric: string; op: string; value: string;
  /** null = every watch of the kind */
  instrument: string | null;
  params: Record<string, string>; channels: Channel[]; priority: Priority; cooldown_h: string; enabled: boolean;
  description: string;
};
export type MetricSpec = {
  key: string; kind: AlertKind; label: string; phrase: string; unit: string; cadence: "intraday" | "daily";
  description: string; source: string; event: boolean; event_text: string; rebase: string | null; params: string[];
  default_op: string; default_value: string; allows_all: boolean;
};
export type RuleTemplate = { id: string; kind: AlertKind; title: string; why: string; metric: string; op: string; value: string; priority: Priority; params: Record<string, string> };
export type AlertRegistry = { kinds: AlertKind[]; metrics: MetricSpec[]; templates: RuleTemplate[]; ops: Record<string, string> };
export type AlertRuleState = {
  rule_id: string; instrument: string; status: "new" | "clear" | "fired" | "unknown"; value: string | null; source: string | null;
  note: string | null; since: string | null; last_fired_at: string | null; fired_count: number; suppressed_count: number; checked_at: string | null;
};
export type ChannelStatus = { enabled: boolean; status: string };
export type NotificationSettings = {
  ntfy: ChannelStatus & { server: string; topic: string; topic_set: boolean; token: string; token_set: boolean };
  telegram: ChannelStatus & { bot_token: string; bot_token_set: boolean; chat_id: string };
  macos: ChannelStatus & { sound: boolean };
  general: { app_url: string; forward_level: "off" | "action" | "warn"; forward_channels: Channel[] };
  channels: Channel[];
};
export type Delivery = {
  id: number; alert_id: number | null; channel: Channel; priority: Priority; title: string; message: string;
  status: "pending" | "sent" | "failed"; attempts: number; next_try_at: string | null; last_error: string | null; held: string | null;
  test: boolean; created_at: string | null; sent_at: string | null; rule_id?: string | null; alert_kind?: string | null;
};
export type Profile = {
  capital_per_ipo_inr: string;
  risk_appetite: "low" | "medium" | "high";
  horizon: "listing" | "short" | "long";
  tax_slab_pct: string;
  category: "retail" | "shni" | "bhni";
  holdings: { symbol: string; sector: string | null; value_inr: string | null }[];
  rules: Rule[];
  /** notify-only alert rules for every asset kind (optional: older API builds omit them) */
  alert_rules?: AlertRule[];
  notes: string;
  // F&O analysis budget (optional: older API builds omit them)
  fno_capital_inr?: string;
  fno_max_loss_pct?: string;
  fno_brokerage_per_order_inr?: string;
  fno_defined_risk_only?: boolean;
  fno_experience?: "none" | "some" | "experienced";
  // identity and dashboard preferences (optional: older API builds omit them)
  display_name?: string;
  avatar_color?: AvatarColor | null;
  preferences?: Preferences;
};

export type Decision = {
  id: number;
  run_id: number;
  company_name?: string | null;
  action: "APPLY" | "APPLY-CONDITIONAL" | "SKIP";
  lots: number;
  category: string | null;
  suggestion: {
    conditions: string[];
    warnings: string[];
    enforcement_notes: string[];
    model?: string;
    agent: { action: string; lots: number; exit_plan: string; watch: string[]; rationale_markdown: string; confidence: string };
  };
  inputs: {
    at: string;
    limits: Record<string, number | null>;
    metrics: Record<string, { value: string | null; source: string; as_of: string | null }>;
    rules: { rule: Rule; status: "fired" | "clear" | "unknown"; value: string | null; source: string | null }[];
  };
  user_action: "applied" | "skipped" | null;
  applied_lots: number | null;
  allotted_lots: number | null;
  issue_price: string | null;
  listing_price: string | null;
  exit_price: string | null;
  exit_date: string | null;
  outcome: Record<string, string | boolean>;
  notes: string | null;
  created_at: string | null;
};

export type AlertItem = {
  id: number;
  watch_id: number | null;
  kind: string;
  level: "info" | "warn" | "action";
  message: string;
  created_at: string | null;
  read_at: string | null;
  nse_symbol?: string | null;
  exchange?: "NSE" | "BSE" | null;
  /** the stock page key (NSE symbol or "BSE:<code>") and how to show it ("INFY", "BSE 526433") */
  key?: string | null;
  label?: string | null;
  /** rule alerts carry their source rule: rule_id, rule_kind, metric, instrument, value, threshold, source, path … */
  data?: Record<string, unknown>;
};

/** GET /api/monitor/schedule: when each monitor check runs (IST), from the scheduler and the profile's watch windows. */
export type MonitorSchedule = {
  running: boolean | null;
  tick_s: number;
  grace_hours: number;
  ipo: {
    check_times: string[]; final_check: string; bidding_hours: [string, string]; allotment_time: string;
    listing_times: { open: string; close: string }; lockin_time: string; anchor_lockin_days: number[];
    archive_times: string[]; archive_window_min: number; listing_max_attempts: number;
  };
  stock: { daily_time: string; horizon_days: number };
  quiet: { start: string | null; end: string | null };
  intraday: { from: string; indices: string[]; max_tries: number; retry_min: number };
  iv: { from: string; indices: string[] };
  forecasts: { after: string; every_min: number };
  equity_hours: [string, string];
};

export type WatchSummary = {
  id: number;
  kind: "ipo" | "stock";
  company: string | null;
  company_name: string | null;
  /** null for a BSE-only stock's watch (exchange "BSE", keyed by bse_code) */
  nse_symbol: string | null;
  exchange: "NSE" | "BSE";
  bse_code: string | null;
  /** the stock page key: the NSE symbol, or "BSE:<code>" */
  key: string;
  /** how the watch is named in alerts: "INFY", or "BSE 526433" */
  label: string;
  open_date: string | null;
  close_date: string | null;
  allotment_date: string | null;
  listing_date: string | null;
  anchor_shares: string | null;
  active: boolean;
  meta: Record<string, unknown>;
  unread_alerts?: number;
  next_check?: { kind: string; due_at: string } | null;
  last_subscription?: { as_of: string; total_times: string } | null;
};

export type WatchDetail = WatchSummary & {
  jobs: { id: number; kind: string; slot: string; due_at: string; status: string; attempts: number; error: string | null; result: Record<string, unknown> }[];
  subscription: { as_of: string; source: string; total_times: string; categories: { name: string; code: string | null; times: string | null }[] }[];
  alerts: AlertItem[];
};

export type AvatarColor = "brand" | "accent" | "gain" | "loss" | "warn" | "info";
export type Preferences = {
  default_landing: "/" | "/ipos" | "/stocks" | "/funds" | "/bonds" | "/fno" | "/runs" | "/monitor" | "/journal";
  number_format: "lakh_crore" | "million";
  compact_tables: boolean;
  reduce_motion: boolean;
  // chart defaults, refresh cadences and the monitor's watch windows (optional: older API builds omit them)
  time_frames?: import("@/lib/timeframes").TimeFrames;
  watch?: import("@/lib/timeframes").WatchWindows;
};
export type ProfileStats = {
  runs: number;
  runs_done: number;
  decisions: number;
  applied: number;
  watches: number;
  active_watches: number;
  first_run_at: string | null;
  profile_updated_at: string | null;
};

// ------------------------------------------------------------------ forecast ledger (finresearch.signals.ledger)
export type Forecast = {
  id: number;
  created_at: string | null;
  asset: "ipo" | "stock" | "fund" | "bond" | "fno";
  instrument: string;
  name: string | null;
  source: string;
  run_id: number | null;
  event_kind: string;
  event: string;
  horizon: string;
  resolve_on: string;
  probability: number | null;
  interval: [number, number] | null;
  action: string;
  score: number | null;
  method: string;
  validation_status: string;
  status: "open" | "resolved" | "void";
  outcome: 0 | 1 | null;
  resolved_at: string | null;
  resolution_value: number | null;
  resolution_note: string | null;
  last_checked_at: string | null;
  inputs: Record<string, unknown>;
};
export type ForecastPage = { total: number; limit: number; offset: number; items: Forecast[] };
export type ReliabilityBin = { n: number; p_low: number; p_high: number; mean_p: number; observed: number; ci: [number, number] | null };
export type CalibrationGroup = {
  asset: string; method: string; validation_status: string;
  total: number; open: number; resolved: number; void: number; no_call: number;
  n: number; base_rate: number | null; mean_p: number | null; brier: number | null; brier_reference: number | null;
  brier_skill: number | null; log_loss: number | null; hits: number; calls: number; hit_rate: number | null;
  hit_rate_ci: [number, number] | null; bins: ReliabilityBin[];
};
export type Calibration = {
  groups: CalibrationGroup[];
  confidence_map: Record<string, number>;
  next_open: Forecast | null;
  next_scored: Forecast | null;
  min_n_for_recalibration: number;
};

/** "NSE INFY", "BSE 526433" (a BSE-only stock) or "BSE SME XYZ" (a BSE SME IPO): the exchange is always named. */
export function watchExchangeLabel(w: Pick<WatchSummary, "exchange" | "bse_code" | "nse_symbol" | "meta" | "kind">): string {
  if (w.exchange === "BSE" && w.bse_code) return `BSE ${w.bse_code}`;
  if (w.kind === "ipo" && w.meta?.bse_ipo_no) return `BSE SME ${w.nse_symbol ?? ""}`.trim();
  return `NSE ${w.nse_symbol ?? ""}`.trim();
}

/** A listed stock's key as shown next to its name: "NSE INFY", or "BSE 526433" for a BSE-only stock. */
export function listingLabel(key: string | null | undefined): string | null {
  if (!key) return null;
  return key.toUpperCase().startsWith("BSE:") ? `BSE ${key.slice(4)}` : `NSE ${key}`;
}

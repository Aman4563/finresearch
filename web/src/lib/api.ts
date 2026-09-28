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
  created_at: string | null;
  finished_at: string | null;
  resume_after: string | null;
  final_gate: { ok: boolean; blocking: string[] } | null;
  worker: Worker;
  steps: Record<string, number>;
};

export type RunDetail = Omit<RunSummary, "steps"> & {
  steps: Step[];
  claims: Record<string, number>;
  usage: { five_hour_used: number; turns: number; minutes: number };
};

export type Report = {
  run_id: number;
  markdown: string;
  published: boolean;
  gate: { ok: boolean; blocking: string[]; warnings: string[] };
  claims: Record<string, Claim>;
};

export type Company = {
  slug: string;
  name: string;
  nse_symbol: string | null;
  documents: number;
  latest_run: number | null;
};

export type Issue = {
  phase: "open" | "upcoming" | "closed" | "current";
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
      headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
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

/** Fetch once (and on `reload`), optionally polling every `pollMs`. */
export function useApi<T>(path: string | null, pollMs?: number) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  const reload = useCallback(() => setTick((t) => t + 1), []);

  useEffect(() => {
    if (!path) return;
    let alive = true;
    const load = () =>
      api<T>(path)
        .then((d) => {
          if (alive) {
            setData(d);
            setError(null);
          }
        })
        .catch((e: Error) => alive && setError(e.message));
    load();
    const timer = pollMs ? setInterval(load, pollMs) : undefined;
    return () => {
      alive = false;
      if (timer) clearInterval(timer);
    };
  }, [path, pollMs, tick]);

  return { data, error, reload };
}

export const pct = (x: number | null | undefined) => (x == null ? "" : `${Math.round(x * 100)}%`);
export const when = (iso: string | null | undefined) =>
  iso ? new Date(iso).toLocaleString("en-IN", { dateStyle: "medium", timeStyle: "short" }) : "";

export type Rule = { id: string; description: string; metric: string; op: string; value: string; action: "skip" | "warn" };
export type Profile = {
  capital_per_ipo_inr: string;
  risk_appetite: "low" | "medium" | "high";
  horizon: "listing" | "short" | "long";
  tax_slab_pct: string;
  category: "retail" | "shni" | "bhni";
  holdings: { symbol: string; sector: string | null; value_inr: string | null }[];
  rules: Rule[];
  notes: string;
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
};

export type WatchSummary = {
  id: number;
  company: string | null;
  company_name: string | null;
  nse_symbol: string;
  open_date: string;
  close_date: string;
  allotment_date: string;
  listing_date: string;
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

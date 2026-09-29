"use client";

// Shared vocabulary for research runs: kind icons/labels, pipeline stage explanations and duration formatting.

import { ChartCandlestick, FlaskConical, Landmark, type LucideIcon, PieChart, Rocket } from "lucide-react";

import { cx } from "@/components/ui";
import type { Step } from "@/lib/api";

export const KIND: Record<string, { label: string; icon: LucideIcon; href: string }> = {
  ipo_report: { label: "IPO", icon: Rocket, href: "/ipos" },
  stock_report: { label: "Stock", icon: ChartCandlestick, href: "/stocks" },
  fund_report: { label: "Mutual fund", icon: PieChart, href: "/funds" },
  bond_report: { label: "Bond", icon: Landmark, href: "/bonds" },
};

export const kindMeta = (kind: string) => KIND[kind] ?? { label: kind.replace(/_report$/, ""), icon: FlaskConical, href: "/runs" };

export function KindIcon({ kind, className }: { kind: string; className?: string }) {
  const Icon = kindMeta(kind).icon;
  return (
    <span className={cx("grid size-9 shrink-0 place-items-center rounded-lg bg-brand-soft text-brand ring-1 ring-inset ring-brand/20", className)}>
      <Icon className="size-4" />
    </span>
  );
}

/** Pipeline stages in the order the orchestrator runs them, with a plain-English explanation of each. */
export const STAGES: { key: string; label: string; help: string }[] = [
  { key: "facts", label: "Facts", help: "Collects hard facts (issue details, prices, NAVs) from official sources before any agent starts." },
  { key: "plan", label: "Plan", help: "A planner agent reads the documents and decides what each research stream must find out." },
  { key: "stream", label: "Research", help: "Researcher agents work in parallel, one topic each (financials, valuation, news…). Every figure they find is saved as a claim with its source." },
  { key: "verify", label: "Verify", help: "An independent verifier re-reads each claim's source and marks it verified, needs review or contradicted. 'verify2' is a second pass on what the first one flagged." },
  { key: "case", label: "Bull vs bear", help: "Two agents argue the strongest case for and against, using only the verified claims." },
  { key: "synthesis", label: "Write", help: "The synthesizer writes the report; every figure must cite a claim like [C123]. 'fix' steps repair problems the publish gate found." },
  { key: "critic", label: "Critic", help: "A critic reviews the draft and can send it back for another round (r1, r2…) of research and rewriting." },
];

export const stageMeta = (key: string) => STAGES.find((s) => s.key === key) ?? { key, label: key, help: "A pipeline step." };

/** "r1:verify2:bond_terms" -> { round: "r1", label: "bond terms", pass: "verify2" } */
export function stepLabel(s: Pick<Step, "key" | "stage">) {
  const parts = s.key.split(":");
  const round = parts.find((p) => /^r\d+$/.test(p)) ?? null;
  const fix = parts.find((p) => /^fix\d+$/.test(p)) ?? null;
  const rest = parts.filter((p) => p !== round && p !== fix);
  const pass = rest.length > 1 ? rest[0] : null;
  const name = (rest.length > 1 ? rest.slice(1).join(" ") : rest[0] ?? s.key).replaceAll("_", " ");
  return { round, fix, pass: pass && pass !== s.stage ? pass : null, name };
}

export function fmtDuration(seconds: number | null | undefined) {
  if (seconds == null || !Number.isFinite(seconds)) return "—";
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${String(s % 60).padStart(2, "0")}s`;
  return `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, "0")}m`;
}

export function runSeconds(created: string | null, finished: string | null, now = Date.now()) {
  if (!created) return null;
  const a = new Date(created).getTime();
  const b = finished ? new Date(finished).getTime() : now;
  return Number.isFinite(a) && Number.isFinite(b) ? (b - a) / 1000 : null;
}

export const shortModel = (m: string | null) => (m ? m.replace(/^claude-/, "") : null);

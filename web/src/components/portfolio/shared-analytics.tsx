"use client";

// Shared pieces of the portfolio analytics tabs (Performance, Risk, Concentration, Costs): metric shapes from
// /api/portfolio/analytics/*, the "not enough data" state and the notes/disclaimer footer. Types stay in these files.

import { Info, TriangleAlert } from "lucide-react";
import type { ReactNode } from "react";

import { Callout, EmptyState, InfoTip, cx } from "@/components/ui";

export type Metric = { value: number | null; unit: string; reason: string | null; n: number | null; note: string | null; how: string | null; inr?: number | null };

export type AnalyticsBase = {
  disclaimer: string; privacy: string; excluded: string[]; warnings: string[]; notes: string[];
  start_reason: string; reason: string | null; as_of: string | null; start: string | null; available: boolean;
};

export const pct = (v: number | null | undefined, d = 1) => (v == null ? "—" : `${(v * 100).toFixed(d)}%`);
export const spct = (v: number | null | undefined, d = 1) => (v == null ? "—" : `${v > 0 ? "+" : ""}${(v * 100).toFixed(d)}%`);

/** A metric tile body: the value, or the reason it is missing (never a blank). */
export function MetricValue({ m, format, tone }: { m: Metric | undefined; format: (v: number) => string; tone?: "gain" | "loss" }) {
  if (!m) return <span className="text-sm text-muted">—</span>;
  if (m.value == null) return <span className="block text-xs font-normal leading-snug text-muted">{m.reason ?? "not available"}</span>;
  return <span className={cx("num", tone === "gain" && "text-gain", tone === "loss" && "text-loss")}>{format(m.value)}</span>;
}

export function MetricRow({ label, help, m, format, extra }: { label: string; help: ReactNode; m: Metric | undefined; format: (v: number) => string; extra?: ReactNode }) {
  return (
    <div className="flex items-start justify-between gap-3 border-b border-border py-2.5 last:border-0">
      <div className="min-w-0">
        <p className="flex items-center gap-1 text-sm">{label}<InfoTip>{help}{m?.how ? <><br /><span className="text-muted">Method: {m.how}.</span></> : null}</InfoTip></p>
        {m?.note && <p className="text-[11px] text-muted">{m.note}</p>}
      </div>
      <div className="max-w-[55%] text-right text-sm font-medium">
        <MetricValue m={m} format={format} />
        {extra}
        {m?.value != null && m.n != null && <p className="text-[10px] font-normal text-muted">n = {m.n}</p>}
      </div>
    </div>
  );
}

export function NotEnough({ data, what }: { data: AnalyticsBase; what: string }) {
  return (
    <EmptyState icon={<Info className="size-5" />} title={`No ${what} yet`}>
      {data.reason ?? "Not enough data."}
      {data.excluded.length > 0 && <span className="mt-2 block text-xs">Left out: {data.excluded.join("; ")}</span>}
    </EmptyState>
  );
}

/** Warnings, exclusions, method notes and the personal, non-advisory disclaimer: under every analytics tab. */
export function AnalyticsFooter({ data, extra }: { data: AnalyticsBase; extra?: ReactNode }) {
  return (
    <div className="space-y-3">
      {(data.warnings.length > 0 || data.excluded.length > 0) && (
        <Callout tone="warn" icon={<TriangleAlert className="size-4" />} title="Check the data">
          <ul className="list-disc space-y-0.5 pl-4">
            {data.excluded.map((x) => <li key={x}>Left out: {x}</li>)}
            {data.warnings.map((x) => <li key={x}>{x}</li>)}
          </ul>
        </Callout>
      )}
      <div className="space-y-1 text-[11px] text-muted">
        {data.start && <p>Series {data.start} to {data.as_of} · {data.start_reason}.</p>}
        {data.notes.map((n) => <p key={n}>{n}</p>)}
        {extra}
        <p className="flex items-start gap-1.5"><Info className="mt-0.5 size-3 shrink-0" />{data.privacy} {data.disclaimer}</p>
      </div>
    </div>
  );
}

export function Loading({ what }: { what: string }) {
  return (
    <div className="rounded-xl border border-border bg-card p-5 shadow-card">
      <div className="flex items-center gap-2 text-sm text-muted"><span className="size-1.5 rounded-full bg-info animate-pulse-ring" />Building {what} from your transactions and daily prices…</div>
      <p className="mt-1 text-xs text-muted">The first build reads each holding&apos;s price history from NSE, BSE or AMFI (politely, a few requests a second) and caches it on this machine; later visits are quick.</p>
      <div className="mt-4 space-y-2">{[0, 1, 2, 3].map((i) => <div key={i} className="skeleton h-4 rounded" style={{ width: `${90 - i * 12}%` }} />)}</div>
    </div>
  );
}

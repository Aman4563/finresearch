"use client";

// Building blocks shared by the Stocks, Mutual funds and Bonds pages (built on ui.tsx / charts.tsx tokens).

import { Loader2, Search } from "lucide-react";
import Link from "next/link";
import { type ReactNode, useMemo } from "react";

import { type Series, TimeSeriesChart, shortDate } from "@/components/charts";
import { InfoTip, Segmented, cx, inputClass } from "@/components/ui";

/** A fraction as a percentage: 0.1234 -> "12.34%". */
export const pctOf = (x: number | null | undefined, digits = 2) => (x == null || Number.isNaN(x) ? "—" : `${(x * 100).toFixed(digits)}%`);
/** A signed fraction as a percentage: 0.05 -> "+5.00%". */
export const signedPct = (x: number | null | undefined, digits = 2) =>
  x == null || Number.isNaN(x) ? "—" : `${x > 0 ? "+" : ""}${(x * 100).toFixed(digits)}%`;
export const inr = (x: number | string | null | undefined, digits = 2) => {
  const n = typeof x === "string" ? Number(x) : x;
  return n == null || Number.isNaN(n) ? "—" : `₹${n.toLocaleString("en-IN", { minimumFractionDigits: digits, maximumFractionDigits: digits })}`;
};
/** Rupees in crore with Indian grouping: 3,89,940 Cr style for large values. */
export const crore = (x: number | string | null | undefined, digits = 0) => {
  const n = typeof x === "string" ? Number(x) : x;
  return n == null || Number.isNaN(n) ? "—" : `₹${(n / 1e7).toLocaleString("en-IN", { maximumFractionDigits: digits })} Cr`;
};
export const toneOf = (x: number | null | undefined) => (x == null ? "text-muted" : x > 0 ? "text-gain" : x < 0 ? "text-loss" : "text-muted");

/** Search input with an icon and a submit button; shows a spinner while searching. */
export function SearchBox({ value, onChange, onSubmit, placeholder, busy, label = "Search", autoFocus }: {
  value: string; onChange: (v: string) => void; onSubmit: () => void; placeholder: string; busy?: boolean; label?: string; autoFocus?: boolean;
}) {
  return (
    <form
      className="flex flex-col gap-2 sm:flex-row"
      onSubmit={(e) => {
        e.preventDefault();
        onSubmit();
      }}
    >
      <div className="relative flex-1">
        <Search className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-muted" />
        <input
          className={cx(inputClass, "h-10 w-full pl-9")}
          placeholder={placeholder}
          value={value}
          aria-label={placeholder}
          autoFocus={autoFocus}
          onChange={(e) => onChange(e.target.value)}
        />
      </div>
      <button
        type="submit"
        disabled={busy}
        className="inline-flex h-10 items-center justify-center gap-1.5 rounded-lg bg-brand px-4 text-sm font-medium text-brand-fg shadow-sm transition hover:bg-brand-strong hover:shadow-glow active:scale-[0.97] disabled:opacity-60"
      >
        {busy ? <Loader2 className="size-4 animate-spin" /> : <Search className="size-4" />}
        {label}
      </button>
    </form>
  );
}

/** Low–high range with a marker for the current value (e.g. the 52-week range). */
export function RangeBar({ low, high, value, lowLabel = "Low", highLabel = "High", format = (v: number) => inr(v) }: {
  low: number; high: number; value: number; lowLabel?: string; highLabel?: string; format?: (v: number) => string;
}) {
  const pos = high > low ? Math.max(0, Math.min(1, (value - low) / (high - low))) : 0.5;
  return (
    <div>
      <div className="relative h-2.5 rounded-full bg-gradient-to-r from-loss/60 via-warn/50 to-gain/60">
        <span
          className="absolute top-1/2 size-4 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-card bg-foreground shadow-pop transition-[left] duration-700 ease-out"
          style={{ left: `${pos * 100}%` }}
          aria-hidden
        />
      </div>
      <div className="mt-2 flex justify-between text-xs">
        <span>
          <span className="block text-muted">{lowLabel}</span>
          <span className="num font-medium">{format(low)}</span>
        </span>
        <span className="text-center">
          <span className="block text-muted">Now</span>
          <span className="num font-semibold">{format(value)}</span>
        </span>
        <span className="text-right">
          <span className="block text-muted">{highLabel}</span>
          <span className="num font-medium">{format(high)}</span>
        </span>
      </div>
      <p className="sr-only">
        {format(value)} is {Math.round(pos * 100)}% of the way from {format(low)} to {format(high)}
      </p>
    </div>
  );
}

/** Label / value rows, e.g. a bond's terms. */
export function Facts({ rows, className }: { rows: { label: ReactNode; value: ReactNode; help?: ReactNode }[]; className?: string }) {
  return (
    <dl className={cx("divide-y divide-border/60 text-sm", className)}>
      {rows.map((r, i) => (
        <div key={i} className="flex items-center justify-between gap-3 py-2">
          <dt className="flex items-center gap-1 text-muted">
            {r.label}
            {r.help && <InfoTip>{r.help}</InfoTip>}
          </dt>
          <dd className="num text-right font-medium">{r.value}</dd>
        </div>
      ))}
    </dl>
  );
}

/** A small labelled number used inside cards (lighter than Stat). */
export function Metric({ label, value, help, tone, sub }: { label: ReactNode; value: ReactNode; help?: ReactNode; tone?: string; sub?: ReactNode }) {
  return (
    <div className="rounded-lg bg-background-subtle/70 px-3 py-2.5 ring-1 ring-inset ring-border/60">
      <p className="flex items-center gap-1 text-[11px] font-medium text-muted">
        {label}
        {help && <InfoTip>{help}</InfoTip>}
      </p>
      <p className={cx("num mt-0.5 text-base font-semibold", tone)}>{value}</p>
      {sub && <p className="mt-0.5 truncate text-[11px] text-muted">{sub}</p>}
    </div>
  );
}

/** Vertical timeline of dated events; `highlight` rows get a brand dot and badge. */
export function Timeline({ items }: {
  items: { key: string; date: string | null; title: ReactNode; meta?: ReactNode; highlight?: boolean; badge?: ReactNode }[];
}) {
  return (
    <ol className="relative space-y-4 border-l border-border pl-5">
      {items.map((it) => (
        <li key={it.key} className="relative animate-fade-in">
          <span
            className={cx(
              "absolute top-1 -left-[26.5px] size-3 rounded-full border-2 border-card",
              it.highlight ? "bg-brand shadow-glow" : "bg-border-strong",
            )}
          />
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
            <span className="num text-xs text-muted">{it.date ? shortDate(it.date) : "date n/a"}</span>
            {it.badge}
          </div>
          <p className="mt-0.5 text-sm">{it.title}</p>
          {it.meta && <p className="mt-0.5 text-xs text-muted">{it.meta}</p>}
        </li>
      ))}
    </ol>
  );
}

export type Period = "1M" | "3M" | "6M" | "1Y" | "3Y" | "5Y";
export const PERIOD_DAYS: Record<Period, number> = { "1M": 31, "3M": 92, "6M": 183, "1Y": 366, "3Y": 1096, "5Y": 1827 };

/** A time series with a period selector; rows older than the period are dropped here (not in the chart), so the
 *  change % in the header is for the chosen period. */
export function PeriodChart({ data, series, periods, period, onPeriod, format, height = 280, extra }: {
  data: Record<string, unknown>[]; series: Series[]; periods: Period[]; period: Period; onPeriod: (p: Period) => void;
  format?: (v: number) => string; height?: number; extra?: ReactNode;
}) {
  const rows = useMemo(() => {
    if (!data.length) return data;
    const last = new Date(String(data[data.length - 1].date).slice(0, 10)).getTime();
    const cutoff = last - PERIOD_DAYS[period] * 86400000;
    return data.filter((r) => new Date(String(r.date).slice(0, 10)).getTime() >= cutoff);
  }, [data, period]);
  const change = rows.length > 1 ? Number(rows[rows.length - 1][series[0].key]) / Number(rows[0][series[0].key]) - 1 : null;
  return (
    <div>
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <p className="text-xs text-muted">
          {period} change{" "}
          <span className={cx("num text-sm font-semibold", toneOf(change))}>{signedPct(change)}</span>
          {rows.length > 1 && (
            <span className="ml-1">
              ({shortDate(String(rows[0].date))} → {shortDate(String(rows[rows.length - 1].date))})
            </span>
          )}
        </p>
        <div className="flex items-center gap-2">
          {extra}
          <Segmented value={period} onChange={onPeriod} options={periods.map((p) => ({ value: p, label: p }))} />
        </div>
      </div>
      <TimeSeriesChart key={period} data={rows} format={format} height={height} showChange={false}
        series={series.map((s, i) => (i === 0 && !s.color ? { ...s, color: (change ?? 0) >= 0 ? "var(--gain)" : "var(--loss)" } : s))} />
    </div>
  );
}

/** A compact list row linking somewhere, with a trailing slot. */
export function LinkRow({ href, title, sub, trailing }: { href: string; title: ReactNode; sub?: ReactNode; trailing?: ReactNode }) {
  return (
    <li>
      <Link href={href} className="group flex items-center gap-3 rounded-lg px-2 py-2.5 transition hover:bg-card-hover">
        <div className="min-w-0 flex-1">
          <p className="truncate text-sm font-medium group-hover:text-brand">{title}</p>
          {sub && <p className="truncate text-xs text-muted">{sub}</p>}
        </div>
        {trailing}
      </Link>
    </li>
  );
}

/** NSE lists one rating per agency, e.g. "AAA/, AAA/, AA+/": the distinct grades, "AAA" or "AAA / AA+". */
export function ratingLabel(r: string | null): string {
  const grades = (r ?? "").split(",").map((x) => x.replace(/\/+\s*$/, "").trim()).filter(Boolean);
  return [...new Set(grades)].join(" / ");
}

/** Rough credit tone from the rating's letters (AAA best). */
export function ratingTone(r: string | null): "gain" | "info" | "warn" | "loss" | "neutral" {
  if (!r) return "neutral";
  const g = r.toUpperCase().replace(/\(.*?\)|\/.*$/g, "").trim();
  if (g.startsWith("AAA")) return "gain";
  if (g.startsWith("AA")) return "info";
  if (g.startsWith("A")) return "warn";
  return "loss";
}

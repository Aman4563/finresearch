"use client";

// IPO helpers shared by the dashboard, the IPO screener and the monitor: IST dates, countdowns, price bands, lot costs,
// subscription meters and the lifecycle stepper.

import { Check } from "lucide-react";
import { type ReactNode, useSyncExternalStore } from "react";

import { InfoTip, cx } from "@/components/ui";
import type { Issue, WatchDetail, WatchSummary } from "@/lib/api";

// ------------------------------------------------------------------ time (IST)

const IST = "Asia/Kolkata";

/** The current time, re-rendering every `stepMs`. `null` during server render so dates never mismatch on hydration. */
export function useNow(stepMs = 30000): number | null {
  return useSyncExternalStore(
    (cb) => {
      const t = setInterval(cb, stepMs);
      return () => clearInterval(t);
    },
    () => Math.floor(Date.now() / stepMs) * stepMs,
    () => null,
  );
}

/** YYYY-MM-DD of an instant in IST. */
export const istDate = (ms: number) => new Date(ms).toLocaleDateString("en-CA", { timeZone: IST });

/** Whole days from IST today to a calendar date (negative = past). */
export function daysUntil(iso: string | null | undefined, now: number) {
  if (!iso) return null;
  const a = Date.parse(`${istDate(now)}T00:00:00Z`), b = Date.parse(`${iso.slice(0, 10)}T00:00:00Z`);
  return Math.round((b - a) / 86400000);
}

/** An instant for a calendar date at an IST wall-clock time, e.g. the 5 PM UPI cut-off on the close date. */
export const istAt = (iso: string, hhmm = "17:00") => Date.parse(`${iso.slice(0, 10)}T${hhmm}:00+05:30`);

/** "3d 4h", "2h 15m", "12m" until `target`; "now" when past. */
export function countdown(target: number, now: number) {
  const s = Math.max(0, Math.round((target - now) / 1000));
  if (s === 0) return "now";
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  if (d) return `${d}d ${h}h`;
  if (h) return `${h}h ${m}m`;
  return `${Math.max(1, m)}m`;
}

/** "Today", "Tomorrow", "in 3 days", "2 days ago". */
export function relDay(days: number | null) {
  if (days == null) return "";
  if (days === 0) return "Today";
  if (days === 1) return "Tomorrow";
  if (days === -1) return "Yesterday";
  return days > 0 ? `in ${days} days` : `${-days} days ago`;
}

export const dayLabel = (iso: string, opts: Intl.DateTimeFormatOptions = { weekday: "short", day: "numeric", month: "short" }) =>
  new Date(`${iso.slice(0, 10)}T00:00:00Z`).toLocaleDateString("en-IN", { ...opts, timeZone: "UTC" });

export const timeIST = (iso: string) =>
  new Date(iso).toLocaleTimeString("en-IN", { hour: "numeric", minute: "2-digit", timeZone: IST });

// ------------------------------------------------------------------ money and bands

/** Stat formatter for counts (the count-up passes fractions while it animates). */
export const int = (n: number) => Math.round(n).toLocaleString("en-IN");

export const inr = (v: number, digits = 0) => `₹${v.toLocaleString("en-IN", { maximumFractionDigits: digits })}`;

/** "Rs.132 to Rs.139" or "107.00 - 113.00" -> [132, 139]. */
export function parseBand(band: string | null | undefined): [number, number] | null {
  if (!band) return null;
  const n = [...band.matchAll(/(\d+(?:\.\d+)?)/g)].map((m) => Number(m[1])).filter((x) => x > 0);
  if (!n.length) return null;
  return [Math.min(...n), Math.max(...n)];
}

export const isSme = (i: Pick<Issue, "series" | "exchange">) => i.series === "SME";

/** Retail cost at the upper band: one lot and the minimum application (SME issues need at least `min_lots`). */
export function lotCost(i: Issue) {
  const band = parseBand(i.price_band);
  if (!band || !i.lot_size) return null;
  const lot = i.lot_size * band[1];
  return { lot, min: lot * (i.min_lots || 1), minLots: i.min_lots || 1 };
}

export const times = (x: string | number | null | undefined) => {
  if (x == null || x === "") return null;
  const n = Number(x);
  return Number.isFinite(n) ? n : null;
};

export const fmtX = (n: number) => `${n >= 100 ? n.toFixed(0) : n >= 10 ? n.toFixed(1) : n.toFixed(2)}x`;

// ------------------------------------------------------------------ subscription meter

/** Bar on a log scale (0x … 100x+) with a tick at 1x (fully subscribed). Amber below 1x, green above. */
export function SubMeter({ value, className, compact }: { value: number | null; className?: string; compact?: boolean }) {
  const pos = (x: number) => Math.min(1, Math.log10(1 + x) / Math.log10(101));
  const one = pos(1);
  return (
    <div className={cx("flex items-center gap-2", className)}>
      <div className="relative h-2 flex-1 overflow-hidden rounded-full bg-background-subtle" aria-hidden>
        <div
          className={cx("h-full rounded-full transition-[width] duration-700 ease-out", value == null ? "" : value >= 1 ? "bg-gain" : "bg-warn")}
          style={{ width: `${value == null ? 0 : Math.max(2, pos(value) * 100)}%` }}
        />
        <span className="absolute inset-y-0 w-px bg-foreground/40" style={{ left: `${one * 100}%` }} />
      </div>
      {!compact && (
        <span className={cx("num w-14 shrink-0 text-right text-xs font-semibold", value == null ? "text-muted" : value >= 1 ? "text-gain" : "text-warn")}>
          {value == null ? "—" : fmtX(value)}
        </span>
      )}
    </div>
  );
}

export const SUB_HELP =
  "Times subscribed = shares applied for ÷ shares on offer. 1x means every share on offer has a bid; above 1x the issue is oversubscribed and allotment becomes a lottery for retail.";

export const TERMS: Record<string, ReactNode> = {
  QIB: "Qualified Institutional Buyers: mutual funds, banks, insurers and foreign funds. Strong QIB demand is read as a vote of confidence by professionals.",
  NII: "Non-Institutional Investors (HNIs): individuals and companies bidding over ₹2 lakh. Often funded by loans, so their demand can be short-term.",
  Retail: "Retail Individual Investors: individuals bidding up to ₹2 lakh. If retail is oversubscribed, allotment is by lottery (one lot per winner).",
  anchor: "Anchor investors are large institutions allotted shares a day before the issue opens. Their shares are locked in for 30 days (half) and 90 days (the rest); the end of a lock-in can add selling pressure.",
  lot: "A lot is the minimum number of shares you can bid for, and bids come in whole lots. Lot cost = lot size × upper price band.",
  band: "The price range the company set. Retail investors usually bid at the cut-off (the upper end), so costs here use the upper band.",
  upi: "Bids are paid with a UPI mandate. The mandate must be approved in your UPI app by 5 PM IST on the closing day or the bid is not counted.",
};

// ------------------------------------------------------------------ categories (NSE bid details)

type Snap = WatchDetail["subscription"][number];

/** Top-level categories of a snapshot, with friendly names. */
export function categories(s: Snap) {
  const out: { key: string; label: string; times: number | null }[] = [];
  for (const c of s.categories) {
    const t = times(c.times);
    if (c.code === "1") out.push({ key: "qib", label: "QIB", times: t });
    else if (c.code === "2") out.push({ key: "nii", label: "NII", times: t });
    else if (c.code === "3") out.push({ key: "retail", label: "Retail", times: t });
    else if (c.code === "4") out.push({ key: "emp", label: "Employees", times: t });
  }
  const total = times(s.total_times);
  if (total != null) out.push({ key: "total", label: "Total", times: total });
  return out;
}

// ------------------------------------------------------------------ lifecycle stepper

export type Stage = { key: string; label: string; date: string | null; hint?: string };

export function stagesFor(w: Pick<WatchSummary, "open_date" | "close_date" | "allotment_date" | "listing_date" | "meta">): Stage[] {
  return [
    { key: "bidding", label: "Bidding", date: w.close_date, hint: w.open_date && w.close_date ? `${dayLabel(w.open_date, { day: "numeric", month: "short" })} – ${dayLabel(w.close_date, { day: "numeric", month: "short" })}` : undefined },
    { key: "allotment", label: "Allotment", date: w.allotment_date },
    { key: "listing", label: "Listing", date: w.listing_date, hint: w.meta?.listing_confirmed ? "confirmed" : w.listing_date ? "expected" : undefined },
    { key: "lockins", label: "Lock-ins", date: null, hint: "30 / 90 days" },
  ];
}

/** Index of the current stage: bidding until the close date, allotment until listing, listing on the day, then lock-ins. */
export function currentStage(w: Pick<WatchSummary, "open_date" | "close_date" | "allotment_date" | "listing_date">, now: number) {
  const d = (x: string | null) => daysUntil(x, now);
  const close = d(w.close_date), allot = d(w.allotment_date), list = d(w.listing_date);
  if (close == null || close >= 0) return 0;
  if (allot != null && allot >= 0) return 1;
  if (list != null && list >= 0) return 2;
  return 3;
}

export function Stepper({ stages, current, className }: { stages: Stage[]; current: number; className?: string }) {
  return (
    <ol className={cx("flex items-start", className)}>
      {stages.map((s, i) => {
        const done = i < current, active = i === current;
        return (
          <li key={s.key} className="relative flex flex-1 flex-col items-center text-center">
            {i > 0 && (
              <span className={cx("absolute top-3 right-1/2 h-0.5 w-full -translate-y-1/2", i <= current ? "bg-brand" : "bg-border")} aria-hidden />
            )}
            <span
              className={cx(
                "relative z-10 grid size-6 place-items-center rounded-full border-2 text-[10px] font-semibold transition",
                done && "border-brand bg-brand text-brand-fg",
                active && "border-brand bg-card text-brand animate-pulse-ring",
                !done && !active && "border-border bg-card text-muted",
              )}
            >
              {done ? <Check className="size-3.5" /> : i + 1}
            </span>
            <span className={cx("mt-1.5 text-[11px] font-medium", active ? "text-foreground" : "text-muted")}>{s.label}</span>
            <span className="num text-[10px] text-muted">
              {s.key === "bidding" ? s.hint : s.date ? dayLabel(s.date, { day: "numeric", month: "short" }) : s.hint}
            </span>
            {s.key === "listing" && s.hint === "expected" && <span className="text-[10px] text-muted/80">expected</span>}
          </li>
        );
      })}
    </ol>
  );
}

/** A small "live" dot for open issues. */
export function LiveDot({ className }: { className?: string }) {
  return <span className={cx("inline-block size-2 shrink-0 rounded-full bg-gain text-gain animate-pulse-ring", className)} aria-hidden />;
}

export function Term({ k, children }: { k: keyof typeof TERMS | string; children: ReactNode }) {
  return (
    <span className="inline-flex items-center gap-1">
      {children}
      {TERMS[k] && <InfoTip>{TERMS[k]}</InfoTip>}
    </span>
  );
}

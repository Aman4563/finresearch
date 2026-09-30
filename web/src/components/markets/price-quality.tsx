"use client";

// Price labels and the data-quality signal on the stock pages: every price says what it is (last traded, official
// close) and when, and when two published sources disagree beyond tolerance both values are shown with their
// sources instead of one being picked silently (src/finresearch/fincalc/price.py, issue #133).

import { AlertTriangle } from "lucide-react";

import { inr } from "@/components/markets/common";
import type { QualityFlag, StockOverview } from "@/components/markets/types";
import { Callout } from "@/components/ui";

type Q = NonNullable<StockOverview["quote"]>;

/** The price a page shows: the API's display price, falling back to the last trade for older payloads. */
export const displayPrice = (q: Partial<Q> | null | undefined): number | null => {
  const v = q?.price ?? q?.last_price;
  return v != null && v !== "" ? Number(v) : null;
};

const IST = { timeZone: "Asia/Kolkata" } as const;
const stamp = (iso: string | null | undefined) =>
  iso ? new Date(iso).toLocaleString("en-IN", { ...IST, day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", hour12: false }) : null;

/** "Close (official) · 30 Sep, 16:00" */
export function priceCaption(q: Partial<Q> | null | undefined): string {
  const label = q?.price_label ?? "Last price";
  const at = stamp(q?.as_of);
  return at ? `${label} · ${at}` : label;
}

/** "Last traded ₹420.00" under an official close that differs from the last trade. */
export function lastTradedNote(q: Partial<Q> | null | undefined): string | null {
  return q?.last_differs && q.last_price ? `Last traded ${inr(q.last_price)}` : null;
}

/** Tolerance for "the same published close" across two reads of one exchange (rupees). */
const SAME_CLOSE_TOL = 0.05;

/** The quote's own flags plus the page's cross-check: after the close, the quote's official close against the daily
 * history's bar for the same day (both are the exchange's; they should be equal). */
export function qualityFlags(q: Partial<Q> | null | undefined, lastBar: { date: string; close: number } | undefined, exchange: string): QualityFlag[] {
  const out = [...(q?.quality ?? [])];
  const px = displayPrice(q);
  const day = q?.as_of ? new Date(q.as_of).toLocaleDateString("en-CA", IST) : null;
  if (q?.price_kind === "official_close" && px != null && lastBar && day && lastBar.date.slice(0, 10) === day
      && Math.abs(lastBar.close - px) > SAME_CLOSE_TOL) {
    out.push({ field: "close", severity: "warn", message: `${exchange}'s quote and its daily history give different closes for ${day}.`,
      values: [{ value: String(px), source: `${exchange} quote (official close)` }, { value: String(lastBar.close), source: `${exchange} daily history bar` }] });
  }
  return out;
}

export function QualityNote({ flags }: { flags: QualityFlag[] }) {
  if (!flags.length) return null;
  return (
    <Callout tone="warn" icon={<AlertTriangle className="size-4" />} title="Sources disagree">
      <ul className="space-y-1">
        {flags.map((f, i) => (
          <li key={`${f.field}-${i}`}>
            {f.message}{" "}
            {f.values.map((v, j) => (
              <span key={j} className="whitespace-nowrap">
                {j > 0 && " vs "}<span className="num font-medium">{f.field === "market_cap" ? crorePlain(v.value) : inr(v.value)}</span>
                <span className="text-muted"> ({v.source})</span>
              </span>
            ))}
          </li>
        ))}
      </ul>
    </Callout>
  );
}

function crorePlain(v: string | null) {
  if (v == null) return "—";
  return `₹${(Number(v) / 1e7).toLocaleString("en-IN", { maximumFractionDigits: 0 })} Cr`;
}

"use client";

// The evidence charts beside the fund and bond signals: where a fund's rolling returns ranked in its category at each
// quarter end, and a bond's yield ladder from the risk-free alternatives to its expected-loss-adjusted yield.

import { Layers, Loader2, Target } from "lucide-react";
import { useState } from "react";

import { BarsChart, TimeSeriesChart } from "@/components/charts";
import { Metric, pctOf } from "@/components/markets/common";
import type { Signal } from "@/components/signal";
import { Card, EmptyState, ErrorNote, Segmented, Skeleton } from "@/components/ui";
import { useApi } from "@/lib/api";

type Window = { end: string; years: number; fund: number; median: number; p25: number; p75: number; percentile: number; peers: number };
export type FundConsistency = {
  scheme: { scheme_code: string; name: string; category: string | null };
  direct: boolean; passive: boolean; peers: number;
  windows_3y: Window[]; windows_1y: Window[];
  quarterly: { quarters?: number; down_quarters?: number; downside_capture?: number | null; r_squared?: number | null; drawdown?: number | null; category_drawdown?: number | null };
  ter: { matched?: boolean; ter?: number | null; percentile?: number | null; category_median?: number | null; peers?: number };
};

export function FundConsistencyCard({ code }: { code: string }) {
  const { data, error, reload } = useApi<FundConsistency>(`/api/funds/${code}/consistency`);
  const [win, setWin] = useState<"3y" | "1y">("3y");
  const [view, setView] = useState<"rank" | "returns">("rank");
  const ws = data ? (win === "3y" ? data.windows_3y : data.windows_1y) : [];
  const hits = ws.filter((w) => w.fund >= w.median).length;
  const q = data?.quarterly;
  return (
    <Card title="Consistency against the category" icon={<Target className="size-4" />}
      subtitle={data ? `${win === "3y" ? "3-year" : "1-year"} returns at each quarter end, ranked among ${data.direct ? "direct" : "regular"}-plan growth peers` : "Rolling returns ranked within the SEBI category"}
      help="At every quarter end, the fund's return over the previous 1 or 3 years is ranked among today's schemes in its category (same plan type). 100th percentile = best, 50th = the median. Consistently above the middle matters more than one good year."
      actions={<Segmented value={win} onChange={setWin} options={[{ value: "3y", label: "3Y" }, { value: "1y", label: "1Y" }]} />}>
      {error ? (
        <ErrorNote error={`Could not rank the fund: ${error}`} onRetry={reload} />
      ) : !data ? (
        <div className="space-y-3">
          <Skeleton className="h-[220px] w-full rounded-lg" />
          <p className="flex items-center gap-2 text-xs text-muted"><Loader2 className="size-3.5 animate-spin" /> Reading every scheme&apos;s NAV at each quarter end from AMFI. The first time takes a minute or two; after that it&apos;s kept.</p>
        </div>
      ) : data.passive ? (
        <EmptyState title="An index fund or ETF">A passive fund aims to match its index, not beat its category: compare its cost and tracking error with other funds on the same index.</EmptyState>
      ) : ws.length === 0 ? (
        <EmptyState title="Not enough history">This needs {win === "3y" ? "three" : "one"} years of NAVs and at least five category peers at both ends of a window.</EmptyState>
      ) : (
        <>
          <div className="mb-3 flex justify-end">
            <Segmented value={view} onChange={setView} options={[{ value: "rank", label: "Percentile" }, { value: "returns", label: "Returns" }]} />
          </div>
          {view === "rank" ? (
            <TimeSeriesChart height={210} area={false} showChange={false} curve="linear" dots yDomain={[0, 100]}
              data={ws.map((w) => ({ date: w.end, pct: Math.round(w.percentile * 1000) / 10 }))}
              series={[{ key: "pct", label: "Percentile in category", color: "var(--chart-1)" }]}
              references={[{ y: 50, label: "category median" }]} format={(v) => `${v.toFixed(0)}th`} />
          ) : (
            <TimeSeriesChart height={210} area={false} showChange={false} curve="linear"
              data={ws.map((w) => ({ date: w.end, fund: w.fund * 100, median: w.median * 100, p75: w.p75 * 100, p25: w.p25 * 100 }))}
              series={[{ key: "fund", label: "This fund", color: "var(--chart-1)" }, { key: "median", label: "Category median", color: "var(--chart-2)" },
                { key: "p75", label: "Top quartile", color: "var(--chart-2)", dashed: true }, { key: "p25", label: "Bottom quartile", color: "var(--muted)", dashed: true }]}
              format={(v) => `${v.toFixed(1)}%`} />
          )}
          <p className="mt-1 text-center text-[11px] text-muted">Each point is the {win === "3y" ? "3" : "1"}-year window ending that quarter; x is the window&apos;s end.</p>
          <div className="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-4">
            <Metric label="At or above median" value={`${hits} / ${ws.length}`} sub={pctOf(hits / ws.length, 0)} />
            <Metric label="Downside capture" value={q?.downside_capture != null ? pctOf(q.downside_capture, 0) : "—"}
              sub={q?.down_quarters ? `${q.down_quarters} falling quarters` : "too few falls"}
              help="In the quarters the category median fell, how much the fund fell by comparison. Below 100% means it cushioned the falls." />
            <Metric label="Moves with category" value={q?.r_squared != null ? `R² ${q.r_squared.toFixed(2)}` : "—"}
              help="The share of the fund's quarterly swings that moved with the category median. A low value can mean the fund invests unlike its label (style drift)." />
            <Metric label="Expense ratio" value={data.ter.ter != null ? `${data.ter.ter.toFixed(2)}%` : "—"}
              sub={data.ter.category_median != null ? `median ${data.ter.category_median.toFixed(2)}%` : "not in TER file"} />
          </div>
        </>
      )}
    </Card>
  );
}

// factor names from finresearch.signals.bond.LADDER; the risk-free alternatives share one colour, the bond another
const LADDER: { name: string; label: string; color: string }[] = [
  { name: "G-sec par yield", label: "G-sec", color: "var(--chart-2)" },
  { name: "FD rate", label: "FD", color: "var(--chart-2)" },
  { name: "Pre-tax YTM", label: "Bond", color: "var(--chart-1)" },
  { name: "G-sec after tax", label: "G-sec, after tax", color: "var(--chart-2)" },
  { name: "FD after tax", label: "FD, after tax", color: "var(--chart-2)" },
  { name: "Post-tax YTM", label: "Bond, after tax", color: "var(--chart-1)" },
  { name: "Post-tax, after expected loss", label: "Bond, after tax & loss", color: "var(--chart-1)" },
];

export function BondLadderCard({ isin, query }: { isin: string; query: Record<string, string> }) {
  const qs = new URLSearchParams(query).toString();
  const { data, error } = useApi<Signal>(`/api/signals/bond/${encodeURIComponent(isin)}${qs ? `?${qs}` : ""}`);
  const byName = new Map((data?.factors ?? []).map((f) => [f.name, f]));
  const rows = LADDER.flatMap((r) => {
    const v = byName.get(r.name)?.value;
    return typeof v === "number" ? [{ label: r.label, y: v, color: r.color }] : [];
  });
  const spread = byName.get("Spread over G-sec (after tax and loss)");
  return (
    <Card title="Yield ladder" icon={<Layers className="size-4" />}
      subtitle="Every rate as an effective yearly yield, from the risk-free options to what the bond keeps after tax and expected default losses"
      help="Half-yearly G-sec coupons, quarterly FD compounding and the bond's own coupon schedule are all turned into one yearly rate so they compare fairly. The last bar deducts the average yearly loss CRISIL's default history implies for the bond's rating grade.">
      {error ? (
        <p className="text-sm text-muted">No ladder: {error}</p>
      ) : !data ? (
        <Skeleton className="h-[250px] w-full rounded-lg" />
      ) : rows.length === 0 ? (
        <EmptyState title="Nothing to compare">NSE&apos;s list lacks the price, coupon or maturity needed for the yields.</EmptyState>
      ) : (
        <>
          <BarsChart data={rows} x="label" layout="vertical" labelWidth={132} height={Math.max(160, rows.length * 34)}
            series={[{ key: "y", label: "Yield a year" }]} colorBy={(r) => String(r.color)} format={(v) => `${v.toFixed(2)}%`} />
          <p className="mt-1 text-[11px] text-muted">Indigo: the risk-free alternatives. Teal: this bond.</p>
          <p className="mt-2 text-xs text-muted">
            {spread && typeof spread.value === "number" ? (
              <>After tax and expected loss the bond earns <span className={spread.value >= 0 ? "num font-semibold text-gain" : "num font-semibold text-loss"}>{spread.value >= 0 ? "+" : ""}{spread.value.toFixed(2)} pp</span> over the matching G-sec.</>
            ) : (
              <>No rating to price the default risk, so the last bar is missing.</>
            )}
          </p>
        </>
      )}
    </Card>
  );
}

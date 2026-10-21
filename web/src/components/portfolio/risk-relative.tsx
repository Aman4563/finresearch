"use client";

// Risk tab, #349: benchmark-relative lines (active return, information ratio, up/down capture), rolling volatility and
// beta, the drawdown table and the walk-forward VaR backtest. Each figure, or the reason it is missing, comes from
// /api/portfolio/analytics/risk; an unknown is shown with its reason, never as 0.

import { CheckCircle2, LineChart, TrendingDown, TriangleAlert } from "lucide-react";

import { TimeSeriesChart } from "@/components/charts";
import { Badge, Card, Table } from "@/components/ui";
import { day } from "@/lib/api";

import { type Metric, MetricRow, pct, spct } from "./shared-analytics";

export type Rolling = { window: number; rows: { day: string; volatility: number | null; beta: number | null }[]; how: string; reason?: string; note?: string };
export type Episode = { peak: string; trough: string; depth: number; recovered: string | null; days_to_recover: number | null; underwater_days: number };
export type VarBacktest = {
  level: number; window: number; how: string; reason?: string; days?: number; exceptions?: number; expected?: number;
  p_uc?: number; p_ind?: number; p_cc?: number; verdict?: "consistent" | "too_many" | "too_few" | "clustered"; note?: string;
};
export type Relative = { active_return?: Metric; information_ratio?: Metric; up_capture?: Metric; down_capture?: Metric };

const ratio = (v: number) => v.toFixed(2);

export function RelativeCard({ r }: { r: Relative }) {
  if (!r.information_ratio) return null;
  return (
    <Card title="Against the index" icon={<LineChart className="size-4" />}
      help="How your returns compared with NIFTYBEES (the Nifty 50 ETF) day by day over the same window as the figures above.">
      <MetricRow label="Active return (a year)" m={r.active_return} format={(v) => spct(v)} help="Your average daily return minus the index's, scaled to a year. Positive: you were ahead on average." />
      <MetricRow label="Information ratio" m={r.information_ratio} format={ratio} help="Active return per unit of tracking error. Above about 0.5 sustained over years is rare; over a year or two it is mostly noise." />
      <MetricRow label="Up capture" m={r.up_capture} format={(v) => pct(v, 0)} help="On days the index rose, how much of its rise you got (100% = the same)." />
      <MetricRow label="Down capture" m={r.down_capture} format={(v) => pct(v, 0)} help="On days the index fell, how much of its fall you took (below 100% = you fell less)." />
    </Card>
  );
}

export function RollingCard({ rolling }: { rolling?: Rolling }) {
  if (!rolling) return null;
  const rows = rolling.rows.map((x) => ({ date: x.day, vol: x.volatility == null ? null : x.volatility * 100, beta: x.beta }));
  const hasBeta = rows.some((x) => x.beta != null);
  return (
    <Card title="Rolling volatility and beta" icon={<LineChart className="size-4" />}
      help={`Each point uses the ${rolling.window} trading days before it (about 6 months), so you can see whether risk is rising or falling, not just its average.`}>
      {rolling.reason ? <p className="text-sm text-muted">{rolling.reason}.</p> : (
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
          <div><p className="mb-1 text-xs text-muted">Volatility (annualised)</p>
            <TimeSeriesChart data={rows} area={false} height={200} showChange={false} format={(v) => `${v.toFixed(1)}%`} series={[{ key: "vol", label: "Rolling volatility", color: "var(--chart-1)" }]} /></div>
          <div><p className="mb-1 text-xs text-muted">Beta vs NIFTYBEES</p>
            {hasBeta
              ? <TimeSeriesChart data={rows} area={false} height={200} showChange={false} format={(v) => v.toFixed(2)} references={[{ y: 1, label: "1", tone: "muted" }]} series={[{ key: "beta", label: "Rolling beta", color: "var(--chart-2)" }]} />
              : <p className="text-sm text-muted">{rolling.note ?? "No rolling beta."}</p>}</div>
        </div>
      )}
    </Card>
  );
}

export function DrawdownTable({ items }: { items?: Episode[] }) {
  if (!items || items.length === 0) return null;
  return (
    <Card title="Largest drawdowns" icon={<TrendingDown className="size-4" />} help="The deepest falls of your time-weighted index from a previous high, and how long each took to win back.">
      <Table label="Largest drawdowns">
        <thead><tr><th>Peak</th><th>Trough</th><th className="text-right">Fall</th><th>Recovered</th><th className="text-right">Days below peak</th></tr></thead>
        <tbody>{items.map((e) => (
          <tr key={e.peak}><td>{day(e.peak)}</td><td>{day(e.trough)}</td><td className="num text-right text-loss">{pct(e.depth)}</td>
            <td>{e.recovered ? `${day(e.recovered)} (${e.days_to_recover} days)` : <Badge tone="warn">not yet</Badge>}</td>
            <td className="num text-right">{e.underwater_days}</td></tr>
        ))}</tbody>
      </Table>
    </Card>
  );
}

export function VarBacktestLine({ b }: { b?: VarBacktest }) {
  if (!b) return null;
  if (b.reason) return <p className="mt-2 text-[11px] text-muted">VaR backtest: {b.reason}.</p>;
  const ok = b.verdict === "consistent";
  return (
    <div className="mt-2 rounded-lg border border-border px-3 py-2 text-xs">
      <p className="flex items-center gap-1.5 font-medium">
        {ok ? <CheckCircle2 className="size-3.5 text-gain" /> : <TriangleAlert className="size-3.5 text-warn" />}
        VaR backtest: {b.exceptions} of {b.days} days lost more than the 1-day VaR (expected {b.expected?.toFixed(1)})
      </p>
      <p className="mt-0.5 text-muted">{b.note} Kupiec p = {b.p_uc?.toFixed(3)}, conditional coverage p = {b.p_cc?.toFixed(3)}. Method: {b.how}.</p>
    </div>
  );
}

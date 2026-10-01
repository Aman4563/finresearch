"use client";

// How past mainboard IPOs opened on listing day, by final subscription band × regime (finresearch.fincalc.ipo.base_rates,
// GET /api/ipo/base-rates). Bars carry their uncertainty as whiskers: the 95% Wilson interval for a probability, the
// interquartile range for the median return. Roadmap §D.1.

import { History, Table2 } from "lucide-react";
import { useMemo, useState } from "react";
import { Bar, BarChart, CartesianGrid, ErrorBar, Legend, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import { Badge, Callout, Card, EmptyState, ErrorNote, InfoTip, Segmented, Skeleton, Table } from "@/components/ui";
import { useApi } from "@/lib/api";

export type BaseRateCell = {
  band: string; regime: "pre_2022" | "post_2022"; n: number; median: number | null; q1: number | null; q3: number | null;
  p10: number | null; p90: number | null; mean: number | null; p_loss: number | null; p_loss_ci: [number, number] | null;
  p_gain: number | null; p_gain_ci: [number, number] | null;
};
type ModelFold = { year: number; n_test?: number; bss_vs_table?: number | null; auc_model?: number | null; skipped?: string };
export type BaseRates = {
  by: "qib" | "total"; bands: string[]; cells: BaseRateCell[]; n: number; source: "database" | "snapshot" | "empty";
  as_of: string | null; caveat: string; scope: string;
  model: { gate: { rule: string; years_evaluated: number[]; years_passed: number[]; passes: boolean }; folds: ModelFold[];
    pooled: Record<string, number | null>; uses_model: boolean;
    /** E-IPO-1: calibrated blend λ·model + (1 − λ)·table, used by the signal when it passed its own walk-forward. */
    blend?: { lambda: number; gate: { years_evaluated: number[]; years_passed: number[]; passes: boolean };
      pooled: { n: number; brier: number; brier_table: number; bss_vs_table: number } } } | null;
};
type Metric = "gain" | "loss" | "median";

const REGIME = {
  pre_2022: { label: "Before Apr-2022", color: "var(--chart-2)" },
  post_2022: { label: "From Apr-2022", color: "var(--chart-1)" },
} as const;
const pct = (v: number | null | undefined, d = 0) => (v == null ? "—" : `${(v * 100).toFixed(d)}%`);

function value(c: BaseRateCell | undefined, metric: Metric): { v: number | null; lo: number | null; hi: number | null } {
  if (!c || !c.n) return { v: null, lo: null, hi: null };
  if (metric === "median") return { v: c.median, lo: c.q1, hi: c.q3 };
  const [v, ci] = metric === "gain" ? [c.p_gain, c.p_gain_ci] : [c.p_loss, c.p_loss_ci];
  return { v, lo: ci?.[0] ?? null, hi: ci?.[1] ?? null };
}

function CellTip({ active, payload, label, cells, metric }: {
  active?: boolean; payload?: { dataKey?: string }[]; label?: string; cells: Record<string, BaseRateCell>; metric: Metric;
}) {
  if (!active || !payload?.length) return null;
  return (
    <div className="min-w-52 rounded-lg border border-border bg-card/95 px-3 py-2 text-xs shadow-pop backdrop-blur animate-scale-in">
      <p className="mb-1 font-medium text-muted">Final book {label}</p>
      {(["pre_2022", "post_2022"] as const).map((r) => {
        const c = cells[`${label}|${r}`];
        return (
          <div key={r} className="mt-1.5">
            <p className="flex items-center gap-1.5 font-medium"><span className="size-2 rounded-full" style={{ background: REGIME[r].color }} />{REGIME[r].label}<span className="num ml-auto text-muted">n={c?.n ?? 0}</span></p>
            {c?.n ? (
              <dl className="num mt-0.5 grid grid-cols-[auto_1fr] gap-x-3 text-muted">
                <dt>Opened above issue</dt><dd className="text-right text-foreground">{pct(c.p_gain)} <span className="text-muted">({pct(c.p_gain_ci?.[0])}–{pct(c.p_gain_ci?.[1])})</span></dd>
                <dt>Opened below issue</dt><dd className="text-right text-foreground">{pct(c.p_loss)} <span className="text-muted">({pct(c.p_loss_ci?.[0])}–{pct(c.p_loss_ci?.[1])})</span></dd>
                <dt>Median open return</dt><dd className="text-right text-foreground">{pct(c.median, 1)} <span className="text-muted">IQR {pct(c.q1, 0)}…{pct(c.q3, 0)}</span></dd>
              </dl>
            ) : <p className="text-muted">no issues</p>}
          </div>
        );
      })}
      <p className="mt-1.5 text-[10px] text-muted">{metric === "median" ? "Whiskers: interquartile range" : "Whiskers: 95% Wilson interval"}</p>
    </div>
  );
}

export function BaseRatesCard() {
  const [by, setBy] = useState<"qib" | "total">("qib");
  const [metric, setMetric] = useState<Metric>("gain");
  const [table, setTable] = useState(false);
  const { data, error, reload } = useApi<BaseRates>(`/api/ipo/base-rates?by=${by}`);

  const cells = useMemo(() => Object.fromEntries((data?.cells ?? []).map((c) => [`${c.band}|${c.regime}`, c])), [data]);
  const rows = useMemo(() => (data?.bands ?? []).map((band) => {
    const row: Record<string, unknown> = { band };
    for (const r of ["pre_2022", "post_2022"] as const) {
      const { v, lo, hi } = value(cells[`${band}|${r}`], metric);
      row[r] = v;
      row[`${r}_err`] = v == null || lo == null || hi == null ? [0, 0] : [Math.max(0, v - lo), Math.max(0, hi - v)];
    }
    return row;
  }), [data, cells, metric]);

  const g = data?.model?.gate;
  return (
    <Card title="How past IPOs listed" icon={<History className="size-4" />}
      subtitle="Mainboard issues by their FINAL subscription: how often the listing-day open beat the issue price. Hover a band for n, ranges and the median return."
      help="Empirical base rates from NSE's past-issue pages (final combined NSE+BSE book, ex-anchor) and listing-day prices. Before and after SEBI's April-2022 change to NII allotment the market behaved differently, so the two periods are shown apart."
      actions={<button type="button" onClick={() => setTable((t) => !t)} className="inline-flex items-center gap-1 text-xs font-medium text-brand hover:underline"><Table2 className="size-3.5" />{table ? "Chart" : "Table"}</button>}>
      <ErrorNote error={error} onRetry={reload} />
      {!data && !error ? <Skeleton className="h-72" /> : data && data.n === 0 ? (
        <EmptyState icon={<History className="size-5" />} title="No IPO history harvested yet">
          Run <code className="num">finresearch ipo harvest</code> once (about an hour, politely rate-limited) to load past NSE issues.
        </EmptyState>
      ) : data ? (
        <>
          <div className="mb-3 flex flex-wrap items-center gap-2">
            <Segmented value={metric} onChange={setMetric} options={[
              { value: "gain", label: "Opened above issue" }, { value: "loss", label: "Opened below issue" }, { value: "median", label: "Median return" },
            ]} />
            <Segmented value={by} onChange={setBy} options={[{ value: "qib", label: "By QIB" }, { value: "total", label: "By total" }]} />
            <InfoTip>QIB = qualified institutional buyers (mutual funds, insurers, banks, FPIs). Their demand is the strongest known predictor of listing gains in India. “By total” bands the whole issue’s subscription instead.</InfoTip>
            <span className="ml-auto text-[11px] text-muted"><span className="num">{data.n}</span> issues{data.as_of && <> · listings to <span className="num">{data.as_of}</span></>}{data.source === "snapshot" && <> · <Badge tone="warn">snapshot</Badge></>}</span>
          </div>
          {table ? (
            <Table label="How past IPOs listed, by subscription band">
              <thead><tr><th>{by === "qib" ? "QIB" : "Total"} band</th><th>Period</th><th className="!text-right">n</th><th className="!text-right">Above issue</th><th className="!text-right">Below issue (95% CI)</th><th className="!text-right">Median (IQR)</th></tr></thead>
              <tbody>
                {data.cells.map((c) => (
                  <tr key={`${c.band}${c.regime}`}>
                    <td className="num">{c.band}</td><td className="text-xs">{REGIME[c.regime].label}</td>
                    <td className="num text-right">{c.n}</td><td className="num text-right">{pct(c.p_gain)}</td>
                    <td className="num text-right">{pct(c.p_loss)} <span className="text-muted">{c.p_loss_ci ? `${pct(c.p_loss_ci[0])}–${pct(c.p_loss_ci[1])}` : ""}</span></td>
                    <td className="num text-right">{pct(c.median, 1)} <span className="text-muted">{c.n ? `${pct(c.q1)}…${pct(c.q3)}` : ""}</span></td>
                  </tr>
                ))}
              </tbody>
            </Table>
          ) : (
            <div className="h-72 animate-fade-in" role="img" aria-label={`Listing outcomes by ${by} subscription band, before and after April 2022`}>
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={rows} margin={{ top: 8, right: 8, bottom: 0, left: 0 }} barGap={2}>
                  <CartesianGrid vertical={false} strokeDasharray="3 3" />
                  <XAxis dataKey="band" tickLine={false} axisLine={false} fontSize={11} />
                  <YAxis tickLine={false} axisLine={false} fontSize={11} width={44} tickFormatter={(v: number) => pct(v)}
                    domain={metric === "median" ? ["auto", "auto"] : [0, 1]} />
                  {metric === "median" && <ReferenceLine y={0} stroke="var(--border-strong)" />}
                  <Tooltip cursor={{ fill: "var(--background-subtle)" }} content={<CellTip cells={cells} metric={metric} />} />
                  <Legend iconType="circle" iconSize={8} wrapperStyle={{ fontSize: 11 }} />
                  {(["pre_2022", "post_2022"] as const).map((r) => (
                    <Bar key={r} dataKey={r} name={REGIME[r].label} fill={REGIME[r].color} radius={[4, 4, 0, 0]} maxBarSize={32} animationDuration={600}>
                      <ErrorBar dataKey={`${r}_err`} width={6} strokeWidth={1.5} stroke="var(--foreground)" opacity={0.55} />
                    </Bar>
                  ))}
                </BarChart>
              </ResponsiveContainer>
            </div>
          )}
          <Callout tone="warn" title="Final numbers, not what you see before the cut-off">{data.caveat}</Callout>
          {g && (
            <p className="mt-2 text-[11px] text-muted">
              Listing model (logistic + quantile regression): Brier skill above this table in <span className="num">{g.years_passed.length}</span> of{" "}
              <span className="num">{g.years_evaluated.length}</span> walk-forward years ({g.years_evaluated[0]}–{g.years_evaluated.at(-1)}).{" "}
              {data.model?.uses_model ? "It passed, so IPO signals use it." : "It did not pass the bar (5 of 7), so IPO signals use this table."}
              {data.model?.blend && (
                <>
                  {" "}Blended with this table ({pct(data.model.blend.lambda)} model, {pct(1 - data.model.blend.lambda)} table, weight fitted on earlier years only) it beat the table in{" "}
                  <span className="num">{data.model.blend.gate.years_passed.length}</span> of <span className="num">{data.model.blend.gate.years_evaluated.length}</span> years
                  (pooled Brier <span className="num">{data.model.blend.pooled.brier.toFixed(3)}</span> vs <span className="num">{data.model.blend.pooled.brier_table.toFixed(3)}</span>, n = <span className="num">{data.model.blend.pooled.n}</span>), but at the minimum and as one of three calibrations tried, so it runs only as a shadow test: logged beside each call for out-of-sample scoring and shown as an &ldquo;experimental comparison&rdquo;, never used for the call.
                </>
              )}
            </p>
          )}
        </>
      ) : null}
    </Card>
  );
}

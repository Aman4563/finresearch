"use client";

import { CartesianGrid, Legend, ReferenceLine, ResponsiveContainer, Scatter, ScatterChart, Tooltip, XAxis, YAxis } from "recharts";

export type RelBin = { n: number; mean_p: number; observed: number; ci: [number, number] | null };

const pct = (x: number) => `${Math.round(x * 100)}%`;

type Pt = { x: number; y: number; n: number; lo: number | null; hi: number | null; series: string };

function Tip({ active, payload }: { active?: boolean; payload?: { payload: Pt }[] }) {
  if (!active || !payload?.length) return null;
  const p = payload[0].payload;
  return (
    <div className="rounded-md border border-border bg-card px-2.5 py-1.5 text-xs shadow-sm">
      <p className="font-medium">{p.series}</p>
      <p className="num">forecast {pct(p.x)} · happened {pct(p.y)}</p>
      <p className="num text-muted">{p.n} days{p.lo != null && p.hi != null ? ` · 95% CI ${pct(p.lo)}–${pct(p.hi)}` : ""}</p>
    </div>
  );
}

/** Reliability diagram (#327): mean forecast vs observed frequency per equal-count bin, out of sample. On the dashed
 * diagonal the forecasts were calibrated. Points are bins, so overlapping days are counted, not independent events. */
export function CalibrationChart({ series }: { series: { name: string; color: string; bins: RelBin[] }[] }) {
  const all = series.flatMap((s) => s.bins.flatMap((b) => [b.mean_p, b.observed]));
  if (!all.length) return <p className="text-xs text-muted">No out-of-sample bins.</p>;
  const lo = Math.max(0, Math.floor((Math.min(...all) - 0.05) * 10) / 10);
  const hi = Math.min(1, Math.ceil((Math.max(...all) + 0.05) * 10) / 10);
  return (
    <div className="h-64 w-full" role="img" aria-label={`Calibration: ${series.map((s) => s.name).join(" and ")}, forecast against observed frequency`}>
      <ResponsiveContainer>
        <ScatterChart margin={{ top: 8, right: 12, bottom: 18, left: 0 }}>
          <CartesianGrid stroke="var(--border)" strokeDasharray="2 4" />
          <XAxis type="number" dataKey="x" domain={[lo, hi]} tickFormatter={pct} tick={{ fontSize: 10, fill: "var(--muted)" }} stroke="var(--border)"
            label={{ value: "forecast probability", position: "insideBottom", offset: -10, fontSize: 10, fill: "var(--muted)" }} />
          <YAxis type="number" dataKey="y" domain={[lo, hi]} tickFormatter={pct} tick={{ fontSize: 10, fill: "var(--muted)" }} stroke="var(--border)" width={40} />
          <ReferenceLine segment={[{ x: lo, y: lo }, { x: hi, y: hi }]} stroke="var(--muted)" strokeDasharray="4 4" />
          <Tooltip content={<Tip />} cursor={{ strokeDasharray: "2 2" }} />
          <Legend wrapperStyle={{ fontSize: 11 }} verticalAlign="top" height={22} />
          {series.map((s) => (
            <Scatter key={s.name} name={s.name} fill={s.color} stroke="var(--card)" strokeWidth={2} isAnimationActive={false}
              data={s.bins.map((b) => ({ x: b.mean_p, y: b.observed, n: b.n, lo: b.ci?.[0] ?? null, hi: b.ci?.[1] ?? null, series: s.name }))} />
          ))}
        </ScatterChart>
      </ResponsiveContainer>
    </div>
  );
}

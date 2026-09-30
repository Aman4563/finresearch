"use client";

// F&O charts on Recharts: the butterfly open-interest chart and an interactive payoff chart with a what-if marker.

import { useId, useMemo } from "react";
import {
  Area, AreaChart, Bar, BarChart, CartesianGrid, ReferenceArea, ReferenceDot, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";

import { fmtINR } from "@/components/charts";

const axis = { tickLine: false, axisLine: false, fontSize: 11 } as const;
const compact = (v: number) => {
  const a = Math.abs(v);
  return a >= 1e7 ? `${(a / 1e7).toFixed(1)}Cr` : a >= 1e5 ? `${(a / 1e5).toFixed(1)}L` : a >= 1e3 ? `${(a / 1e3).toFixed(0)}K` : String(Math.round(a));
};

type OiRow = { strike: number; calls: number; puts: number; callChg: number; putChg: number };

function OiTip({ active, payload, label }: { active?: boolean; payload?: { payload: OiRow }[]; label?: number }) {
  if (!active || !payload?.length) return null;
  const r = payload[0].payload;
  const chg = (x: number) => `${x >= 0 ? "+" : "−"}${compact(x)}`;
  return (
    <div className="rounded-lg border border-border bg-card/95 px-3 py-2 text-xs shadow-pop backdrop-blur">
      <p className="num mb-1 font-semibold">Strike {Number(label).toLocaleString("en-IN")}</p>
      <p className="flex justify-between gap-4"><span className="text-loss">Call OI</span><span className="num">{compact(-r.calls)} <span className="text-muted">({chg(r.callChg)})</span></span></p>
      <p className="flex justify-between gap-4"><span className="text-gain">Put OI</span><span className="num">{compact(r.puts)} <span className="text-muted">({chg(r.putChg)})</span></span></p>
    </div>
  );
}

/** Calls to the left (red, resistance), puts to the right (green, support); spot and max pain marked. */
export function OiButterfly({ rows, spot, maxPain, height = 420 }: { rows: OiRow[]; spot: number; maxPain: number | null; height?: number }) {
  const max = Math.max(1, ...rows.map((r) => Math.max(-r.calls, r.puts)));
  // Recharts category axes can't place a line between categories: mark the strike nearest to spot
  const nearest = rows.reduce((b, r) => (Math.abs(r.strike - spot) < Math.abs(b - spot) ? r.strike : b), rows[0]?.strike ?? spot);
  return (
    <div style={{ height }} className="animate-fade-in">
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={rows} layout="vertical" stackOffset="sign" barCategoryGap={2} margin={{ top: 4, right: 8, bottom: 0, left: 0 }}>
          <CartesianGrid horizontal={false} strokeDasharray="3 3" />
          <XAxis type="number" {...axis} domain={[-max, max]} tickFormatter={compact} />
          <YAxis type="category" dataKey="strike" {...axis} width={52} interval="preserveStartEnd" tickFormatter={(v) => Number(v).toLocaleString("en-IN")} />
          <Tooltip cursor={{ fill: "var(--background-subtle)" }} content={<OiTip />} />
          <ReferenceLine x={0} stroke="var(--border-strong)" />
          <ReferenceLine y={nearest} stroke="var(--accent)" strokeDasharray="4 4" />
          {maxPain != null && rows.some((r) => r.strike === maxPain) && (
            <ReferenceLine y={maxPain} stroke="var(--warn)" strokeDasharray="2 3" />
          )}
          <Bar dataKey="calls" name="Call OI" stackId="oi" fill="var(--loss)" fillOpacity={0.75} radius={[4, 0, 0, 4]} animationDuration={600} />
          <Bar dataKey="puts" name="Put OI" stackId="oi" fill="var(--gain)" fillOpacity={0.75} radius={[0, 4, 4, 0]} animationDuration={600} />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

function PayoffTip({ active, payload, label }: { active?: boolean; payload?: { value?: number }[]; label?: number }) {
  if (!active || !payload?.length) return null;
  const v = Number(payload[0].value);
  return (
    <div className="rounded-lg border border-border bg-card/95 px-3 py-2 text-xs shadow-pop backdrop-blur">
      <p className="text-muted">At expiry, price {Number(label).toLocaleString("en-IN")}</p>
      <p className={`num font-semibold ${v >= 0 ? "text-gain" : "text-loss"}`}>{v >= 0 ? "+" : ""}{fmtINR(v)}</p>
    </div>
  );
}

/** Payoff at expiry: profit green, loss red; spot, breakevens and a movable what-if price (click the chart to move it). */
export function InteractivePayoff({ points, spot, breakevens, what, onPick, band, height = 300 }: {
  points: { spot: number; pnl: number }[]; spot: number; breakevens: number[]; what?: { spot: number; pnl: number } | null;
  onPick?: (s: number) => void; band?: { low: number; high: number } | null; height?: number;
}) {
  const gid = useId().replace(/:/g, "");
  const off = useMemo(() => {
    const max = Math.max(...points.map((p) => p.pnl)), min = Math.min(...points.map((p) => p.pnl));
    return max <= 0 ? 0 : min >= 0 ? 1 : max / (max - min);
  }, [points]);
  return (
    <div style={{ height }} className="animate-fade-in">
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={points} margin={{ top: 18, right: 12, bottom: 0, left: 0 }}
          onClick={(e) => {
            const x = Number((e as { activeLabel?: string | number } | null)?.activeLabel);
            if (onPick && Number.isFinite(x)) onPick(x);
          }}>
          <defs>
            <linearGradient id={`${gid}-fill`} x1="0" y1="0" x2="0" y2="1">
              <stop offset={0} stopColor="var(--gain)" stopOpacity={0.35} />
              <stop offset={off} stopColor="var(--gain)" stopOpacity={0.05} />
              <stop offset={off} stopColor="var(--loss)" stopOpacity={0.05} />
              <stop offset={1} stopColor="var(--loss)" stopOpacity={0.35} />
            </linearGradient>
            <linearGradient id={`${gid}-line`} x1="0" y1="0" x2="0" y2="1">
              <stop offset={off} stopColor="var(--gain)" />
              <stop offset={off} stopColor="var(--loss)" />
            </linearGradient>
          </defs>
          <CartesianGrid strokeDasharray="3 3" vertical={false} />
          {band && (
            <ReferenceArea x1={band.low} x2={band.high} fill="var(--chart-2)" fillOpacity={0.1} stroke="var(--chart-2)" strokeOpacity={0.35} strokeDasharray="3 3"
              ifOverflow="hidden" label={{ value: "±1σ expected move", position: "insideBottom", fill: "var(--chart-2)", fontSize: 10 }} />
          )}
          <XAxis dataKey="spot" type="number" domain={["dataMin", "dataMax"]} {...axis} tickFormatter={(v) => Number(v).toLocaleString("en-IN")} />
          <YAxis {...axis} width={72} tickFormatter={(v) => fmtINR(Number(v)).replace(/\.\d+$/, "")} />
          <Tooltip content={<PayoffTip />} />
          <ReferenceLine y={0} stroke="var(--border-strong)" />
          <ReferenceLine x={spot} stroke="var(--accent)" strokeDasharray="4 4"
            label={{ value: `spot ${spot.toLocaleString("en-IN")}`, position: "top", fill: "var(--accent)", fontSize: 10 }} />
          {breakevens.map((b) => (
            <ReferenceDot key={b} x={b} y={0} r={4} fill="var(--warn)" stroke="var(--card)" strokeWidth={2} />
          ))}
          <Area type="linear" dataKey="pnl" name="P&L at expiry" stroke={`url(#${gid}-line)`} strokeWidth={2} fill={`url(#${gid}-fill)`}
            dot={false} activeDot={{ r: 4 }} animationDuration={500} />
          {what && (
            <>
              <ReferenceLine x={what.spot} stroke="var(--foreground)" strokeOpacity={0.5} />
              <ReferenceDot x={what.spot} y={what.pnl} r={6} fill={what.pnl >= 0 ? "var(--gain)" : "var(--loss)"} stroke="var(--card)" strokeWidth={2} />
            </>
          )}
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}

"use client";

// Charts specific to the market pages. Same conventions as components/charts.tsx: colours from CSS tokens, one
// y-axis, a crosshair / per-bar tooltip, a legend whenever there are two or more series.

import { useId } from "react";
import {
  Area,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ComposedChart,
  Legend,
  Line,
  LineChart,
  ReferenceDot,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { fmtCompactINR, shortDate } from "@/components/charts";
import { inr, pctOf } from "@/components/markets/common";

const axis = { tickLine: false, axisLine: false, fontSize: 11 } as const;

type TipRow = { name?: string; value?: number; color?: string; dataKey?: string; payload?: Record<string, unknown> };

function Tip({ active, payload, title, format }: {
  active?: boolean; payload?: TipRow[]; title: (row: Record<string, unknown>) => string; format: (v: number, key?: string) => string;
}) {
  if (!active || !payload?.length) return null;
  const row = payload[0].payload ?? {};
  return (
    <div className="rounded-lg border border-border bg-card/95 px-3 py-2 text-xs shadow-pop backdrop-blur animate-scale-in">
      <p className="mb-1 font-medium text-muted">{title(row)}</p>
      {payload.map((p) => (
        <p key={p.dataKey} className="flex items-center gap-2">
          <span className="size-2 rounded-full" style={{ background: p.color }} />
          <span className="text-muted">{p.name}</span>
          <span className="num ml-auto pl-3 font-medium">{typeof p.value === "number" ? format(p.value, p.dataKey) : "—"}</span>
        </p>
      ))}
    </div>
  );
}

/** Distribution of rolling returns: bars per return bucket, green above the reference (e.g. 0% or the risk-free
 *  rate), red below; the median is marked. */
export function ReturnHistogram({ bins, median, reference, height = 220 }: {
  bins: { from: number; to: number; count: number }[]; median: number; reference: { value: number; label: string }; height?: number;
}) {
  const total = bins.reduce((a, b) => a + b.count, 0) || 1;
  const data = bins.map((b) => ({ ...b, mid: (b.from + b.to) / 2, share: b.count / total }));
  const nearest = (v: number) => data.reduce((best, d) => (Math.abs(d.mid - v) < Math.abs(best.mid - v) ? d : best), data[0])?.mid;
  return (
    <div style={{ height }} className="animate-fade-in">
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={data} margin={{ top: 18, right: 8, bottom: 0, left: 0 }} barCategoryGap={2}>
          <CartesianGrid vertical={false} strokeDasharray="3 3" />
          <XAxis dataKey="mid" {...axis} tickFormatter={(v) => pctOf(Number(v), 0)} minTickGap={24} />
          <YAxis {...axis} width={40} tickFormatter={(v) => `${Math.round(Number(v) * 100)}%`} />
          <Tooltip
            cursor={{ fill: "var(--background-subtle)" }}
            content={<Tip title={(r) => `${pctOf(Number(r.from), 1)} to ${pctOf(Number(r.to), 1)} a year`}
              format={(v) => `${(v * 100).toFixed(1)}% of windows`} />}
          />
          <ReferenceLine x={nearest(reference.value)} stroke="var(--warn)" strokeDasharray="4 4"
            label={{ value: reference.label, position: "top", fill: "var(--warn)", fontSize: 10 }} />
          <ReferenceLine x={nearest(median)} stroke="var(--foreground)" strokeOpacity={0.45} />
          <Bar dataKey="share" name="Windows" radius={[4, 4, 0, 0]} animationDuration={700}>
            {data.map((d) => (
              <Cell key={d.mid} fill={d.mid >= reference.value ? "var(--gain)" : "var(--loss)"} fillOpacity={0.85} />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

/** A bond's remaining cash flows: coupon and principal stacked per payment date. */
export function CashFlowChart({ flows, height = 240 }: {
  flows: { date: string; coupon: number; principal: number; coupon_after_tax: number }[]; height?: number;
}) {
  return (
    <div style={{ height }} className="animate-fade-in">
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={flows} margin={{ top: 6, right: 8, bottom: 0, left: 0 }}>
          <CartesianGrid vertical={false} strokeDasharray="3 3" />
          <XAxis dataKey="date" {...axis} tickFormatter={(v) => shortDate(String(v))} minTickGap={16} />
          <YAxis {...axis} width={64} tickFormatter={(v) => fmtCompactINR(Number(v))} />
          <Tooltip cursor={{ fill: "var(--background-subtle)" }}
            content={<Tip title={(r) => `Paid on ${shortDate(String(r.date))}`} format={(v) => inr(v)} />} />
          <Legend iconType="circle" iconSize={8} wrapperStyle={{ fontSize: 11 }} />
          <Bar dataKey="coupon" name="Coupon (interest)" stackId="cf" fill="var(--chart-1)" maxBarSize={36} animationDuration={700} />
          <Bar dataKey="principal" name="Principal back" stackId="cf" fill="var(--chart-2)" radius={[4, 4, 0, 0]} maxBarSize={36}
            animationDuration={700} />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

/** Price against yield for a bond: the curve, today's point, and the what-if point from the slider. */
export function PriceYieldChart({ curve, ytm, price, shifted, height = 260 }: {
  curve: { yield: number; dirty: number }[]; ytm: number; price: number; shifted?: { yield: number; dirty: number } | null; height?: number;
}) {
  return (
    <div style={{ height }} className="animate-fade-in">
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={curve} margin={{ top: 14, right: 12, bottom: 0, left: 0 }}>
          <CartesianGrid strokeDasharray="3 3" vertical={false} />
          <XAxis dataKey="yield" type="number" domain={["dataMin", "dataMax"]} {...axis} tickFormatter={(v) => pctOf(Number(v), 1)} />
          <YAxis {...axis} width={70} domain={["auto", "auto"]} tickFormatter={(v) => inr(Number(v), 0)} />
          <Tooltip content={<Tip title={(r) => `At a yield of ${pctOf(Number(r.yield))}`} format={(v) => inr(v)} />} />
          <ReferenceLine x={ytm} stroke="var(--muted)" strokeDasharray="4 4" />
          <Line type="monotone" dataKey="dirty" name="Price (incl. accrued)" stroke="var(--chart-2)" strokeWidth={2} dot={false}
            activeDot={{ r: 4 }} animationDuration={700} />
          <ReferenceDot x={ytm} y={price} r={6} fill="var(--brand)" stroke="var(--card)" strokeWidth={2}
            label={{ value: "today", position: "top", fill: "var(--muted)", fontSize: 10 }} />
          {shifted && Math.abs(shifted.yield - ytm) > 1e-6 && (
            <ReferenceDot x={shifted.yield} y={shifted.dirty} r={6} fill={shifted.yield > ytm ? "var(--loss)" : "var(--gain)"}
              stroke="var(--card)" strokeWidth={2} label={{ value: "what if", position: "top", fill: "var(--muted)", fontSize: 10 }} />
          )}
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

/** SIP value against money put in, with the same total invested as one lump sum on day one. */
export function SipChart({ path, height = 260 }: {
  path: { date: string; invested: number; value: number; lump_sum: number | null }[]; height?: number;
}) {
  const gid = useId().replace(/:/g, "");
  return (
    <div style={{ height }} className="animate-fade-in">
      <ResponsiveContainer width="100%" height="100%">
        <ComposedChart data={path} margin={{ top: 6, right: 8, bottom: 0, left: 0 }}>
          <defs>
            <linearGradient id={gid} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="var(--chart-1)" stopOpacity={0.3} />
              <stop offset="100%" stopColor="var(--chart-1)" stopOpacity={0} />
            </linearGradient>
          </defs>
          <CartesianGrid vertical={false} strokeDasharray="3 3" />
          <XAxis dataKey="date" {...axis} tickFormatter={(v) => shortDate(String(v))} minTickGap={40} />
          <YAxis {...axis} width={70} tickFormatter={(v) => fmtCompactINR(Number(v))} />
          <Tooltip content={<Tip title={(r) => shortDate(String(r.date))} format={(v) => inr(v, 0)} />} />
          <Legend iconType="circle" iconSize={8} wrapperStyle={{ fontSize: 11 }} />
          <Area type="monotone" dataKey="value" name="SIP value" stroke="var(--chart-1)" strokeWidth={2} fill={`url(#${gid})`}
            dot={false} activeDot={{ r: 4 }} animationDuration={700} />
          <Line type="stepAfter" dataKey="invested" name="Money put in" stroke="var(--muted)" strokeWidth={1.5} dot={false}
            animationDuration={700} />
          <Line type="monotone" dataKey="lump_sum" name="Same total as lump sum" stroke="var(--chart-2)" strokeWidth={2}
            strokeDasharray="5 4" dot={false} animationDuration={700} />
        </ComposedChart>
      </ResponsiveContainer>
    </div>
  );
}

/** Quarterly revenue and profit (₹ crore) side by side. */
export function ResultsChart({ rows, height = 240 }: {
  rows: { label: string; title?: string; revenue: number | null; profit: number | null }[]; height?: number;
}) {
  return (
    <div style={{ height }} className="animate-fade-in">
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={rows} margin={{ top: 6, right: 8, bottom: 0, left: 0 }} barGap={2}>
          <CartesianGrid vertical={false} strokeDasharray="3 3" />
          <XAxis dataKey="label" {...axis} />
          <YAxis {...axis} width={70} tickFormatter={(v) => `₹${Number(v).toLocaleString("en-IN", { maximumFractionDigits: 0 })}`} />
          <Tooltip cursor={{ fill: "var(--background-subtle)" }}
            content={<Tip title={(r) => String(r.title ?? r.label)}
              format={(v) => `₹${v.toLocaleString("en-IN", { maximumFractionDigits: 0 })} Cr`} />} />
          <Legend iconType="circle" iconSize={8} wrapperStyle={{ fontSize: 11 }} />
          <Bar dataKey="revenue" name="Revenue (₹ Cr)" fill="var(--chart-1)" radius={[4, 4, 0, 0]} maxBarSize={28} animationDuration={700} />
          <Bar dataKey="profit" name="Net profit (₹ Cr)" fill="var(--chart-2)" radius={[4, 4, 0, 0]} maxBarSize={28} animationDuration={700} />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

/** Net margin (profit / revenue) per quarter, on its own small chart under the revenue/profit bars (one axis each). */
export function MarginChart({ rows, height = 120 }: {
  rows: { label: string; title?: string; margin: number | null }[]; height?: number;
}) {
  return (
    <div style={{ height }} className="animate-fade-in">
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={rows} margin={{ top: 8, right: 12, bottom: 0, left: 0 }}>
          <CartesianGrid vertical={false} strokeDasharray="3 3" />
          <XAxis dataKey="label" {...axis} />
          <YAxis {...axis} width={70} domain={["auto", "auto"]} tickFormatter={(v) => pctOf(Number(v), 0)} />
          <Tooltip content={<Tip title={(r) => String(r.title ?? r.label)} format={(v) => pctOf(v, 1)} />} />
          <Line type="monotone" dataKey="margin" name="Net margin" stroke="var(--chart-3)" strokeWidth={2}
            dot={{ r: 4, strokeWidth: 2, fill: "var(--card)" }} activeDot={{ r: 5 }} connectNulls animationDuration={700} />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

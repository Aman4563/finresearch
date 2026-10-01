"use client";

// Themed, interactive charts (Recharts). Colours come from the CSS tokens (--chart-1..6, --gain, --loss), so charts
// follow light/dark automatically. Every chart has a crosshair tooltip; time series can have a range selector.

import { type ReactNode, useId, useMemo, useState } from "react";
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  Line,
  LineChart,
  Pie,
  PieChart,
  ReferenceDot,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { Segmented, cx } from "@/components/ui";

export const CHART_COLORS = ["var(--chart-1)", "var(--chart-2)", "var(--chart-3)", "var(--chart-4)", "var(--chart-5)", "var(--chart-6)"];

export type Series = { key: string; label: string; color?: string; dashed?: boolean };
type Fmt = (v: number) => string;

export const fmtINR: Fmt = (v) => `₹${v.toLocaleString("en-IN", { maximumFractionDigits: 2 })}`;
export const fmtCompactINR: Fmt = (v) => {
  const a = Math.abs(v);
  // a no-break space: an axis tick or a sentence never splits "₹60.00" from its "L"
  if (a >= 1e7) return `₹${(v / 1e7).toFixed(2)}\u00a0Cr`;
  if (a >= 1e5) return `₹${(v / 1e5).toFixed(2)}\u00a0L`;
  return `₹${v.toLocaleString("en-IN", { maximumFractionDigits: 0 })}`;
};
export const fmtPct: Fmt = (v) => `${v.toFixed(2)}%`;
export const fmtTimes: Fmt = (v) => `${v.toFixed(2)}x`;
const fmtDefault: Fmt = (v) => v.toLocaleString("en-IN", { maximumFractionDigits: 2 });

function ChartTooltip({ active, payload, label, format, labelFormat }: {
  active?: boolean; payload?: { name?: string; value?: number; color?: string; dataKey?: string }[]; label?: string | number;
  format: Fmt; labelFormat?: (l: string | number) => string;
}) {
  if (!active || !payload?.length) return null;
  return (
    <div className="rounded-lg border border-border bg-card/95 px-3 py-2 text-xs shadow-pop backdrop-blur animate-scale-in">
      {label != null && <p className="mb-1 font-medium text-muted">{labelFormat ? labelFormat(label) : label}</p>}
      {payload.map((p) => (
        <p key={p.dataKey} className="flex items-center gap-2">
          <span className="size-2 rounded-full" style={{ background: p.color }} />
          <span className="text-muted">{p.name}</span>
          <span className="num ml-auto pl-3 font-medium">{typeof p.value === "number" ? format(p.value) : "—"}</span>
        </p>
      ))}
    </div>
  );
}

const axis = { tickLine: false, axisLine: false, fontSize: 11 } as const;

export function shortDate(iso: string | number) {
  const d = new Date(typeof iso === "number" ? iso : `${String(iso).slice(0, 10)}T00:00:00Z`);
  return Number.isNaN(d.getTime()) ? String(iso) : d.toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "2-digit", timeZone: "UTC" });
}

export type Range = "1M" | "3M" | "6M" | "1Y" | "3Y" | "5Y" | "ALL";
const RANGE_DAYS: Record<Range, number> = { "1M": 31, "3M": 92, "6M": 183, "1Y": 366, "3Y": 1096, "5Y": 1827, ALL: Infinity };

/** Line/area time series. `data` rows have an ISO date under `x` plus one numeric field per series. */
export function TimeSeriesChart({
  data, x = "date", series, height = 260, format = fmtDefault, ranges, defaultRange = "1Y", area = true,
  references, showChange = true, xFormat = shortDate, yDomain, curve = "monotone", dots = false, onPointClick,
}: {
  data: Record<string, unknown>[]; x?: string; series: Series[]; height?: number; format?: Fmt; ranges?: Range[];
  defaultRange?: Range; area?: boolean; references?: { y: number; label: string; tone?: "gain" | "loss" | "muted" }[];
  showChange?: boolean; xFormat?: (v: string | number) => string; yDomain?: [number | "auto" | "dataMin", number | "auto" | "dataMax"];
  /** "linear" for sparse observations (no invented shape between points); `dots` marks each observation. */
  curve?: "monotone" | "linear"; dots?: boolean;
  /** Click on the chart: called with the row under the crosshair (e.g. to show that point's sources). */
  onPointClick?: (row: Record<string, unknown>, index: number) => void;
}) {
  const gid = useId().replace(/:/g, "");
  const [range, setRange] = useState<Range>(ranges ? (ranges.includes(defaultRange) ? defaultRange : ranges[ranges.length - 1]) : "ALL");
  const rows = useMemo(() => {
    if (!ranges || range === "ALL" || !data.length) return data;
    const last = new Date(String(data[data.length - 1][x]).slice(0, 10)).getTime();
    const cutoff = last - RANGE_DAYS[range] * 86400000;
    return data.filter((r) => new Date(String(r[x]).slice(0, 10)).getTime() >= cutoff);
  }, [data, range, ranges, x]);

  const first = series[0];
  const change = useMemo(() => {
    if (!showChange || rows.length < 2) return null;
    const a = Number(rows[0][first.key]), b = Number(rows[rows.length - 1][first.key]);
    return Number.isFinite(a) && Number.isFinite(b) && a !== 0 ? ((b - a) / Math.abs(a)) * 100 : null;
  }, [rows, first.key, showChange]);

  const up = (change ?? 0) >= 0;
  const click = onPointClick
    ? (st: { activeIndex?: unknown }) => {
        const i = Number(st?.activeIndex);
        if (Number.isInteger(i) && rows[i]) onPointClick(rows[i], i);
      }
    : undefined;
  const color = (s: Series, i: number) => s.color ?? (series.length === 1 ? (up ? "var(--gain)" : "var(--loss)") : CHART_COLORS[i % 6]);

  return (
    <div>
      {(ranges || change != null) && (
        <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
          {change != null ? (
            <p className="text-xs text-muted">
              {range === "ALL" ? "Whole period" : range}:{" "}
              <span className={cx("num font-semibold", up ? "text-gain" : "text-loss")}>
                {up ? "+" : ""}
                {change.toFixed(2)}%
              </span>
            </p>
          ) : <span />}
          {ranges && <Segmented value={range} onChange={setRange} options={ranges.map((r) => ({ value: r, label: r }))} />}
        </div>
      )}
      <div style={{ height }} className="animate-fade-in">
        <ResponsiveContainer width="100%" height="100%">
          {area ? (
            <AreaChart data={rows} margin={{ top: 6, right: 8, bottom: 0, left: 0 }} onClick={click} style={click ? { cursor: "pointer" } : undefined}>
              <defs>
                {series.map((s, i) => (
                  <linearGradient key={s.key} id={`${gid}-${i}`} x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor={color(s, i)} stopOpacity={0.28} />
                    <stop offset="100%" stopColor={color(s, i)} stopOpacity={0} />
                  </linearGradient>
                ))}
              </defs>
              <CartesianGrid vertical={false} strokeDasharray="3 3" />
              <XAxis dataKey={x} {...axis} tickFormatter={xFormat} minTickGap={40} />
              <YAxis {...axis} width={64} tickFormatter={format} domain={yDomain ?? ["auto", "auto"]} />
              <Tooltip content={<ChartTooltip format={format} labelFormat={xFormat} />} />
              {references?.map((r) => (
                <ReferenceLine key={r.label} y={r.y} stroke={r.tone === "gain" ? "var(--gain)" : r.tone === "loss" ? "var(--loss)" : "var(--muted)"}
                  strokeDasharray="4 4" label={{ value: r.label, position: "insideTopLeft", fill: "var(--muted)", fontSize: 10 }} />
              ))}
              {series.map((s, i) => (
                <Area key={s.key} type={curve} dataKey={s.key} name={s.label} stroke={color(s, i)} strokeWidth={2}
                  strokeDasharray={s.dashed ? "5 4" : undefined} fill={`url(#${gid}-${i})`}
                  dot={dots ? { r: 3, strokeWidth: 0, fill: color(s, i) } : false}
                  activeDot={{ r: 4, strokeWidth: 2, stroke: "var(--card)" }} connectNulls animationDuration={700} />
              ))}
            </AreaChart>
          ) : (
            <LineChart data={rows} margin={{ top: 6, right: 8, bottom: 0, left: 0 }} onClick={click} style={click ? { cursor: "pointer" } : undefined}>
              <CartesianGrid vertical={false} strokeDasharray="3 3" />
              <XAxis dataKey={x} {...axis} tickFormatter={xFormat} minTickGap={40} />
              <YAxis {...axis} width={64} tickFormatter={format} domain={yDomain ?? ["auto", "auto"]} />
              <Tooltip content={<ChartTooltip format={format} labelFormat={xFormat} />} />
              {series.length > 1 && <Legend iconType="circle" iconSize={8} wrapperStyle={{ fontSize: 11 }} />}
              {references?.map((r) => (
                <ReferenceLine key={r.label} y={r.y} stroke="var(--muted)" strokeDasharray="4 4"
                  label={{ value: r.label, position: "insideTopLeft", fill: "var(--muted)", fontSize: 10 }} />
              ))}
              {series.map((s, i) => (
                <Line key={s.key} type={curve} dataKey={s.key} name={s.label} stroke={color(s, i)} strokeWidth={2}
                  strokeDasharray={s.dashed ? "5 4" : undefined}
                  dot={dots ? { r: 3, strokeWidth: 0, fill: color(s, i) } : false} activeDot={{ r: 4 }} connectNulls animationDuration={700} />
              ))}
            </LineChart>
          )}
        </ResponsiveContainer>
      </div>
    </div>
  );
}

/** Bars per category, e.g. subscription times per investor category. `reference` draws a line (1x = fully subscribed). */
export function BarsChart({ data, x, series, height = 240, format = fmtDefault, layout = "horizontal", reference, colorBy, onBarClick,
  labelWidth = 96, stacked, tickFormat }: {
  data: Record<string, unknown>[]; x: string; series: Series[]; height?: number; format?: Fmt;
  layout?: "horizontal" | "vertical"; reference?: { value: number; label: string }; colorBy?: (row: Record<string, unknown>) => string;
  /** Click on a bar: the row and the series key (e.g. to show that bar's sources). */
  onBarClick?: (row: Record<string, unknown>, seriesKey: string) => void;
  /** Width of the category axis in vertical layout (long names). */ labelWidth?: number;
  stacked?: boolean;
  /** Value-axis tick labels, when they should be shorter than the tooltip's `format`. */ tickFormat?: Fmt;
}) {
  const vertical = layout === "vertical";
  return (
    <div style={{ height }} className="animate-fade-in">
      <ResponsiveContainer width="100%" height="100%">
        {/* a vertical chart's reference label sits above the plot: give it room, or "1x" is cut to "…" */}
        <BarChart data={data} layout={vertical ? "vertical" : "horizontal"} margin={{ top: vertical && reference ? 18 : 6, right: 12, bottom: 0, left: 0 }}>
          <CartesianGrid vertical={vertical} horizontal={!vertical} strokeDasharray="3 3" />
          {vertical ? (
            <>
              <XAxis type="number" {...axis} tickFormatter={tickFormat ?? format} />
              <YAxis type="category" dataKey={x} {...axis} width={labelWidth} />
            </>
          ) : (
            <>
              <XAxis dataKey={x} {...axis} />
              <YAxis {...axis} width={56} tickFormatter={tickFormat ?? format} />
            </>
          )}
          <Tooltip cursor={{ fill: "var(--background-subtle)" }} content={<ChartTooltip format={format} />} />
          {series.length > 1 && <Legend iconType="circle" iconSize={8} wrapperStyle={{ fontSize: 11 }} />}
          {reference && (
            <ReferenceLine {...(vertical ? { x: reference.value } : { y: reference.value })} stroke="var(--warn)" strokeDasharray="4 4"
              label={{ value: reference.label, position: vertical ? "top" : "insideTopRight", fill: "var(--warn)", fontSize: 10 }} />
          )}
          {series.map((s, i) => (
            <Bar key={s.key} dataKey={s.key} name={s.label} fill={s.color ?? CHART_COLORS[i % 6]} radius={vertical ? [0, 4, 4, 0] : [4, 4, 0, 0]}
              maxBarSize={36} animationDuration={700} stackId={stacked ? "s" : undefined}
              onClick={onBarClick ? (_d: unknown, j: number) => data[j] && onBarClick(data[j], s.key) : undefined}
              style={onBarClick ? { cursor: "pointer" } : undefined}>
              {colorBy && data.map((row, j) => <Cell key={j} fill={colorBy(row)} />)}
            </Bar>
          ))}
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

/** Donut with a centre label; slices highlight on hover. */
export function DonutChart({ data, height = 200, center, format = fmtDefault, onSliceClick }: {
  data: { name: string; value: number; color?: string }[]; height?: number; center?: ReactNode; format?: Fmt;
  /** Click on a slice or its legend row. */ onSliceClick?: (index: number) => void;
}) {
  const [active, setActive] = useState<number | null>(null);
  const total = data.reduce((a, d) => a + d.value, 0);
  return (
    <div className="flex items-center gap-4">
      <div className="relative shrink-0" style={{ height, width: height }}>
        <ResponsiveContainer width="100%" height="100%">
          <PieChart>
            <Pie data={data} dataKey="value" nameKey="name" innerRadius="68%" outerRadius="92%" paddingAngle={2} stroke="none"
              onMouseEnter={(_, i) => setActive(i)} onMouseLeave={() => setActive(null)} animationDuration={700}
              onClick={onSliceClick ? (_: unknown, i: number) => onSliceClick(i) : undefined} style={onSliceClick ? { cursor: "pointer" } : undefined}>
              {data.map((d, i) => (
                <Cell key={d.name} fill={d.color ?? CHART_COLORS[i % 6]} opacity={active == null || active === i ? 1 : 0.35} />
              ))}
            </Pie>
          </PieChart>
        </ResponsiveContainer>
        <div className="pointer-events-none absolute inset-0 grid place-items-center text-center">
          {active != null ? (
            <div>
              <p className="num text-lg font-semibold">{format(data[active].value)}</p>
              <p className="text-[11px] text-muted">{data[active].name}</p>
            </div>
          ) : center}
        </div>
      </div>
      <ul className="min-w-0 flex-1 space-y-1.5 text-xs">
        {data.map((d, i) => (
          <li key={d.name} className={cx("flex items-center gap-2 transition-opacity", active != null && active !== i && "opacity-40", onSliceClick && "cursor-pointer")}
            onMouseEnter={() => setActive(i)} onMouseLeave={() => setActive(null)} onClick={onSliceClick ? () => onSliceClick(i) : undefined}>
            <span className="size-2.5 shrink-0 rounded-sm" style={{ background: d.color ?? CHART_COLORS[i % 6] }} />
            <span className="truncate text-muted">{d.name}</span>
            <span className="num ml-auto font-medium">{format(d.value)}</span>
            <span className="num w-10 text-right text-muted">{total ? Math.round((d.value / total) * 100) : 0}%</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

/** Option-strategy payoff at expiry: profit shaded green, loss red, with breakevens and the current spot. */
export function PayoffChart({ points, spot, breakevens = [], height = 280, format = fmtINR }: {
  points: { spot: number; pnl: number }[]; spot?: number; breakevens?: number[]; height?: number; format?: Fmt;
}) {
  const gid = useId().replace(/:/g, "");
  const { off } = useMemo(() => {
    const max = Math.max(...points.map((p) => p.pnl)), min = Math.min(...points.map((p) => p.pnl));
    return { off: max <= 0 ? 0 : min >= 0 ? 1 : max / (max - min) };
  }, [points]);
  return (
    <div style={{ height }} className="animate-fade-in">
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={points} margin={{ top: 10, right: 12, bottom: 0, left: 0 }}>
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
          <XAxis dataKey="spot" type="number" domain={["dataMin", "dataMax"]} {...axis} tickFormatter={(v) => v.toLocaleString("en-IN")} />
          <YAxis {...axis} width={72} tickFormatter={format} />
          <Tooltip content={<ChartTooltip format={format} labelFormat={(l) => `At expiry, spot ${Number(l).toLocaleString("en-IN")}`} />} />
          <ReferenceLine y={0} stroke="var(--border-strong)" />
          {spot != null && (
            <ReferenceLine x={spot} stroke="var(--accent)" strokeDasharray="4 4"
              label={{ value: `Spot ${spot.toLocaleString("en-IN")}`, position: "top", fill: "var(--accent)", fontSize: 10 }} />
          )}
          {breakevens.map((b) => (
            <ReferenceDot key={b} x={b} y={0} r={4} fill="var(--warn)" stroke="var(--card)" strokeWidth={2} />
          ))}
          <Area type="linear" dataKey="pnl" name="P&L at expiry" stroke={`url(#${gid}-line)`} strokeWidth={2} fill={`url(#${gid}-fill)`}
            dot={false} activeDot={{ r: 4 }} animationDuration={700} />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}

/** Tiny inline trend line for tables and tiles (plain SVG, no axes). */
export function Sparkline({ values, width = 96, height = 28, className }: { values: number[]; width?: number; height?: number; className?: string }) {
  const gid = useId().replace(/:/g, "");
  if (values.length < 2) return null;
  const min = Math.min(...values), max = Math.max(...values), span = max - min || 1;
  const pts = values.map((v, i) => [(i / (values.length - 1)) * width, height - 2 - ((v - min) / span) * (height - 4)]);
  const d = pts.map((p, i) => `${i ? "L" : "M"}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(" ");
  const up = values[values.length - 1] >= values[0];
  const c = up ? "var(--gain)" : "var(--loss)";
  return (
    <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} className={className} aria-hidden>
      <defs>
        <linearGradient id={gid} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={c} stopOpacity={0.25} />
          <stop offset="100%" stopColor={c} stopOpacity={0} />
        </linearGradient>
      </defs>
      <path d={`${d} L${width},${height} L0,${height} Z`} fill={`url(#${gid})`} />
      <path d={d} fill="none" stroke={c} strokeWidth={1.5} pathLength={1} strokeDasharray={1} className="animate-draw" />
    </svg>
  );
}

/** A semicircle gauge, e.g. how much of the plan window is used. */
export function Gauge({ value, max = 1, label, format = (v: number) => `${Math.round(v * 100)}%`, size = 140 }: {
  value: number | null | undefined; max?: number; label?: ReactNode; format?: Fmt; size?: number;
}) {
  const ratio = value == null ? 0 : Math.max(0, Math.min(1, value / max));
  const r = size / 2 - 10, c = Math.PI * r;
  const tone = ratio >= 0.85 ? "var(--loss)" : ratio >= 0.6 ? "var(--warn)" : "var(--brand)";
  return (
    <div className="flex flex-col items-center">
      <svg width={size} height={size / 2 + 12} viewBox={`0 0 ${size} ${size / 2 + 12}`} aria-hidden>
        <path d={`M10,${size / 2} A${r},${r} 0 0 1 ${size - 10},${size / 2}`} fill="none" stroke="var(--background-subtle)" strokeWidth={10} strokeLinecap="round" />
        <path d={`M10,${size / 2} A${r},${r} 0 0 1 ${size - 10},${size / 2}`} fill="none" stroke={tone} strokeWidth={10} strokeLinecap="round"
          strokeDasharray={`${c * ratio} ${c}`} style={{ transition: "stroke-dasharray 0.8s cubic-bezier(0.22,1,0.36,1)" }} />
      </svg>
      <p className="num -mt-7 text-xl font-semibold">{value == null ? "—" : format(value)}</p>
      {label && <p className="mt-0.5 text-xs text-muted">{label}</p>}
    </div>
  );
}

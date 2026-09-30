"use client";

// Price chart with time frames: a range (1D, 5D intraday; 1M…5Y daily) and a candle interval (1m…1h intraday, 1D
// daily), line or candles, a volume strip where the source has volume, a crosshair tooltip with OHLC, and an honest
// freshness line (source, as-of, measured delay; EOD for daily bars). Recharts has no candlestick mark, so a candle
// is a Bar spanning [low, high] with a custom shape that draws the wick and the open→close body.

import { CandlestickChart, LineChart as LineIcon, SlidersHorizontal } from "lucide-react";
import Link from "next/link";
import { useEffect, useMemo } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  ComposedChart,
  Line,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { shortDate } from "@/components/charts";
import { Freshness, useLive } from "@/components/live";
import { signedPct, toneOf } from "@/components/markets/common";
import { EmptyState, ErrorNote, Segmented, Skeleton, cx } from "@/components/ui";
import {
  type ChartDefaults, type ChartInterval, type ChartRange, DAILY_RANGES, INTRADAY_RANGES, RANGE_DAYS, intervalsFor, useChartChoice,
} from "@/lib/timeframes";

export type Ohlc = { t: string; o: number | null; h: number | null; l: number | null; c: number | null; v?: number | null; partial?: boolean };

export type IntradayResponse = {
  symbol: string;
  kind: "equity" | "index";
  interval: string;
  days_requested: number;
  sessions: { day: string; prev_close: number | null; samples: number; complete: boolean }[];
  candles: { t: string; o: number; h: number; l: number; c: number; n: number; v?: number; partial?: boolean }[];
  prev_close: number | null;
  last: number | null;
  change: number | null;
  change_pct: number | null;
  as_of: string | null;
  fetched_at: string | null;
  delay_s: number | null;
  live: boolean;
  refresh_s: number;
  source: string | null;
  source_label: string;
  exchange?: "NSE" | "BSE";
  has_volume: boolean;
  notes: string[];
};

type Fmt = (v: number) => string;
const axis = { tickLine: false, axisLine: false, fontSize: 11 } as const;

const hhmm = (iso: string) => new Date(iso).toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", hour12: false, timeZone: "Asia/Kolkata" });
const dayMon = (iso: string) => new Date(iso).toLocaleDateString("en-IN", { day: "numeric", month: "short", timeZone: "Asia/Kolkata" });
const compact = (v: number) => (v >= 1e7 ? `${(v / 1e7).toFixed(1)} Cr` : v >= 1e5 ? `${(v / 1e5).toFixed(1)} L` : v >= 1e3 ? `${(v / 1e3).toFixed(1)}k` : String(v));

type CandleShapeProps = { x?: number; y?: number; width?: number; height?: number; payload?: Ohlc };

function CandleShape({ x = 0, y = 0, width = 0, height = 0, payload }: CandleShapeProps) {
  if (!payload || payload.o == null || payload.c == null || payload.h == null || payload.l == null) return null;
  const { o, c, h, l } = payload;
  const up = c >= o;
  const color = up ? "var(--gain)" : "var(--loss)";
  const px = (v: number) => (h === l ? y : y + ((h - v) / (h - l)) * height);
  const cx0 = x + width / 2;
  const bw = Math.max(1, Math.min(width * 0.7, 12));
  const top = px(Math.max(o, c));
  const bh = Math.max(1, px(Math.min(o, c)) - top);
  return (
    <g opacity={payload.partial ? 0.55 : 1}>
      <line x1={cx0} x2={cx0} y1={y} y2={y + Math.max(height, 1)} stroke={color} strokeWidth={1} />
      <rect x={cx0 - bw / 2} y={top} width={bw} height={bh} rx={1} fill={up ? "var(--card)" : color} stroke={color} strokeWidth={1} />
    </g>
  );
}

function OhlcTooltip({ active, payload, format, label, labelFormat }: {
  active?: boolean; payload?: { payload?: Ohlc & { range?: unknown } }[]; format: Fmt; label?: string; labelFormat: (s: string) => string;
}) {
  const row = payload?.[0]?.payload;
  if (!active || !row) return null;
  const up = (row.c ?? 0) >= (row.o ?? 0);
  const items: [string, number | null | undefined][] = [["Open", row.o], ["High", row.h], ["Low", row.l], ["Close", row.c]];
  return (
    <div className="rounded-lg border border-border bg-card/95 px-3 py-2 text-xs shadow-pop backdrop-blur">
      <p className="mb-1 font-medium text-muted">{labelFormat(String(label ?? row.t))}{row.partial ? " · still forming" : ""}</p>
      <div className="grid grid-cols-[auto_auto] gap-x-4 gap-y-0.5">
        {items.map(([k, v]) => (
          <span key={k} className="contents">
            <span className="text-muted">{k}</span>
            <span className={cx("num text-right font-medium", k === "Close" && (up ? "text-gain" : "text-loss"))}>{v == null ? "—" : format(v)}</span>
          </span>
        ))}
        {row.v != null && (
          <span className="contents">
            <span className="text-muted">Volume</span>
            <span className="num text-right font-medium">{row.v.toLocaleString("en-IN")}</span>
          </span>
        )}
      </div>
    </div>
  );
}

function niceStep(raw: number) {
  const p = 10 ** Math.floor(Math.log10(raw));
  const f = raw / p;
  return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10) * p;
}

/** OHLC rows as a line (closes) or candles, with an optional volume strip (its own scale, below the price). */
export function CandleChart({ rows, type, format, xFormat, height = 300, reference, syncId }: {
  rows: Ohlc[]; type: "line" | "candle"; format: Fmt; xFormat: (iso: string) => string; height?: number;
  reference?: { y: number; label: string }; syncId?: string;
}) {
  const data = useMemo(() => rows.map((r) => ({ ...r, range: r.l != null && r.h != null ? [r.l, r.h] : null })), [rows]);
  const hasVolume = rows.some((r) => r.v != null && r.v > 0);
  // a "nice" price axis that holds every high/low (and the reference line)
  const lo = Math.min(...rows.map((r) => r.l ?? r.c ?? Infinity), reference?.y ?? Infinity);
  const hi = Math.max(...rows.map((r) => r.h ?? r.c ?? -Infinity), reference?.y ?? -Infinity);
  const step = niceStep((hi - lo) / 5 || Math.abs(hi) * 0.01 || 1);
  const room = (hi - lo) * 0.04; // keep marks and the reference label off the axis edges
  const domain: [number, number] = [Math.floor((lo - room) / step) * step, Math.ceil((hi + room) / step) * step];
  const ticks = Array.from({ length: Math.round((domain[1] - domain[0]) / step) + 1 }, (_, i) => +(domain[0] + i * step).toFixed(6));
  const up = rows.length > 1 && (rows[rows.length - 1].c ?? 0) >= (rows[0].o ?? rows[0].c ?? 0);
  const tooltip = <OhlcTooltip format={format} labelFormat={xFormat} />;
  return (
    <div className="animate-fade-in">
      <div style={{ height }}>
        <ResponsiveContainer width="100%" height="100%">
          <ComposedChart data={data} syncId={syncId} margin={{ top: 6, right: 8, bottom: 0, left: 0 }} barCategoryGap={type === "candle" ? "20%" : 0}>
            <CartesianGrid vertical={false} strokeDasharray="3 3" />
            <XAxis dataKey="t" {...axis} tickFormatter={xFormat} minTickGap={48} hide={hasVolume} />
            <YAxis {...axis} width={64} domain={domain} ticks={ticks} tickFormatter={format} />
            <Tooltip content={tooltip} cursor={{ stroke: "var(--muted)", strokeDasharray: "3 3" }} isAnimationActive={false} />
            {reference && (
              <ReferenceLine y={reference.y} stroke="var(--muted)" strokeDasharray="4 4"
                label={{ value: reference.label, position: "insideBottomLeft", fill: "var(--muted)", fontSize: 10 }} />
            )}
            {type === "candle" ? (
              <Bar dataKey="range" shape={<CandleShape />} isAnimationActive={false} />
            ) : (
              <Line dataKey="c" type="linear" stroke={up ? "var(--gain)" : "var(--loss)"} strokeWidth={2} dot={false}
                activeDot={{ r: 4, strokeWidth: 2, stroke: "var(--card)" }} isAnimationActive={false} connectNulls />
            )}
          </ComposedChart>
        </ResponsiveContainer>
      </div>
      {hasVolume && (
        <div style={{ height: 72 }} aria-label="Volume">
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={data} syncId={syncId} margin={{ top: 4, right: 8, bottom: 0, left: 0 }} barCategoryGap="20%">
              <XAxis dataKey="t" {...axis} tickFormatter={xFormat} minTickGap={48} />
              <YAxis {...axis} width={64} tickFormatter={compact} tickCount={2} />
              <Tooltip content={tooltip} cursor={{ fill: "var(--background-subtle)" }} isAnimationActive={false} />
              <Bar dataKey="v" name="Volume" fill="var(--muted)" fillOpacity={0.45} radius={[2, 2, 0, 0]} isAnimationActive={false} />
            </BarChart>
          </ResponsiveContainer>
        </div>
      )}
    </div>
  );
}

const inr: Fmt = (v) => `₹${v.toLocaleString("en-IN", { maximumFractionDigits: v >= 1000 ? 0 : 2 })}`;
const pts: Fmt = (v) => v.toLocaleString("en-IN", { maximumFractionDigits: 2 });

/**
 * The time-framed price chart for a stock (intraday + daily) or an index (intraday only). `daily` are the page's daily
 * bars (the exchange's EOD history); `defaults` the profile's chart defaults; `refreshS` the profile's live refresh (0 = off).
 */
export function PriceChart({ kind, symbol, daily, dailyLoading, dailyError, onDailyRetry, dailyEmpty, defaults, refreshS, height = 300,
  compactHeader = false, onRange, exchange = "NSE" }: {
  kind: "stock" | "index"; symbol: string; daily?: Ohlc[]; dailyLoading?: boolean; defaults: ChartDefaults; refreshS: number;
  /** daily-history states, shown only when a daily range (1M…5Y) is chosen */
  dailyError?: string | null; onDailyRetry?: () => void; dailyEmpty?: string | null;
  height?: number; compactHeader?: boolean;
  /** Called with the chosen range (e.g. so the page fetches enough daily history). */ onRange?: (r: ChartRange) => void;
  /** The exchange whose series this is (the stock page's BSE view passes "BSE" with a "BSE:<code>" symbol). */ exchange?: "NSE" | "BSE";
}) {
  const choice = useChartChoice(kind, defaults);
  const { range, interval, type } = choice.value;
  useEffect(() => onRange?.(range), [onRange, range]);
  const intraday = INTRADAY_RANGES.includes(range);
  const base = kind === "stock" ? `/api/stocks/${encodeURIComponent(symbol)}` : `/api/indices/${encodeURIComponent(symbol)}`;
  const everyMs = refreshS > 0 ? Math.max(refreshS, 30) * 1000 : 0; // the series gains one point a minute
  const live = useLive<IntradayResponse>(intraday ? `${base}/intraday?interval=${interval}&days=${RANGE_DAYS[range]}` : null,
    { session: "equity", everyMs: everyMs || 0, active: everyMs > 0 });
  const ranges: ChartRange[] = kind === "stock" ? [...INTRADAY_RANGES, ...DAILY_RANGES] : INTRADAY_RANGES;
  const format = kind === "stock" ? inr : pts;

  const dailyRows = useMemo(() => {
    if (intraday || !daily?.length) return [];
    const last = new Date(daily[daily.length - 1].t).getTime();
    return daily.filter((r) => new Date(r.t).getTime() >= last - RANGE_DAYS[range] * 86400000);
  }, [daily, intraday, range]);
  const intradayRows: Ohlc[] = useMemo(() => (live.data?.candles ?? []).map((c) => ({ ...c, v: c.v ?? null })), [live.data]);
  const rows = intraday ? intradayRows : dailyRows;
  const multiDay = intraday ? (live.data?.sessions.length ?? 0) > 1 : true;
  const xFormat = intraday ? (multiDay ? (s: string) => `${dayMon(s)} ${hhmm(s)}` : hhmm) : (s: string) => shortDate(s);
  const change = intraday
    ? range === "1D" ? live.data?.change_pct ?? null : rows.length > 1 && rows[0].o ? ((rows[rows.length - 1].c ?? 0) / rows[0].o - 1) * 100 : null
    : rows.length > 1 && rows[0].c ? ((rows[rows.length - 1].c ?? 0) / rows[0].c - 1) * 100 : null;
  const sessions = live.data?.sessions.length ?? 0;
  const lineOnly = interval === "1m";

  return (
    <div>
      <div className={cx("mb-3 flex flex-wrap items-center gap-2", compactHeader ? "justify-end" : "justify-between")}>
        {!compactHeader && (
          <p className="text-xs text-muted">
            {range} change{" "}
            <span className={cx("num text-sm font-semibold", toneOf(change == null ? null : change / 100))}>{signedPct(change == null ? null : change / 100)}</span>
            {intraday && range === "1D" && live.data?.prev_close != null && <span className="ml-1">vs previous close {format(live.data.prev_close)}</span>}
          </p>
        )}
        <div className="flex flex-wrap items-center gap-2">
          <Segmented value={range} onChange={(r) => choice.set({ range: r })} options={ranges.map((r) => ({ value: r, label: r }))} />
          {intraday && (
            <Segmented value={interval} onChange={(i: ChartInterval) => choice.set({ interval: i })}
              options={intervalsFor(range).map((i) => ({ value: i, label: i }))} />
          )}
          <Segmented value={lineOnly ? "line" : type} onChange={(t) => choice.set({ type: t })}
            options={[
              { value: "line", label: <span className="inline-flex items-center gap-1" title="Line (closes)"><LineIcon className="size-3.5" /><span className="sr-only">Line</span></span> },
              { value: "candle", label: <span className="inline-flex items-center gap-1" title={lineOnly ? "1-minute data is one price a minute: shown as a line" : "Candles (open, high, low, close)"}><CandlestickChart className="size-3.5" /><span className="sr-only">Candles</span></span> },
            ]} />
          <Link href="/profile#time-frames" title="Set your default range, interval, chart type and refresh on the profile page"
            className="inline-flex items-center gap-1 rounded-md px-1.5 py-1 text-[11px] text-muted transition hover:bg-background-subtle hover:text-foreground">
            <SlidersHorizontal className="size-3.5" /> Defaults
          </Link>
        </div>
      </div>

      {intraday && live.error && !live.data ? (
        <ErrorNote error={`Intraday series: ${live.error}`} onRetry={live.reload} />
      ) : !intraday && dailyError ? (
        <ErrorNote error={`Daily history: ${dailyError}`} onRetry={onDailyRetry} />
      ) : !intraday && dailyEmpty ? (
        <EmptyState title="No price history">{dailyEmpty}</EmptyState>
      ) : (intraday && !live.data) || (!intraday && dailyLoading && !rows.length) ? (
        <div style={{ height }}><Skeleton className="h-full w-full rounded-lg" /></div>
      ) : rows.length < 2 ? (
        <EmptyState title="Not enough data for this range">
          {intraday ? `${exchange} has published fewer than two samples for this session yet.` : "No trading days in this period."}
        </EmptyState>
      ) : (
        <CandleChart rows={rows} type={lineOnly ? "line" : type} format={format} xFormat={xFormat} height={height} syncId={`${kind}-${symbol}`}
          reference={intraday && range === "1D" && live.data?.prev_close ? { y: live.data.prev_close, label: "prev close" } : undefined} />
      )}

      <div className="mt-2 space-y-1">
        {intraday && live.data ? (
          <Freshness mode="intraday" sourceLabel={live.data.source_label} sourceHref={live.data.source} asOf={live.data.as_of}
            fetchedAt={live.data.fetched_at} delayS={live.data.delay_s} live={live.data.live}
            refresh={everyMs ? `refreshes every ${Math.round(everyMs / 1000)} s in market hours` : "auto-refresh off"} />
        ) : !intraday && rows.length ? (
          <Freshness mode="eod" sourceLabel={`${exchange} daily bars (end of day)`} asOf={rows[rows.length - 1].t} />
        ) : null}
        {intraday && live.data && (
          <p className="text-[11px] text-muted">
            {interval === "1m" ? "One price a minute, shown as a line." : `${interval} candles built from 1-minute prices, so highs/lows can be slightly understated.`}
            {live.data.has_volume ? ` Volume is ${live.data.exchange ?? "the exchange"}'s own.` : " NSE publishes no intraday volume on its public pages."}
            {range === "5D" && sessions < 5 && ` ${sessions} of 5 sessions archived so far: the exchange publishes only the current session, so the app keeps each day from the first day you view or watch this ${kind === "stock" ? "stock" : "index"}.`}
          </p>
        )}
      </div>
    </div>
  );
}

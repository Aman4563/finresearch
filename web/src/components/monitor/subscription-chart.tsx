"use client";

// Subscription over time on a time-proportional axis (roadmap §B /monitor/1 item 1). Checks are unevenly spaced
// (six a bidding day, more around the close), so an evenly spaced category axis bends the curve. Here the x-axis is
// bidding time: each day's session (9:30 am-5:30 pm IST, so the pre-open and the post-close final book fit) takes the
// same width and positions inside it are proportional to the clock. Nights and weekends, when nothing can change,
// are left out and marked by a day divider instead of being drawn as long flat gaps.

import { useMemo } from "react";
import { CartesianGrid, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import { dayLabel, istDate, timeIST } from "@/components/ipo/lib";

const OPEN_MIN = 9 * 60 + 30; // 9:30 am IST
const SESSION_MIN = 8 * 60; // to 5:30 pm
const axis = { tickLine: false, axisLine: false, fontSize: 11 } as const;

type Series = { key: string; label: string; color: string };

/** Minutes after midnight IST for an ISO timestamp. */
function istMinutes(iso: string) {
  const d = new Date(Date.parse(iso) + 330 * 60000);
  return d.getUTCHours() * 60 + d.getUTCMinutes();
}

/** Position of each row on the bidding-time axis: day index × session + minutes into the session (clamped). */
export function biddingTime(rows: { at: string }[]) {
  const days = [...new Set(rows.map((r) => istDate(Date.parse(r.at))))].sort();
  const pos = rows.map((r) => {
    const day = days.indexOf(istDate(Date.parse(r.at)));
    const into = Math.min(SESSION_MIN, Math.max(0, istMinutes(r.at) - OPEN_MIN));
    return day * SESSION_MIN + into;
  });
  return { days, pos };
}

function Tip({ active, payload, format }: { active?: boolean; payload?: { name?: string; value?: number; color?: string; dataKey?: string; payload?: Record<string, unknown> }[]; format: (v: number) => string }) {
  if (!active || !payload?.length) return null;
  const at = String(payload[0].payload?.at ?? "");
  return (
    <div className="rounded-lg border border-border bg-card/95 px-3 py-2 text-xs shadow-pop backdrop-blur animate-scale-in">
      <p className="mb-1 font-medium text-muted">{dayLabel(at, { day: "numeric", month: "short" })}, {timeIST(at)}</p>
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

export function SubscriptionTimeChart({ data, series, format, height = 300 }: {
  data: (Record<string, unknown> & { at: string })[]; series: Series[]; format: (v: number) => string; height?: number;
}) {
  const { rows, days } = useMemo(() => {
    const { days, pos } = biddingTime(data);
    return { rows: data.map((r, i) => ({ ...r, t: pos[i] })), days };
  }, [data]);
  // a tick at 10 am and 1 pm of every bidding day (the day's label sits on its 1 pm tick)
  // (the 5 pm tick only on the last day, so a close and the next open do not print on top of each other)
  const ticks = days.flatMap((_, d) => (d === days.length - 1 ? [30, 210, 450] : [30, 210]).map((m) => d * SESSION_MIN + m));
  const tickLabel = (v: number) => {
    const d = Math.floor(v / SESSION_MIN), m = v - d * SESSION_MIN;
    if (m === 210) return dayLabel(days[d], { day: "numeric", month: "short" });
    return m === 30 ? "10am" : "5pm";
  };
  return (
    <div>
      <div style={{ height }} className="animate-fade-in" role="img"
        aria-label={`Times subscribed by category across ${days.length} bidding day${days.length === 1 ? "" : "s"}; the table below lists every check.`}>
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={rows} margin={{ top: 6, right: 12, bottom: 0, left: 0 }}>
            <CartesianGrid vertical={false} strokeDasharray="3 3" />
            <XAxis dataKey="t" type="number" domain={[0, days.length * SESSION_MIN]} ticks={ticks} tickFormatter={tickLabel} {...axis} interval={0} />
            <YAxis {...axis} width={56} tickFormatter={format} />
            <Tooltip content={<Tip format={format} />} />
            {days.slice(1).map((d, i) => (
              <ReferenceLine key={d} x={(i + 1) * SESSION_MIN} stroke="var(--border-strong)" />
            ))}
            <ReferenceLine y={1} stroke="var(--muted)" strokeDasharray="4 4" />
            {series.map((s) => (
              <Line key={s.key} type="linear" dataKey={s.key} name={s.label} stroke={s.color} strokeWidth={2} connectNulls
                dot={{ r: 3, strokeWidth: 0, fill: s.color }} activeDot={{ r: 4, strokeWidth: 2, stroke: "var(--card)" }} animationDuration={700} />
            ))}
          </LineChart>
        </ResponsiveContainer>
      </div>
      <ul className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-muted">
        {series.map((s) => (
          <li key={s.key} className="inline-flex items-center gap-1"><span className="size-2 rounded-full" style={{ background: s.color }} />{s.label}</li>
        ))}
        <li className="ml-auto">x-axis: bidding hours only, spaced by the clock; nights are left out.</li>
      </ul>
    </div>
  );
}

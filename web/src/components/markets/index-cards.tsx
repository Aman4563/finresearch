"use client";

// Dashboard "Markets today": NIFTY 50, NIFTY BANK and INDIA VIX from NSE's 1-minute index series (5-minute closes as a
// sparkline), with the last value, the change from the previous close and freshness. Pick one to open its chart with
// the index time frames from the profile (1D / 5D, candle interval, line or candles).

import { Activity } from "lucide-react";
import { useState } from "react";

import { Sparkline } from "@/components/charts";
import { behind, useLive } from "@/components/live";
import { type IntradayResponse, PriceChart } from "@/components/markets/price-chart";
import { Card, cx, Skeleton } from "@/components/ui";
import { useTimeFrames } from "@/lib/timeframes";

const INDICES = ["NIFTY 50", "NIFTY BANK", "INDIA VIX"] as const;

function IndexTile({ name, refreshS, selected, onSelect }: { name: string; refreshS: number; selected: boolean; onSelect: () => void }) {
  const everyMs = refreshS > 0 ? Math.max(refreshS, 30) * 1000 : 0;
  const r = useLive<IntradayResponse>(`/api/indices/${encodeURIComponent(name)}/intraday?interval=5m`, { session: "equity", everyMs, active: everyMs > 0 });
  const d = r.data;
  const pct = d?.change_pct ?? null;
  const up = (pct ?? 0) >= 0;
  const asOf = d?.as_of ? new Date(d.as_of).toLocaleTimeString("en-IN", { hour: "numeric", minute: "2-digit", timeZone: "Asia/Kolkata" }) : null;
  return (
    <button type="button" onClick={onSelect} aria-pressed={selected}
      className={cx("min-w-0 rounded-lg border p-3 text-left transition hover:border-border-strong",
        selected ? "border-brand/50 bg-brand-soft/40" : "border-border bg-background-subtle/40")}>
      <div className="flex items-baseline justify-between gap-2">
        <span className="truncate text-xs font-medium text-muted">{name}</span>
        {pct != null && <span className={cx("num text-xs font-semibold", up ? "text-gain" : "text-loss")}>{up ? "+" : ""}{pct.toFixed(2)}%</span>}
      </div>
      {d ? (
        <>
          <div className="mt-1 flex items-end justify-between gap-2">
            <span className="num text-lg font-semibold">{d.last?.toLocaleString("en-IN", { maximumFractionDigits: 2 }) ?? "—"}</span>
            <Sparkline values={d.candles.map((c) => c.c)} width={88} height={28} />
          </div>
          <p className="mt-1 text-[10px] text-muted">
            {d.live ? `NSE ${asOf}${d.delay_s != null ? ` · ${behind(d.delay_s * 1000)}` : ""}` : d.last_kind === "official_close" ? `Official close · data to ${asOf}` : `Closed · last value ${asOf}`}
          </p>
        </>
      ) : r.error ? (
        <p className="mt-2 text-xs text-muted">NSE did not answer. Try again shortly.</p>
      ) : (
        <Skeleton className="mt-2 h-9 w-full" />
      )}
    </button>
  );
}

export function IndexCards() {
  const { tf } = useTimeFrames();
  const [open, setOpen] = useState<string | null>(null);
  return (
    <Card title="Markets today" icon={<Activity className="size-4" />}
      help="NSE index values from NSE's 1-minute index series. Pick an index to see its intraday chart; default range and candle size are on the profile page (Time frames & watch).">
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
        {INDICES.map((n) => (
          <IndexTile key={n} name={n} refreshS={tf.quote_refresh_s} selected={open === n} onSelect={() => setOpen(open === n ? null : n)} />
        ))}
      </div>
      {open && (
        <div className="mt-4 animate-fade-in">
          <p className="mb-2 text-sm font-semibold">{open}</p>
          <PriceChart kind="index" symbol={open} defaults={tf.index} refreshS={tf.quote_refresh_s} height={240} />
        </div>
      )}
    </Card>
  );
}

"use client";

// Profile → "Time frames & watch": default chart range/interval/type for stocks and indices, live refresh cadences
// (never faster than the source), and the monitor's watch windows (IPO subscription check times, the daily stock
// check time, quiet hours for the alert badge). Saved with the profile; the monitor re-plans pending checks.

import { BellOff, CalendarClock, CandlestickChart, Check, LineChart as LineIcon, RefreshCw } from "lucide-react";
import { type ReactNode, useEffect } from "react";

import { Labelled, Switch } from "@/components/profile/common";
import { Card, cx, InfoTip, inputClass, Segmented } from "@/components/ui";
import {
  type ChartDefaults, type ChartInterval, type ChartRange, DAILY_RANGES, fitInterval, INTRADAY_RANGES, intervalsFor, IPO_CHECK_CHOICES,
  IPO_FINAL_CHECK, refreshLabel, STOCK_DAILY_CHOICES, type TimeFrames, type WatchWindows,
} from "@/lib/timeframes";

const clock = (hm: string) => {
  const [h, m] = hm.split(":").map(Number);
  return `${((h + 11) % 12) + 1}:${String(m).padStart(2, "0")} ${h < 12 ? "am" : "pm"}`;
};

function Section({ icon, title, children, tip }: { icon: ReactNode; title: string; children: ReactNode; tip?: ReactNode }) {
  return (
    <div className="space-y-4 border-t border-border pt-4 first:border-t-0 first:pt-0">
      <h3 className="inline-flex items-center gap-1.5 text-sm font-semibold">
        <span className="text-muted">{icon}</span>
        {title}
        {tip && <InfoTip>{tip}</InfoTip>}
      </h3>
      {children}
    </div>
  );
}

function ChartDefaultsRow({ label, value, ranges, onChange }: {
  label: string; value: ChartDefaults; ranges: ChartRange[]; onChange: (v: ChartDefaults) => void;
}) {
  const intervals = intervalsFor(value.range);
  return (
    <div className="grid gap-3 sm:grid-cols-[7rem_1fr_1fr_auto] sm:items-end">
      <p className="text-sm font-medium sm:pb-2">{label}</p>
      <label className="block space-y-1.5">
        <span className="block text-xs font-medium text-muted">Range</span>
        <select aria-label={`${label}: default range`} className={cx(inputClass, "w-full")} value={value.range}
          onChange={(e) => {
            const range = e.target.value as ChartRange;
            onChange({ ...value, range, interval: fitInterval(range, value.interval) });
          }}>
          {ranges.map((r) => <option key={r} value={r}>{r}{INTRADAY_RANGES.includes(r) ? " (intraday)" : ""}</option>)}
        </select>
      </label>
      <label className="block space-y-1.5">
        <span className="block text-xs font-medium text-muted">Candle interval</span>
        <select aria-label={`${label}: default candle interval`} className={cx(inputClass, "w-full")} value={value.interval}
          disabled={intervals.length === 1} onChange={(e) => onChange({ ...value, interval: e.target.value as ChartInterval })}>
          {intervals.map((i) => <option key={i} value={i}>{i === "1D" ? "1D (daily bars)" : i}</option>)}
        </select>
      </label>
      <div className="space-y-1.5">
        <span className="block text-xs font-medium text-muted">Chart</span>
        <Segmented value={value.type} onChange={(type) => onChange({ ...value, type })}
          options={[
            { value: "line", label: <span className="inline-flex items-center gap-1"><LineIcon className="size-3.5" />Line</span> },
            { value: "candle", label: <span className="inline-flex items-center gap-1"><CandlestickChart className="size-3.5" />Candles</span> },
          ]} />
      </div>
    </div>
  );
}

function RefreshPicker<T extends number>({ label, value, options, onChange, hint }: {
  label: string; value: T; options: T[]; onChange: (v: T) => void; hint: ReactNode;
}) {
  return (
    <Labelled label={label} hint={hint}>
      <div>
        <Segmented value={String(value)} onChange={(v) => onChange(Number(v) as T)}
          options={options.map((o) => ({ value: String(o), label: refreshLabel(o) }))} />
      </div>
    </Labelled>
  );
}

export function TimeFramesCard({ tf, watch, onTf, onWatch }: {
  tf: TimeFrames; watch: WatchWindows; onTf: (v: TimeFrames) => void; onWatch: (v: WatchWindows) => void;
}) {
  const toggleSlot = (t: string) => {
    const on = watch.ipo_check_times.includes(t);
    onWatch({ ...watch, ipo_check_times: (on ? watch.ipo_check_times.filter((x) => x !== t) : [...watch.ipo_check_times, t]).sort() });
  };
  const quiet = watch.quiet_start != null && watch.quiet_end != null;
  // /profile#time-frames (the charts' "Defaults" link): the card renders after the profile loads, so scroll then
  useEffect(() => {
    if (window.location.hash === "#time-frames") document.getElementById("time-frames")?.scrollIntoView({ block: "start" });
  }, []);
  return (
    <div id="time-frames" className="scroll-mt-20">
    <Card icon={<CalendarClock className="size-4" />} title="Time frames & watch"
      subtitle="Default charts, how often live prices refresh, and when the monitor checks.">
      <div className="space-y-5">
        <Section icon={<CandlestickChart className="size-4" />} title="Default charts"
          tip="Range is how much time the chart covers; the candle interval is how much time each candle (or line point) sums up. 1D and 5D use NSE's 1-minute prices; 1M and longer use NSE's official daily bars. Each page also remembers your last choice on this browser.">
          <ChartDefaultsRow label="Stocks" value={tf.stock} ranges={[...INTRADAY_RANGES, ...DAILY_RANGES]} onChange={(stock) => onTf({ ...tf, stock })} />
          <ChartDefaultsRow label="Indices" value={tf.index} ranges={INTRADAY_RANGES} onChange={(index) => onTf({ ...tf, index })} />
          <p className="text-[11px] text-muted">
            5D fills in over time: NSE publishes only the current session, so the app keeps each day from the first day you view or watch a symbol.
          </p>
        </Section>

        <Section icon={<RefreshCw className="size-4" />} title="Live refresh (market hours only)">
          <div className="grid gap-4 sm:grid-cols-3">
            <RefreshPicker label="Stock price" value={tf.quote_refresh_s} options={[15, 30, 60, 0] as const as (0 | 15 | 30 | 60)[]}
              onChange={(quote_refresh_s) => onTf({ ...tf, quote_refresh_s })}
              hint="NSE's quote is about a minute behind the exchange; the app caches it 15 s, so faster would not be fresher." />
            <RefreshPicker label="F&O option chain" value={tf.fno_refresh_s} options={[60, 120, 300, 0] as const as (0 | 60 | 120 | 300)[]}
              onChange={(fno_refresh_s) => onTf({ ...tf, fno_refresh_s })}
              hint="NSE updates the chain every few minutes; 1 min is the floor." />
            <RefreshPicker label="IPO subscription book" value={tf.ipo_book_refresh_s} options={[60, 120, 300, 0] as const as (0 | 60 | 120 | 300)[]}
              onChange={(ipo_book_refresh_s) => onTf({ ...tf, ipo_book_refresh_s })}
              hint="On a watched IPO's page during bidding hours. The exchanges refresh the book every few minutes." />
          </div>
        </Section>

        <Section icon={<CalendarClock className="size-4" />} title="Watch windows (IST)"
          tip="When the monitor runs its scheduled checks for watched IPOs and stocks. Changing a time re-plans the checks that have not run yet; nothing runs twice.">
          <Labelled label="IPO subscription checks on each bidding day"
            hint={<>Bidding runs 10:00 am – 5:00 pm. The {clock(IPO_FINAL_CHECK)} check after the close always runs: it records the final book. Default: 10:30, 12:00, 1:30, 3:00 and 4:00.</>}>
            <div className="flex flex-wrap gap-1.5" role="group" aria-label="IPO subscription check times">
              {IPO_CHECK_CHOICES.map((t) => {
                const on = watch.ipo_check_times.includes(t);
                return (
                  <button key={t} type="button" aria-pressed={on} onClick={() => toggleSlot(t)}
                    className={cx("num inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs ring-1 ring-inset transition",
                      on ? "bg-brand-soft text-brand ring-brand/40" : "text-muted ring-border hover:text-foreground")}>
                    {on && <Check className="size-3" />}
                    {clock(t)}
                  </button>
                );
              })}
              <span className="num inline-flex items-center gap-1 rounded-md bg-background-subtle px-2 py-1 text-xs text-muted ring-1 ring-inset ring-border" title="Always runs">
                <Check className="size-3" /> {clock(IPO_FINAL_CHECK)} final
              </span>
            </div>
          </Labelled>
          <div className="grid gap-4 sm:grid-cols-2">
            <label className="block space-y-1.5">
              <span className="block text-xs font-medium text-muted">Daily check for watched stocks</span>
              <select aria-label="Daily stock check time" className={cx(inputClass, "w-full")} value={watch.stock_daily_time}
                onChange={(e) => onWatch({ ...watch, stock_daily_time: e.target.value })}>
                {STOCK_DAILY_CHOICES.map((t) => <option key={t} value={t}>{clock(t)}{t === "16:30" ? " (default)" : ""}</option>)}
              </select>
              <span className="block text-[11px] text-muted">After the 3:30 pm close. Later catches more results filed in the evening.</span>
            </label>
            <div className="space-y-2">
              <Switch checked={quiet} label="Quiet hours"
                description="The alert bell shows no count or animation for new info alerts in this window. Alerts are still recorded; warnings still show."
                onChange={(v) => onWatch({ ...watch, quiet_start: v ? "22:00" : null, quiet_end: v ? "07:00" : null })} />
              {quiet && (
                <div className="flex items-center gap-2 text-sm">
                  <BellOff className="size-4 text-muted" />
                  <input type="time" step={60} aria-label="Quiet from" className={cx(inputClass, "w-32")} value={watch.quiet_start ?? ""}
                    onChange={(e) => onWatch({ ...watch, quiet_start: e.target.value || "22:00" })} />
                  <span className="text-muted">to</span>
                  <input type="time" step={60} aria-label="Quiet until" className={cx(inputClass, "w-32")} value={watch.quiet_end ?? ""}
                    onChange={(e) => onWatch({ ...watch, quiet_end: e.target.value || "07:00" })} />
                </div>
              )}
            </div>
          </div>
        </Section>
      </div>
    </Card>
    </div>
  );
}

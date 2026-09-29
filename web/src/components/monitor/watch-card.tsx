"use client";

import { BellDot, ChartCandlestick, Clock, Square } from "lucide-react";
import Link from "next/link";
import { useState } from "react";

import { LinkButton } from "@/components/dashboard/link-button";
import {
  LiveDot, Stepper, SubMeter, countdown, currentStage, dayLabel, daysUntil, stagesFor, times, timeIST,
} from "@/components/ipo/lib";
import { Badge, Button, Modal, cx } from "@/components/ui";
import { api, when, type WatchSummary } from "@/lib/api";

export const JOB_LABEL: Record<string, string> = {
  subscription: "Subscription check",
  allotment: "Allotment check",
  listing: "Listing price",
  lockin: "Lock-in end",
  stock_daily: "Daily after-close check",
};

export const jobLabel = (k: string) => JOB_LABEL[k] ?? k.replaceAll("_", " ");

/** Rule statuses recorded on the watch by the monitor (clear / fired / unknown). */
export function RuleChips({ status }: { status: unknown }) {
  if (!status || typeof status !== "object") return null;
  const entries = Object.entries(status as Record<string, string>);
  if (!entries.length) return null;
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      <span className="text-[11px] text-muted">Your rules:</span>
      {entries.map(([k, v]) => (
        <Badge key={k} status={v === "fired" ? "fired" : v === "clear" ? "clear" : "pending"}>
          {k} {v === "fired" ? "fired" : v === "clear" ? "ok" : "?"}
        </Badge>
      ))}
    </div>
  );
}

export function NextCheck({ next, now }: { next: WatchSummary["next_check"]; now: number | null }) {
  if (!next) return <span className="text-xs text-muted">No check scheduled</span>;
  const due = Date.parse(next.due_at);
  return (
    <span className="inline-flex items-center gap-1.5 text-xs text-muted" title={when(next.due_at)}>
      <Clock className="size-3.5" />
      {jobLabel(next.kind)}
      <span className="num font-medium text-foreground">
        {now == null ? "" : due > now ? `in ${countdown(due, now)}` : "due now"}
      </span>
    </span>
  );
}

export function StopWatch({ w, onDone, onError }: { w: WatchSummary; onDone: () => void; onError: (e: string) => void }) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const stop = async () => {
    setBusy(true);
    try {
      await api(`/api/watches/${w.id}/stop`, { method: "POST" });
      setOpen(false);
      onDone();
    } catch (e) {
      onError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <>
      <Button variant="ghost" icon={<Square className="size-3" />} onClick={() => setOpen(true)}>Stop</Button>
      <Modal open={open} onClose={() => setOpen(false)} title={`Stop watching ${w.company_name ?? w.nse_symbol}?`}>
        <p className="p-4 text-sm">
          Pending checks are cancelled and no new alerts are raised. The history (snapshots, alerts) stays. You can watch it
          again later.
        </p>
        <div className="flex justify-end gap-2 border-t border-border px-4 py-3">
          <Button variant="ghost" onClick={() => setOpen(false)}>Keep watching</Button>
          <Button variant="danger" onClick={stop} disabled={busy}>{busy ? "Stopping…" : "Stop watch"}</Button>
        </div>
      </Modal>
    </>
  );
}

export function WatchCard({ w, now, onChange, onError }: {
  w: WatchSummary; now: number | null; onChange: () => void; onError: (e: string) => void;
}) {
  const sub = times(w.last_subscription?.total_times);
  const bidding = w.kind === "ipo" && now != null && (daysUntil(w.open_date, now) ?? 1) <= 0 && (daysUntil(w.close_date, now) ?? -1) >= 0;
  return (
    <article className={cx(
      "flex flex-col rounded-xl border border-border bg-card p-4 shadow-card transition duration-200 hover:-translate-y-0.5 hover:border-border-strong hover:shadow-glow",
      !w.active && "opacity-70",
    )}>
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <Link href={`/monitor/${w.id}`} className="flex items-center gap-2 font-semibold leading-snug hover:text-brand">
            {w.active && bidding && <LiveDot />}
            <span className="truncate">{w.company_name ?? w.nse_symbol}</span>
          </Link>
          <p className="num mt-0.5 text-xs text-muted">{w.nse_symbol} · {w.kind === "stock" ? "listed stock" : "IPO"}</p>
        </div>
        <div className="flex shrink-0 flex-wrap justify-end gap-1">
          {!w.active && <Badge status="closed">stopped</Badge>}
          {w.active && <Badge tone={bidding ? "gain" : "info"} dot={bidding}>{bidding ? "bidding" : "watching"}</Badge>}
          {!!w.unread_alerts && <Badge tone="loss"><BellDot className="size-3" />{w.unread_alerts} new</Badge>}
        </div>
      </div>

      {w.kind === "ipo" ? (
        <>
          <Stepper className="mt-5" stages={stagesFor(w)} current={now == null ? -1 : currentStage(w, now)} />
          <div className="mt-4 rounded-lg bg-background-subtle/60 p-3">
            <p className="mb-1.5 flex justify-between text-[11px] text-muted">
              <span>Last subscription (total)</span>
              {w.last_subscription && <span className="num">{dayLabel(w.last_subscription.as_of, { day: "numeric", month: "short" })}, {timeIST(w.last_subscription.as_of)}</span>}
            </p>
            {w.last_subscription ? <SubMeter value={sub} /> : <p className="text-xs text-muted">No snapshot yet: the first check records one.</p>}
          </div>
        </>
      ) : (
        <p className="mt-4 flex items-start gap-2 rounded-lg bg-background-subtle/60 p-3 text-xs text-muted">
          <ChartCandlestick className="mt-0.5 size-4 shrink-0 text-info" />
          Checked after each market close: results filings, corporate actions and ex-dates, promoter holding, and moves of 5% or more.
        </p>
      )}

      <div className="mt-3 mb-4"><RuleChips status={w.meta?.rule_status} /></div>

      <div className="mt-auto flex flex-wrap items-center justify-between gap-2 border-t border-border/70 pt-3">
        {w.active ? <NextCheck next={w.next_check} now={now} /> : <span className="text-xs text-muted">Stopped</span>}
        <div className="flex items-center gap-1">
          {w.active && <StopWatch w={w} onDone={onChange} onError={onError} />}
          <LinkButton href={`/monitor/${w.id}`}>Details</LinkButton>
        </div>
      </div>
    </article>
  );
}

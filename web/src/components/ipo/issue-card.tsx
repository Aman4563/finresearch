"use client";

import { CalendarClock, FileText } from "lucide-react";
import Link from "next/link";

import { CopyCommand, ResearchButton, bseCommand } from "@/components/ipo/actions";
import {
  LiveDot, SubMeter, countdown, dayLabel, daysUntil, inr, isSme, istAt, lotCost, parseBand, relDay, times,
} from "@/components/ipo/lib";
import { Badge, cx } from "@/components/ui";
import type { Issue } from "@/lib/api";

export const PHASE_TONE = { open: "gain", current: "gain", upcoming: "info", closed: "neutral" } as const;

/** "Closes today · 3h 10m", "Opens in 2 days", "Closed 1 day ago". */
export function issueTiming(i: Issue, now: number | null) {
  if (now == null) return { text: "", urgent: false };
  if (i.phase === "upcoming" && i.issue_start) {
    const d = daysUntil(i.issue_start, now)!;
    return { text: d <= 0 ? "Opens today" : `Opens ${relDay(d).toLowerCase()}`, urgent: false };
  }
  if (i.issue_end) {
    const d = daysUntil(i.issue_end, now)!;
    if (d < 0) return { text: `Closed ${relDay(d)}`, urgent: false };
    const cut = istAt(i.issue_end);
    if (d === 0) return { text: cut > now ? `Closes today · ${countdown(cut, now)} left` : "Bidding closed today", urgent: cut > now };
    return { text: `${d} day${d === 1 ? "" : "s"} left · closes ${dayLabel(i.issue_end, { day: "numeric", month: "short" })}`, urgent: d === 1 };
  }
  return { text: "", urgent: false };
}

export const windowText = (i: Issue) => {
  const f = (d: string | null) => (d ? dayLabel(d, { day: "numeric", month: "short" }) : "?");
  return `${f(i.issue_start)} → ${f(i.issue_end)}`;
};

/** NSE's issue list has no lot size, so lot costs are only shown where the exchange publishes one (BSE SME). */
export function NoLot() {
  return (
    <span className="font-normal text-muted" title="NSE's list does not include the lot size; it is in the offer document (RHP).">
      in RHP
    </span>
  );
}

export function ResearchCell({ i, compact }: { i: Issue; compact?: boolean }) {
  if (i.latest_run)
    return (
      <Link href={`/runs/${i.latest_run}`} className="inline-flex items-center gap-1.5 text-xs font-medium text-brand hover:underline">
        <FileText className="size-3.5" /> Run #{i.latest_run} <Badge status={i.latest_run_status ?? "pending"} />
      </Link>
    );
  if (i.slug) return <ResearchButton slug={i.slug} kind="ipo_report" label={i.company} />;
  if (i.bse_ipo_no) return <CopyCommand command={bseCommand(i)} compact={compact} className={compact ? undefined : "w-full"} />;
  return <ResearchButton issue={i} kind="ipo_report" />;
}

export function IssueCard({ i, now }: { i: Issue; now: number | null }) {
  const band = parseBand(i.price_band);
  const cost = lotCost(i);
  const sub = times(i.times_subscribed);
  const t = issueTiming(i, now);
  const open = i.phase === "open" || i.phase === "current";
  return (
    <article className="group flex flex-col rounded-xl border border-border bg-card p-4 shadow-card transition duration-200 hover:-translate-y-0.5 hover:border-border-strong hover:shadow-glow">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h3 className="flex items-center gap-2 font-semibold leading-snug">
            {open && <LiveDot />}
            <span className="line-clamp-2">{i.company}</span>
          </h3>
          <p className="num mt-0.5 text-xs text-muted">
            {i.exchange} · {i.symbol}
          </p>
        </div>
        <div className="flex shrink-0 flex-col items-end gap-1">
          <Badge tone={PHASE_TONE[i.phase]} dot={open}>{i.phase === "current" ? "open" : i.phase}</Badge>
          <Badge tone={isSme(i) ? "accent" : "brand"}>{isSme(i) ? "SME" : "Mainboard"}</Badge>
        </div>
      </div>

      <dl className="mt-4 grid grid-cols-3 gap-2 text-xs">
        <div>
          <dt className="text-muted">Price band</dt>
          <dd className="num mt-0.5 font-medium">{band ? `₹${band[0]}–${band[1]}` : "—"}</dd>
        </div>
        <div>
          <dt className="text-muted">Lot cost</dt>
          <dd className="num mt-0.5 font-medium">{cost ? inr(cost.lot) : <NoLot />}</dd>
          {i.lot_size ? <dd className="num text-[10px] text-muted">{i.lot_size.toLocaleString("en-IN")} sh</dd> : null}
        </div>
        <div>
          <dt className="text-muted">Min. invest</dt>
          <dd className="num mt-0.5 font-medium">{cost ? inr(cost.min) : <NoLot />}</dd>
          {cost && cost.minLots > 1 ? <dd className="text-[10px] text-muted">{cost.minLots} lots</dd> : null}
        </div>
      </dl>

      <div className="mt-4">
        <p className="mb-1 flex justify-between text-[11px] text-muted">
          <span>Subscription</span>
          {sub == null && <span>{i.phase === "upcoming" ? "not open yet" : "not published"}</span>}
        </p>
        <SubMeter value={sub} />
      </div>

      <div className="mt-auto pt-4">
        <div className="mb-3 flex flex-wrap items-center justify-between gap-x-3 gap-y-1 text-xs">
          <p className={cx("flex items-center gap-1.5", t.urgent ? "font-medium text-warn" : "text-muted")}>
            <CalendarClock className="size-3.5" />
            <span className="num">{t.text}</span>
          </p>
          <span className="num text-[11px] text-muted">{windowText(i)}</span>
        </div>
        <ResearchCell i={i} />
      </div>
    </article>
  );
}

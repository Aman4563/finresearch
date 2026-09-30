"use client";

import { CalendarClock, FileText } from "lucide-react";
import Link from "next/link";

import { CopyCommand, ResearchButton, bseCommand } from "@/components/ipo/actions";
import { IpoSignalLine } from "@/components/ipo/ipo-signal";
import {
  LiveDot, SubMeter, TERMS, categoryMins, countdown, dayLabel, daysUntil, inr, isSme, istAt, istDate, lakh, lotCost, lotSourceText,
  parseBand, relDay, times,
} from "@/components/ipo/lib";
import { Badge, InfoTip, cx } from "@/components/ui";
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

/** No lot: "not out yet" when the exchanges have not published one, "unavailable" when a detail call failed. */
export function NoLot({ i }: { i: Issue }) {
  const failed = !!i.lot_note && /issue page|unavailable/i.test(i.lot_note);
  return (
    <span className={cx("font-normal", failed ? "text-warn" : "text-muted")} title={i.lot_note ?? "The exchanges have not published the lot yet."}>
      {failed ? "unavailable" : "not out yet"}
    </span>
  );
}

/** Retail maximum and sHNI / bHNI minimums at the upper band, with where the lot came from. */
export function CategoryMins({ i, className }: { i: Issue; className?: string }) {
  const cats = categoryMins(i), src = lotSourceText(i);
  if (!cats) return null;
  return (
    <div className={className}>
      <p className="mb-1 flex items-center gap-1 text-[11px] text-muted">
        Apply as <InfoTip>{TERMS.categories}</InfoTip>
      </p>
      <dl className="grid grid-cols-3 gap-2 text-xs">
        {cats.map((c) => (
          <div key={c.key} title={c.hint}>
            <dt className="text-[11px] text-muted">{c.label}</dt>
            <dd className="num font-medium">{c.amount == null ? "—" : lakh(c.amount)}</dd>
            <dd className="num text-[10px] text-muted">{c.lots == null ? "not reachable" : `${c.lots} lot${c.lots === 1 ? "" : "s"}`}</dd>
          </div>
        ))}
      </dl>
      {src && (
        <p className="mt-1.5 truncate text-[10px] text-muted" title={src.title}>
          Lot: {src.short}
        </p>
      )}
    </div>
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
  if (i.bse_ipo_no)
    return compact ? <CopyCommand command={bseCommand(i)} compact /> : (
      <div className="space-y-1">
        {/* why no button: the one-click run starts from NSE's issue page; a BSE-only SME issue has none, so its run is
            started from the terminal with BSE's issue number (roadmap §B /ipos 2) */}
        <p className="text-[11px] text-muted">BSE-only issue: no one-click run yet. Copy and run in a terminal:</p>
        <CopyCommand command={bseCommand(i)} className="w-full" />
      </div>
    );
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
          <dd className="num mt-0.5 font-medium" title={i.price_band_note ?? undefined}>
            {band ? `₹${band[0]}–${band[1]}` : "—"}
            {i.price_band_note && <span className="ml-0.5 text-warn">*</span>}
          </dd>
        </div>
        <div>
          <dt className="text-muted">Lot cost</dt>
          <dd className="num mt-0.5 font-medium">{cost ? inr(cost.lot) : <NoLot i={i} />}</dd>
          {i.lot_size ? <dd className="num text-[10px] text-muted">{i.lot_size.toLocaleString("en-IN")} sh</dd> : null}
        </div>
        <div>
          <dt className="text-muted">Min. invest</dt>
          <dd className="num mt-0.5 font-medium">{cost ? inr(cost.min) : <NoLot i={i} />}</dd>
          {cost && cost.minLots > 1 ? <dd className="text-[10px] text-muted">{cost.minLots} lots</dd> : null}
        </div>
      </dl>

      <CategoryMins i={i} className="mt-3 border-t border-border/70 pt-3" />

      <div className="mt-4">
        <p className="mb-1 flex justify-between text-[11px] text-muted">
          <span>Subscription{sub != null && biddingDay(i, now) && <span className="num"> · {biddingDay(i, now)}</span>}</span>
          {sub == null && <span>{i.phase === "upcoming" ? "not open yet" : "not published"}</span>}
        </p>
        <SubMeter value={sub} />
      </div>
      {i.exchange === "NSE" && !isSme(i) && i.phase !== "upcoming" && <IpoSignalLine symbol={i.symbol} series={i.series} company={i.company} closed={i.phase === "closed"} />}

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

/** "day 2 of 3" while bidding (weekdays from the open to the close, IST), "final" once closed; null otherwise.
 * Day-1 7x means something different from day-3 7x (roadmap §B /ipos 4). */
export function biddingDay(i: Issue, now: number | null): string | null {
  if (!i.issue_start || !i.issue_end || now == null) return null;
  if (i.phase === "closed" || istAt(i.issue_end) <= now) return "final";
  const days: string[] = [];
  for (let t = Date.parse(`${i.issue_start.slice(0, 10)}T00:00:00Z`); t <= Date.parse(`${i.issue_end.slice(0, 10)}T00:00:00Z`); t += 86400000) {
    const wd = new Date(t).getUTCDay();
    if (wd !== 0 && wd !== 6) days.push(new Date(t).toISOString().slice(0, 10));
  }
  const today = days.indexOf(istDate(now));
  return today < 0 || days.length < 2 ? null : `day ${today + 1} of ${days.length}`;
}

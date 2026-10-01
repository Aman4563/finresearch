"use client";

import { BellRing, CalendarDays, ChartBar, Eye, FlaskConical, NotebookPen, Radio, Timer } from "lucide-react";
import Link from "next/link";
import { useMemo } from "react";

import { LinkButton } from "@/components/dashboard/link-button";
import { PortfolioStrip } from "@/components/dashboard/portfolio-strip";
import {
  Calendar, GettingStarted, JournalMini, type Limits, PlanTile, QuickActions, RecentRuns, WatchedSubscription, bindingWindow,
  buildEvents, planUsage,
} from "@/components/dashboard/panels";
import {
  LiveDot, SubMeter, TERMS, biddingOver, categoryMins, countdown, daysUntil, inr, int, istAt, lakh, lotCost, lotSourceText, times, useNow,
} from "@/components/ipo/lib";
import { IndexCards } from "@/components/markets/index-cards";
import { AlertFeed } from "@/components/monitor/alert-feed";
import { useWatchDetails } from "@/components/monitor/hooks";
import { Card, EmptyState, ErrorNote, InfoTip, Skeleton, SkeletonRows, Stat, cx } from "@/components/ui";
import { useLive } from "@/components/live";
import {
  type AlertItem, type Decision, type Issue, type Profile, type RunSummary, useApi, type WatchSummary,
} from "@/lib/api";

type Radar = { fetched_at: string; issues: Issue[]; errors: string[] };
const IST = "Asia/Kolkata";

export default function Dashboard() {
  const radar = useLive<Radar>("/api/ipos", { session: "ipo", everyMs: 60000, idleMs: 5 * 60000 });
  const watches = useApi<WatchSummary[]>("/api/watches", 60000);
  const alerts = useApi<AlertItem[]>("/api/alerts?limit=50", 60000);
  const runs = useApi<RunSummary[]>("/api/runs", 30000);
  const decisions = useApi<Decision[]>("/api/decisions");
  const limits = useApi<Limits>("/api/limits", 60000);
  const profile = useApi<Profile>("/api/profile");
  const details = useWatchDetails(watches.data);
  const now = useNow(30000);

  const issues = useMemo(() => radar.data?.issues ?? [], [radar.data]);
  const open = issues.filter((i) => i.phase === "open" || i.phase === "current");
  const closingToday = now == null ? [] : open.filter((i) => daysUntil(i.issue_end, now) === 0);
  const activeWatches = (watches.data ?? []).filter((w) => w.active);
  const unread = (alerts.data ?? []).filter((a) => !a.read_at);
  const running = (runs.data ?? []).filter((r) => r.status === "running" || r.status === "paused");
  const finished = (runs.data ?? []).filter((r) => r.status === "done");
  const plan = planUsage(limits.data);

  const events = useMemo(
    () => (now == null || !radar.data || !watches.data ? null : buildEvents(issues, watches.data, details.data ?? [], now)),
    [now, radar.data, watches.data, details.data, issues],
  );

  const name = profile.data?.display_name?.trim() || "Investor";
  const hour = now == null ? null : Number(new Date(now).toLocaleString("en-GB", { hour: "numeric", hour12: false, timeZone: IST }));
  const greet = hour == null ? "Welcome" : hour < 5 ? "Good evening" : hour < 12 ? "Good morning" : hour < 17 ? "Good afternoon" : "Good evening";
  const today = now == null ? "" : new Date(now).toLocaleDateString("en-IN", { weekday: "long", day: "numeric", month: "long", year: "numeric", timeZone: IST });

  // one line of what needs attention, most urgent first
  const attention: { text: string; href: string; tone: string }[] = [];
  if (closingToday.length && now != null) {
    const cut = istAt(closingToday[0].issue_end!);
    attention.push({
      text: cut > now ? `${closingToday.length} IPO${closingToday.length > 1 ? "s" : ""} close today, UPI cut-off in ${countdown(cut, now)}` : `${closingToday.length} IPO${closingToday.length > 1 ? "s" : ""} closed today`,
      href: "/ipos", tone: "text-warn",
    });
  }
  if (unread.length) attention.push({ text: `${unread.length} unread alert${unread.length > 1 ? "s" : ""}`, href: "/monitor", tone: "text-loss" });
  if (running.length) attention.push({ text: `${running.length} research run${running.length > 1 ? "s" : ""} in progress`, href: "/runs", tone: "text-info" });
  const bind = bindingWindow(plan);
  if (bind.used != null && bind.used >= 0.6) attention.push({ text: `${bind.label} plan window ${Math.round(bind.used * 100)}% used`, href: "/usage", tone: "text-warn" });
  const loaded = radar.data || watches.data || alerts.data || runs.data;

  return (
    <div className="space-y-6">
      <section className="relative overflow-hidden rounded-2xl border border-border bg-card p-5 shadow-card animate-fade-up sm:p-6">
        <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(600px_200px_at_100%_0%,color-mix(in_srgb,var(--brand)_14%,transparent),transparent_70%),radial-gradient(500px_200px_at_0%_100%,color-mix(in_srgb,var(--accent)_10%,transparent),transparent_70%)]" />
        <svg className="pointer-events-none absolute right-0 bottom-0 hidden h-24 w-72 text-brand/25 sm:block" viewBox="0 0 288 96" fill="none" aria-hidden>
          <path d="M0 80 L40 70 L70 76 L110 50 L140 58 L180 30 L210 40 L250 14 L288 20" stroke="currentColor" strokeWidth="2"
            pathLength={1} strokeDasharray={1} className="animate-draw" />
        </svg>
        <div className="relative flex flex-wrap items-end justify-between gap-4">
          <div className="min-w-0">
            <p className="text-xs font-medium uppercase tracking-wider text-brand">{today || " "}</p>
            <h1 className="mt-1 text-2xl font-semibold tracking-tight sm:text-3xl">
              {greet}, {profile.data ? name : <span className="inline-block h-7 w-28 align-middle"><Skeleton className="h-7 w-28" /></span>}
            </h1>
            <p className="mt-2 max-w-2xl text-sm text-muted">
              {!loaded ? "Checking what needs your attention…" : attention.length ? (
                <>
                  Needs your attention:{" "}
                  {attention.map((a, i) => (
                    <span key={a.text}>
                      {i > 0 && (i === attention.length - 1 ? " and " : ", ")}
                      <Link href={a.href} className={cx("font-medium underline-offset-2 hover:underline", a.tone)}>{a.text}</Link>
                    </span>
                  ))}
                  .
                </>
              ) : "All quiet: nothing needs you right now. Browse open IPOs or review your journal."}
            </p>
          </div>
          <div className="flex flex-wrap gap-2">
            <LinkButton href="/ipos" variant="primary" icon={<Radio className="size-3.5" />}>Open IPOs</LinkButton>
            <LinkButton href="/journal" icon={<NotebookPen className="size-3.5" />}>Journal</LinkButton>
          </div>
        </div>
      </section>

      <GettingStarted profileSet={!!profile.data?.display_name} researched={!!runs.data?.length} watched={!!watches.data?.length} />

      <PortfolioStrip />

      <div className="stagger grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-5">
        <Stat label="IPOs open now" format={int} value={radar.data ? open.filter((i) => !biddingOver(i, now)).length : null} href="/ipos" tone="gain"
          icon={<LiveDot className="size-1.5" />}
          // past the 5 PM cut-off today's closers are no longer "open" or "closing": say they closed
          hint={radar.data ? (closingToday.length
            ? (closingToday.some((i) => !biddingOver(i, now)) ? `${closingToday.length} closing today` : `${closingToday.length} closed today at 5 PM`)
            : "none closing today") : undefined}
          help={TERMS.upi} />
        <Stat label="Active watches" format={int} value={watches.data ? activeWatches.length : null} href="/monitor" icon={<Eye className="size-4" />}
          hint={watches.data ? `${activeWatches.filter((w) => w.kind === "ipo").length} IPO · ${activeWatches.filter((w) => w.kind === "stock").length} stock` : undefined} />
        <Stat label="Unread alerts" format={int} value={alerts.data ? unread.length : null} href="/monitor" icon={<BellRing className="size-4" />}
          tone={unread.length ? "loss" : "neutral"} hint={unread.length ? "tap to review" : "all caught up"} />
        <Stat label="Research runs" format={int} value={runs.data ? running.length : null} href="/runs" icon={<FlaskConical className="size-4" />} tone="info"
          hint={runs.data ? `in progress · ${finished.length} finished` : undefined} />
        <div className="col-span-2 md:col-span-1"><PlanTile limits={limits.data} now={now} /></div>
      </div>

      <QuickActions />

      <IndexCards />

      <div className="grid gap-4 lg:grid-cols-5 [&>*]:min-w-0">
        <Card className="lg:col-span-3" title="Today & next 14 days" icon={<CalendarDays className="size-4" />}
          subtitle="Bidding closes (UPI cut-off 5 PM IST), openings, allotments, listings and lock-ins. Tap a day to filter."
          help={TERMS.anchor}>
          <ErrorNote error={radar.error ?? watches.error} onRetry={() => { radar.reload(); watches.reload(); }} />
          {events && now != null ? <Calendar events={events.events} now={now} /> : !(radar.error || watches.error) && (
            <div className="space-y-3">
              <div className="flex gap-1.5 overflow-hidden">{Array.from({ length: 9 }, (_, i) => <Skeleton key={i} className="h-14 w-11 shrink-0" />)}</div>
              <SkeletonRows rows={5} />
            </div>
          )}
        </Card>
        <Card className="lg:col-span-2" title="Live subscription" icon={<ChartBar className="size-4" />}
          subtitle="Latest bid book of the IPOs you watch."
          actions={<Link href="/monitor" className="text-xs font-medium text-brand hover:underline">Monitor →</Link>}>
          <ErrorNote error={details.error} />
          {details.data ? <WatchedSubscription details={details.data} /> : !details.error && <Skeleton className="h-56" />}
        </Card>
      </div>

      <div className="grid gap-4 lg:grid-cols-2 [&>*]:min-w-0">
        <Card title="Latest alerts" icon={<BellRing className="size-4" />}
          actions={<Link href="/monitor" className="text-xs font-medium text-brand hover:underline">All alerts →</Link>}>
          <ErrorNote error={alerts.error} onRetry={alerts.reload} />
          {alerts.data ? <AlertFeed alerts={alerts.data} onRead={alerts.reload} limit={5} /> : !alerts.error && <SkeletonRows rows={4} />}
        </Card>
        <Card title="Recent research" icon={<FlaskConical className="size-4" />}
          help="Every run ends at a publish gate: the report is only published if every important figure is verified against its source."
          actions={<Link href="/runs" className="text-xs font-medium text-brand hover:underline">All runs →</Link>}>
          <ErrorNote error={runs.error} onRetry={runs.reload} />
          {runs.data ? <RecentRuns runs={runs.data} now={now} /> : !runs.error && <SkeletonRows rows={5} />}
        </Card>
      </div>

      <div className="grid gap-4 lg:grid-cols-3 [&>*]:min-w-0">
        <Card title="Decision journal" icon={<NotebookPen className="size-4" />}
          subtitle="Your apply / skip calls and how listings turned out."
          actions={<Link href="/journal" className="text-xs font-medium text-brand hover:underline">Journal →</Link>}>
          <ErrorNote error={decisions.error} onRetry={decisions.reload} />
          {decisions.data ? <JournalMini decisions={decisions.data} /> : !decisions.error && <Skeleton className="h-40" />}
        </Card>
        <Card className="lg:col-span-2" title="Closing soonest" icon={<Timer className="size-4" />}
          subtitle="Open issues by close date: cost to apply at the upper band and the live book."
          actions={<Link href="/ipos" className="text-xs font-medium text-brand hover:underline">Screener →</Link>}>
          {radar.data ? <ClosingSoon issues={open} now={now} /> : !radar.error && <SkeletonRows rows={5} />}
        </Card>
      </div>
    </div>
  );
}

/** "Lot ₹14,960 · min ₹14,960 · sHNI ₹2.09L · bHNI ₹10.02L" (upper band), with the category thresholds explained. */
function ApplyLine({ i }: { i: Issue }) {
  const cost = lotCost(i), cats = categoryMins(i), src = lotSourceText(i);
  if (!cost) {
    const failed = !!i.lot_note && /issue page|unavailable/i.test(i.lot_note);
    return <p className="text-[11px] text-muted" title={i.lot_note ?? undefined}>{failed ? "Lot unavailable (exchange fetch failed)" : "Lot not published yet"}</p>;
  }
  const shni = cats?.find((c) => c.key === "shni"), bhni = cats?.find((c) => c.key === "bhni");
  return (
    <p className="num flex flex-wrap items-center gap-x-1.5 text-[11px] text-muted" title={src?.title}>
      <span>Lot <span className="text-foreground">{inr(cost.lot)}</span></span>
      <span>· min <span className="text-foreground">{inr(cost.min)}</span>{cost.minLots > 1 ? ` (${cost.minLots} lots)` : ""}</span>
      {shni?.amount != null && <span className="hidden sm:inline">· sHNI {lakh(shni.amount)}</span>}
      {bhni?.amount != null && <span className="hidden sm:inline">· bHNI {lakh(bhni.amount)}</span>}
      <InfoTip>{TERMS.categories}</InfoTip>
    </p>
  );
}

function ClosingSoon({ issues, now }: { issues: Issue[]; now: number | null }) {
  const rows = [...issues].sort((a, b) => (a.issue_end ?? "").localeCompare(b.issue_end ?? "") ||
    (times(b.times_subscribed) ?? -1) - (times(a.times_subscribed) ?? -1)).slice(0, 7);
  if (!rows.length)
    return <EmptyState title="No issue is open right now">Upcoming issues are on the IPOs page with their opening dates.</EmptyState>;
  return (
    <ul className="stagger divide-y divide-border/70">
      {rows.map((i) => {
        const d = now == null ? null : daysUntil(i.issue_end, now);
        return (
          <li key={`${i.exchange}-${i.symbol}`} className="grid grid-cols-[1fr_auto] items-center gap-x-4 gap-y-1 py-2.5 sm:grid-cols-[1fr_10rem_5.5rem]">
            <div className="min-w-0">
              <p className="flex items-center gap-2 truncate text-sm font-medium">{!biddingOver(i, now) && <LiveDot />}<span className="truncate">{i.company}</span></p>
              <p className="num text-xs text-muted">{i.exchange} {i.symbol}{i.series === "SME" ? " · SME" : ""}</p>
              <ApplyLine i={i} />
            </div>
            <span className={cx("num text-right text-xs sm:order-last", d === 0 ? "font-medium text-warn" : "text-muted")}>
              {d == null ? "" : d === 0 ? (now! < istAt(i.issue_end!) ? `${countdown(istAt(i.issue_end!), now!)} left` : "closed") : `${d}d left`}
            </span>
            <SubMeter value={times(i.times_subscribed)} className="col-span-2 sm:col-span-1" />
          </li>
        );
      })}
    </ul>
  );
}

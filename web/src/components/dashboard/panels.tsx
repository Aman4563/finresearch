"use client";

import {
  BookOpen, CalendarDays, Check, ChevronRight, CircleHelp, Eye, FlaskConical, NotebookPen, Rocket, Search, Sparkles,
  UserRound, X,
} from "lucide-react";
import Link from "next/link";
import { type ReactNode, useEffect, useMemo, useState } from "react";

import { BarsChart, DonutChart, Gauge, Sparkline } from "@/components/charts";
import { LinkButton } from "@/components/dashboard/link-button";
import { KIND_LABEL } from "@/components/ipo/actions";
import {
  LiveDot, SUB_HELP, TERMS, categories, countdown, dayLabel, daysUntil, fmtX, istAt, istDate, timeIST,
} from "@/components/ipo/lib";
import { Badge, EmptyState, InfoTip, Segmented, cx } from "@/components/ui";
import type { Decision, Issue, RunSummary, WatchDetail, WatchSummary } from "@/lib/api";

// ------------------------------------------------------------------ plan window tile

export type Limits = {
  tiers: Record<string, { snapshot?: { five_hour_utilization?: number; five_hour_resets_at?: number; seven_day_utilization?: number } }>;
  now: number;
  ceilings: { five_hour: number };
};

export function planUsage(l: Limits | null) {
  const snap = l?.tiers?.claude_max?.snapshot;
  return { used: snap?.five_hour_utilization ?? null, resets: snap?.five_hour_resets_at ?? null, week: snap?.seven_day_utilization ?? null };
}

export function PlanTile({ limits, now }: { limits: Limits | null; now: number | null }) {
  const { used, resets, week } = planUsage(limits);
  return (
    <Link href="/usage" className="group relative block h-full overflow-hidden rounded-xl border border-border bg-card p-4 shadow-card transition duration-200 hover:-translate-y-0.5 hover:border-border-strong hover:shadow-glow">
      <p className="flex items-center gap-1 text-xs font-medium text-muted">
        Plan window
        <InfoTip>
          Research runs use your Claude plan. It has a rolling 5-hour window; FinResearch pauses runs before it reaches
          {limits ? ` ${Math.round(limits.ceilings.five_hour * 100)}%` : " the ceiling"} and resumes after the reset.
        </InfoTip>
      </p>
      <div className="mt-1 flex items-end justify-between gap-2">
        <div className="text-xs text-muted">
          <p>{resets && now && resets * 1000 > now ? <>resets in <span className="num text-foreground">{countdown(resets * 1000, now)}</span></> : "used, 5-h"}</p>
          {week != null && <p className="num mt-0.5">7-day {Math.round(week * 100)}%</p>}
        </div>
        <div className="-mb-1 shrink-0"><Gauge value={limits ? used : null} size={96} /></div>
      </div>
    </Link>
  );
}

// ------------------------------------------------------------------ calendar

type Ev = { day: string; at?: number; kind: "close" | "open" | "allot" | "list" | "lockin"; title: string; sub: string; href?: string; live?: boolean };

const EV_STYLE: Record<Ev["kind"], { label: string; cls: string; dot: string }> = {
  close: { label: "Bidding closes", cls: "bg-warn-soft text-warn", dot: "bg-warn" },
  open: { label: "Bidding opens", cls: "bg-info-soft text-info", dot: "bg-info" },
  allot: { label: "Allotment", cls: "bg-accent-soft text-accent", dot: "bg-accent" },
  list: { label: "Listing", cls: "bg-gain-soft text-gain", dot: "bg-gain" },
  lockin: { label: "Lock-in ends", cls: "bg-loss-soft text-loss", dot: "bg-loss" },
};

export function buildEvents(issues: Issue[], watches: WatchSummary[], details: WatchDetail[], now: number) {
  const today = istDate(now);
  const within = (d: string | null | undefined) => {
    const n = daysUntil(d, now);
    return n != null && n >= 0 && n <= 14;
  };
  const out: Ev[] = [];
  const seen = new Set<string>();
  const push = (e: Ev, key: string) => {
    if (!seen.has(key)) {
      seen.add(key);
      out.push(e);
    }
  };
  for (const i of issues) {
    const open = i.phase === "open" || i.phase === "current";
    if (open && within(i.issue_end))
      push({ day: i.issue_end!, at: istAt(i.issue_end!), kind: "close", title: i.company, sub: `${i.exchange} ${i.symbol}${i.series === "SME" ? " · SME" : ""}`, href: "/ipos", live: true }, `close:${i.symbol}`);
    if (i.phase === "upcoming" && within(i.issue_start))
      push({ day: i.issue_start!, at: istAt(i.issue_start!, "10:00"), kind: "open", title: i.company, sub: `${i.exchange} ${i.symbol}${i.series === "SME" ? " · SME" : ""}`, href: "/ipos" }, `open:${i.symbol}`);
  }
  for (const w of watches.filter((x) => x.kind === "ipo" && x.active)) {
    const name = w.company_name ?? w.nse_symbol;
    if (within(w.close_date)) push({ day: w.close_date!, at: istAt(w.close_date!), kind: "close", title: name, sub: `NSE ${w.nse_symbol} · watched`, href: `/monitor/${w.id}`, live: true }, `close:${w.nse_symbol}`);
    if (within(w.allotment_date)) push({ day: w.allotment_date!, kind: "allot", title: name, sub: "basis of allotment; check the registrar", href: `/monitor/${w.id}` }, `allot:${w.nse_symbol}`);
    if (within(w.listing_date))
      push({ day: w.listing_date!, at: istAt(w.listing_date!, "10:00"), kind: "list", title: name, sub: w.meta?.listing_confirmed ? "confirmed by NSE" : "expected date", href: `/monitor/${w.id}` }, `list:${w.nse_symbol}`);
  }
  for (const d of details) {
    for (const j of d.jobs.filter((x) => x.kind === "lockin" && x.status === "pending")) {
      const day = j.due_at.slice(0, 10);
      if (within(day)) push({ day, kind: "lockin", title: d.company_name ?? d.nse_symbol, sub: j.slot.split(":").pop() ?? "", href: `/monitor/${d.id}` }, `lock:${j.id}`);
    }
  }
  out.sort((a, b) => a.day.localeCompare(b.day) || (a.at ?? 0) - (b.at ?? 0));
  return { events: out, today };
}

export function Calendar({ events, now }: { events: Ev[]; now: number }) {
  const [sel, setSel] = useState<string | null>(null);
  const [all, setAll] = useState(false);
  const days = useMemo(() => {
    const base = Date.parse(`${istDate(now)}T00:00:00Z`);
    return Array.from({ length: 15 }, (_, i) => new Date(base + i * 86400000).toISOString().slice(0, 10));
  }, [now]);
  const count = (d: string) => events.filter((e) => e.day === d);
  const shown = sel ? events.filter((e) => e.day === sel) : events;
  const limited = all || sel ? shown : shown.slice(0, 8);
  const groups = new Map<string, Ev[]>();
  for (const e of limited) groups.set(e.day, [...(groups.get(e.day) ?? []), e]);

  return (
    <div>
      <div className="-mx-1 flex gap-1.5 overflow-x-auto px-1 pb-2" role="tablist" aria-label="Pick a day">
        {days.map((d, i) => {
          const evs = count(d);
          const active = sel === d;
          const wk = new Date(`${d}T00:00:00Z`).getUTCDay();
          return (
            <button key={d} type="button" role="tab" aria-selected={active} onClick={() => setSel(active ? null : d)}
              className={cx(
                "flex w-11 shrink-0 flex-col items-center rounded-lg py-1.5 text-[10px] ring-1 ring-inset transition",
                active ? "bg-brand text-brand-fg ring-brand" : i === 0 ? "bg-brand-soft text-foreground ring-brand/30" : "ring-border hover:bg-background-subtle",
                (wk === 0 || wk === 6) && !active && "opacity-60",
              )}>
              <span className={cx("uppercase", active ? "" : "text-muted")}>{i === 0 ? "Today" : dayLabel(d, { weekday: "short" })}</span>
              <span className="num text-sm font-semibold">{Number(d.slice(8))}</span>
              <span className="mt-0.5 flex h-1.5 gap-0.5">
                {evs.slice(0, 3).map((e, k) => <span key={k} className={cx("size-1.5 rounded-full", active ? "bg-brand-fg" : EV_STYLE[e.kind].dot)} />)}
              </span>
            </button>
          );
        })}
      </div>
      {limited.length === 0 ? (
        <EmptyState icon={<CalendarDays className="size-5" />} title={sel ? `Nothing on ${dayLabel(sel)}` : "Nothing in the next 14 days"}>
          {sel ? "Pick another day or tap it again to see all." : "No bidding, allotment or listing dates ahead. Browse IPOs to find the next issue."}
        </EmptyState>
      ) : (
        <div className="mt-2 space-y-3">
          {[...groups.entries()].map(([d, evs]) => {
            const n = daysUntil(d, now)!;
            return (
              <div key={d}>
                <p className="mb-1.5 text-[11px] font-medium uppercase tracking-wider text-muted">
                  {n === 0 ? "Today" : n === 1 ? "Tomorrow" : dayLabel(d, { weekday: "long", day: "numeric", month: "short" })}
                  {n > 1 && <span className="num normal-case tracking-normal"> · in {n} days</span>}
                </p>
                <ul className="stagger space-y-1.5">
                  {evs.map((e, k) => {
                    const left = e.at && e.at > now ? countdown(e.at, now) : null;
                    const body = (
                      <div className="flex items-center gap-3 rounded-lg border border-border/70 px-3 py-2 transition hover:border-border-strong hover:bg-card-hover">
                        <span className={cx("hidden w-28 shrink-0 rounded-md px-1.5 py-0.5 text-center text-[10px] font-medium sm:block", EV_STYLE[e.kind].cls)}>
                          {EV_STYLE[e.kind].label}
                        </span>
                        <span className={cx("w-1 self-stretch shrink-0 rounded-full sm:hidden", EV_STYLE[e.kind].dot)} aria-hidden />
                        <div className="min-w-0 flex-1">
                          <p className="flex items-center gap-1.5 truncate text-sm font-medium">
                            {e.live && n === 0 && <LiveDot />}
                            <span className="truncate">{e.title}</span>
                          </p>
                          <p className="truncate text-xs text-muted"><span className="sm:hidden">{EV_STYLE[e.kind].label} · </span>{e.sub}</p>
                        </div>
                        {left && n <= 1 && (
                          <span className={cx("num shrink-0 text-xs font-medium", e.kind === "close" ? "text-warn" : "text-muted")}>
                            {e.kind === "close" ? `${left} left` : `in ${left}`}
                          </span>
                        )}
                      </div>
                    );
                    return <li key={k}>{e.href ? <Link href={e.href}>{body}</Link> : body}</li>;
                  })}
                </ul>
              </div>
            );
          })}
          {!sel && shown.length > 8 && (
            <button type="button" onClick={() => setAll((a) => !a)} className="text-xs font-medium text-brand hover:underline">
              {all ? "Show fewer" : `Show all ${shown.length} events`}
            </button>
          )}
        </div>
      )}
    </div>
  );
}

// ------------------------------------------------------------------ watched subscription

export function WatchedSubscription({ details }: { details: WatchDetail[] }) {
  const withData = details.filter((d) => d.subscription.length);
  const [pick, setPick] = useState<string>("");
  const cur = withData.find((d) => String(d.id) === pick) ?? withData[0];
  if (!cur)
    return (
      <EmptyState icon={<Eye className="size-5" />} title="No live subscription to show"
        action={<LinkButton href="/monitor" icon={<Eye className="size-3.5" />}>Watch an IPO</LinkButton>}>
        Watch an IPO while it is open and its QIB, NII and retail demand appears here, refreshed six times a bidding day.
      </EmptyState>
    );
  const last = cur.subscription[cur.subscription.length - 1];
  const rows = categories(last).filter((c) => c.times != null).map((c) => ({ name: c.label, times: Number(c.times!.toFixed(2)) }));
  const trend = cur.subscription.map((s) => Number(s.total_times));
  return (
    <div>
      {withData.length > 1 && (
        <div className="mb-3 overflow-x-auto">
          <Segmented value={String(cur.id)} onChange={setPick} options={withData.map((d) => ({ value: String(d.id), label: d.nse_symbol }))} />
        </div>
      )}
      <div className="mb-2 flex items-center justify-between gap-3">
        <Link href={`/monitor/${cur.id}`} className="min-w-0 truncate text-sm font-medium hover:text-brand">{cur.company_name ?? cur.nse_symbol}</Link>
        <div className="flex shrink-0 items-center gap-2 text-xs text-muted">
          <Sparkline values={trend} width={64} height={22} />
          <span className="num">{dayLabel(last.as_of, { day: "numeric", month: "short" })} {timeIST(last.as_of)}</span>
        </div>
      </div>
      <BarsChart data={rows} x="name" layout="vertical" height={rows.length * 36 + 30} series={[{ key: "times", label: "Subscribed" }]}
        format={fmtX} reference={{ value: 1, label: "1x" }} colorBy={(r) => (Number(r.times) >= 1 ? "var(--gain)" : "var(--warn)")} />
      <p className="mt-2 flex items-center gap-1 text-[11px] text-muted">
        Green is fully subscribed (1x or more), amber is not yet. <InfoTip>{SUB_HELP} {TERMS.QIB}</InfoTip>
      </p>
    </div>
  );
}

// ------------------------------------------------------------------ runs

const RUN_ICON: Record<string, ReactNode> = {
  ipo_report: <Rocket className="size-3.5" />,
  stock_report: <FlaskConical className="size-3.5" />,
  fund_report: <FlaskConical className="size-3.5" />,
  bond_report: <FlaskConical className="size-3.5" />,
};

export function RecentRuns({ runs, now }: { runs: RunSummary[]; now: number | null }) {
  if (!runs.length)
    return (
      <EmptyState icon={<FlaskConical className="size-5" />} title="No research yet"
        action={<LinkButton href="/ipos" variant="primary" icon={<Rocket className="size-3.5" />}>Research an IPO</LinkButton>}>
        A research run collects the filings, fact-checks every figure against its source and writes a cited report.
      </EmptyState>
    );
  const ago = (iso: string | null) => {
    if (!iso || now == null) return "";
    const m = Math.round((now - Date.parse(iso)) / 60000);
    if (m < 60) return `${Math.max(1, m)}m ago`;
    if (m < 1440) return `${Math.round(m / 60)}h ago`;
    return `${Math.round(m / 1440)}d ago`;
  };
  return (
    <ul className="stagger divide-y divide-border/70">
      {runs.slice(0, 6).map((r) => {
        const done = Object.values(r.steps).reduce((a, b) => a + b, 0);
        const finished = r.steps.done ?? 0;
        return (
          <li key={r.id}>
            <Link href={r.status === "done" && r.has_report ? `/runs/${r.id}/report` : `/runs/${r.id}`}
              className="group -mx-2 flex items-center gap-3 rounded-lg px-2 py-2.5 transition hover:bg-card-hover">
              <span className="grid size-8 shrink-0 place-items-center rounded-lg bg-brand-soft text-brand">{RUN_ICON[r.kind] ?? <FlaskConical className="size-3.5" />}</span>
              <div className="min-w-0 flex-1">
                <p className="truncate text-sm font-medium">{r.company_name ?? r.company}</p>
                <p className="num text-xs text-muted">
                  #{r.id} · {KIND_LABEL[r.kind as keyof typeof KIND_LABEL] ?? r.kind} · {ago(r.finished_at ?? r.created_at)}
                  {r.status === "running" && done ? ` · ${finished}/${done} steps` : ""}
                </p>
              </div>
              <div className="flex shrink-0 flex-col items-end gap-1">
                <Badge status={r.status} />
                {r.final_gate ? (
                  <Badge tone={r.final_gate.ok ? "gain" : "loss"}>{r.final_gate.ok ? "gate passed" : "gate blocked"}</Badge>
                ) : r.status === "done" ? <Badge tone="neutral">no gate</Badge> : null}
              </div>
              <ChevronRight className="size-4 shrink-0 text-muted transition group-hover:translate-x-0.5 group-hover:text-foreground" />
            </Link>
          </li>
        );
      })}
    </ul>
  );
}

// ------------------------------------------------------------------ journal

export function JournalMini({ decisions }: { decisions: Decision[] }) {
  if (!decisions.length)
    return (
      <EmptyState icon={<NotebookPen className="size-5" />} title="No decisions yet">
        After a research run, press Suggest on the report to get an apply/skip call checked against your rules. Your
        decisions and their listing results collect here.
      </EmptyState>
    );
  const mix = (["APPLY", "APPLY-CONDITIONAL", "SKIP"] as const)
    .map((a) => ({ name: a === "APPLY-CONDITIONAL" ? "Conditional" : a === "APPLY" ? "Apply" : "Skip", value: decisions.filter((d) => d.action === a).length,
      color: a === "APPLY" ? "var(--gain)" : a === "SKIP" ? "var(--loss)" : "var(--warn)" }))
    .filter((m) => m.value);
  const gains = decisions
    .filter((d) => d.outcome?.listing_gain_pct != null)
    .map((d) => ({ name: (d.company_name ?? `#${d.run_id}`).split(" ")[0], gain: Number(d.outcome.listing_gain_pct) }));
  const followed = decisions.filter((d) => typeof d.outcome?.followed_suggestion === "boolean");
  return (
    <div className="space-y-4">
      <DonutChart data={mix} height={120} center={<div><p className="num text-lg font-semibold">{decisions.length}</p><p className="text-[10px] text-muted">decisions</p></div>} />
      {gains.length ? (
        <div>
          <p className="mb-1 text-xs font-medium text-muted">Listing gain vs issue price</p>
          <BarsChart data={gains} x="name" height={150} series={[{ key: "gain", label: "Listing gain" }]} format={(v) => `${v.toFixed(1)}%`}
            colorBy={(r) => (Number(r.gain) >= 0 ? "var(--gain)" : "var(--loss)")} />
        </div>
      ) : (
        <p className="rounded-lg bg-background-subtle/60 p-3 text-xs text-muted">
          Listing gains appear once a decided IPO lists: the monitor records the listing price automatically.
        </p>
      )}
      {followed.length > 0 && (
        <p className="text-xs text-muted">
          You followed the suggestion in <span className="num font-medium text-foreground">{followed.filter((d) => d.outcome.followed_suggestion).length}/{followed.length}</span> recorded decisions.
        </p>
      )}
    </div>
  );
}

// ------------------------------------------------------------------ quick actions and onboarding

export const openPalette = () => window.dispatchEvent(new KeyboardEvent("keydown", { key: "k", ctrlKey: true, metaKey: false }));

const HELP_KEY = "finresearch.help.seen";
const DISMISS_KEY = "finresearch.onboarding.dismissed";
export const markHelpSeen = () => { try { localStorage.setItem(HELP_KEY, "1"); } catch { /* storage blocked */ } };

export function QuickActions() {
  const items = [
    { href: "/ipos", icon: <Rocket className="size-4" />, label: "Research an IPO", sub: "Open and upcoming issues" },
    { href: "/monitor", icon: <Eye className="size-4" />, label: "Add a watch", sub: "Track subscription to listing" },
    { onClick: openPalette, icon: <Search className="size-4" />, label: "Search", sub: "Stocks, funds, bonds, pages", kbd: "⌘K" },
    { href: "/help", icon: <CircleHelp className="size-4" />, label: "Open help", sub: "Guides and glossary", onClick: markHelpSeen },
  ];
  return (
    <div className="stagger grid grid-cols-2 gap-2 lg:grid-cols-4">
      {items.map((i) => {
        const inner = (
          <>
            <span className="grid size-9 shrink-0 place-items-center rounded-lg bg-gradient-to-br from-brand/15 to-accent/15 text-brand ring-1 ring-inset ring-brand/20 transition group-hover:scale-105">
              {i.icon}
            </span>
            <span className="min-w-0 text-left">
              <span className="flex items-center gap-1.5 text-sm font-medium">
                {i.label}
                {i.kbd && <kbd className="num hidden rounded border border-border px-1 text-[10px] text-muted sm:inline">{i.kbd}</kbd>}
              </span>
              <span className="hidden truncate text-xs text-muted sm:block">{i.sub}</span>
            </span>
          </>
        );
        const cls = "group flex items-center gap-3 rounded-xl border border-border bg-card p-3 shadow-card transition duration-200 hover:-translate-y-0.5 hover:border-border-strong hover:shadow-glow";
        return i.href ? (
          <Link key={i.label} href={i.href} onClick={i.onClick} className={cls}>{inner}</Link>
        ) : (
          <button key={i.label} type="button" onClick={i.onClick} className={cls}>{inner}</button>
        );
      })}
    </div>
  );
}

export function GettingStarted({ profileSet, researched, watched }: { profileSet: boolean; researched: boolean; watched: boolean }) {
  const [state, setState] = useState<{ dismissed: boolean; help: boolean } | null>(null);
  useEffect(() => {
    let dismissed = false, help = false;
    try {
      dismissed = localStorage.getItem(DISMISS_KEY) === "1";
      help = localStorage.getItem(HELP_KEY) === "1";
    } catch { /* storage blocked: show the checklist */ }
    // eslint-disable-next-line react-hooks/set-state-in-effect -- read saved progress after hydration
    setState({ dismissed, help });
  }, []);
  if (!state) return null;
  const steps = [
    { done: profileSet, label: "Set up your profile", sub: "Your name, capital per IPO and risk appetite shape every suggestion.", href: "/profile", icon: <UserRound className="size-4" /> },
    { done: researched, label: "Run your first research", sub: "Pick an open IPO and press Research.", href: "/ipos", icon: <FlaskConical className="size-4" /> },
    { done: watched, label: "Watch an IPO", sub: "Get alerts from bidding to listing.", href: "/monitor", icon: <Eye className="size-4" /> },
    { done: state.help, label: "Read the help guide", sub: "Two minutes on how FinResearch checks its facts.", href: "/help", icon: <BookOpen className="size-4" />, onClick: markHelpSeen },
  ];
  const n = steps.filter((s) => s.done).length;
  if (state.dismissed || n === steps.length) return null;
  const dismiss = () => {
    try { localStorage.setItem(DISMISS_KEY, "1"); } catch { /* storage blocked */ }
    setState({ ...state, dismissed: true });
  };
  return (
    <section className="relative overflow-hidden rounded-xl border border-brand/30 bg-card p-4 shadow-card animate-fade-up sm:p-5">
      <span className="pointer-events-none absolute -top-16 -right-16 size-48 rounded-full bg-brand/10 blur-2xl" />
      <div className="relative mb-3 flex items-start justify-between gap-3">
        <div>
          <h2 className="flex items-center gap-2 text-sm font-semibold"><Sparkles className="size-4 text-brand" /> Getting started</h2>
          <p className="mt-0.5 text-xs text-muted"><span className="num">{n}</span> of {steps.length} done</p>
        </div>
        <button type="button" onClick={dismiss} aria-label="Hide getting started" title="Hide"
          className="grid size-7 place-items-center rounded-md text-muted transition hover:bg-background-subtle hover:text-foreground">
          <X className="size-4" />
        </button>
      </div>
      <div className="relative mb-4 h-1.5 overflow-hidden rounded-full bg-background-subtle">
        <div className="h-full rounded-full bg-gradient-to-r from-brand to-accent transition-[width] duration-700" style={{ width: `${(n / steps.length) * 100}%` }} />
      </div>
      <ol className="relative grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
        {steps.map((s) => (
          <li key={s.label}>
            <Link href={s.href} onClick={s.onClick}
              className={cx("flex h-full items-start gap-3 rounded-lg border p-3 transition hover:border-border-strong hover:bg-card-hover", s.done ? "border-gain/30 bg-gain-soft/40" : "border-border")}>
              <span className={cx("grid size-7 shrink-0 place-items-center rounded-full", s.done ? "bg-gain text-white" : "bg-brand-soft text-brand")}>
                {s.done ? <Check className="size-4" /> : s.icon}
              </span>
              <span className="min-w-0">
                <span className={cx("block text-sm font-medium", s.done && "text-muted line-through")}>{s.label}</span>
                <span className="block text-xs text-muted">{s.sub}</span>
              </span>
            </Link>
          </li>
        ))}
      </ol>
    </section>
  );
}

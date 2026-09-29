"use client";

import { BellRing, CalendarClock, Eye, Info, Plus, Radar } from "lucide-react";
import Link from "next/link";
import { useMemo, useState } from "react";

import { countdown, daysUntil, int, useNow } from "@/components/ipo/lib";
import { AlertFeed } from "@/components/monitor/alert-feed";
import { WatchCard, jobLabel } from "@/components/monitor/watch-card";
import {
  Button, Callout, Card, EmptyState, ErrorNote, Field, PageHeader, Segmented, Skeleton, Stat, cx, inputClass,
} from "@/components/ui";
import { api, type AlertItem, type Company, useApi, type WatchSummary } from "@/lib/api";

export default function Monitor() {
  const watches = useApi<WatchSummary[]>("/api/watches", 60000);
  const alerts = useApi<AlertItem[]>("/api/alerts", 60000);
  const companies = useApi<Company[]>("/api/companies");
  const now = useNow(30000);
  const [slug, setSlug] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [added, setAdded] = useState<string | null>(null);
  const [show, setShow] = useState<"active" | "all">("active");
  const [alertView, setAlertView] = useState<"unread" | "all">("all");

  const watchable = (companies.data ?? []).filter((c) => c.nse_symbol && (c.kind === "ipo_report" || c.kind === "stock_report"));

  const add = async () => {
    if (!slug) return;
    // a listed stock gets the daily after-close watch, anything else the IPO timeline
    const co = companies.data?.find((c) => c.slug === slug);
    const kind = co?.kind === "stock_report" ? "stock" : "ipo";
    setBusy(true);
    try {
      await api("/api/watches", { method: "POST", body: JSON.stringify({ company: slug, kind }) });
      setError(null);
      setAdded(co?.name ?? slug);
      setSlug("");
      watches.reload();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const list = useMemo(() => (watches.data ?? []).filter((w) => show === "all" || w.active), [watches.data, show]);
  const kpi = useMemo(() => {
    const active = (watches.data ?? []).filter((w) => w.active);
    const next = active
      .map((w) => w.next_check)
      .filter((n): n is NonNullable<typeof n> => !!n)
      .sort((a, b) => a.due_at.localeCompare(b.due_at))[0];
    const bidding = now == null ? 0 : active.filter((w) => w.kind === "ipo" && (daysUntil(w.open_date, now) ?? 1) <= 0 && (daysUntil(w.close_date, now) ?? -1) >= 0).length;
    return { active: active.length, bidding, next, stopped: (watches.data ?? []).length - active.length };
  }, [watches.data, now]);
  const unread = (alerts.data ?? []).filter((a) => !a.read_at);
  const shownAlerts = alertView === "unread" ? unread : alerts.data ?? [];

  return (
    <div className="space-y-6">
      <PageHeader
        icon={<BellRing className="size-5" />}
        eyebrow="Workspace"
        title="Monitor"
        description="Watched IPOs and stocks: scheduled checks from bidding to listing and lock-ins, with alerts when something changes."
      />

      <div className="stagger grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Stat label="Active watches" format={int} value={watches.data ? kpi.active : null} icon={<Eye className="size-4" />}
          hint={kpi.stopped ? `${kpi.stopped} stopped` : "being checked"} />
        <Stat label="Bidding now" format={int} value={watches.data ? kpi.bidding : null} icon={<Radar className="size-4" />} tone="gain"
          hint="watched IPOs open today" />
        <Stat label="Unread alerts" format={int} value={alerts.data ? unread.length : null} icon={<BellRing className="size-4" />}
          tone={unread.length ? "loss" : "neutral"} hint={alerts.data ? `${alerts.data.length} in total` : undefined} />
        <Stat label="Next check" icon={<CalendarClock className="size-4" />} tone="info"
          display={<span className="num">{!watches.data ? "—" : kpi.next && now != null ? countdown(Date.parse(kpi.next.due_at), now) : "none"}</span>}
          hint={kpi.next ? jobLabel(kpi.next.kind) : "nothing scheduled"} />
      </div>

      <div className="grid gap-4 lg:grid-cols-3 [&>*]:min-w-0">
        <Card className="lg:col-span-2" title="Add a watch" icon={<Plus className="size-4" />}
          subtitle="IPOs get the full timeline; listed stocks get a daily after-close check.">
          <ErrorNote error={companies.error} onRetry={companies.reload} />
          <div className="flex flex-col gap-3 sm:flex-row sm:items-end">
            <div className="flex-1">
              <Field label="Company from your research store">
                <select className={cx(inputClass, "w-full")} value={slug} onChange={(e) => setSlug(e.target.value)} disabled={!companies.data}>
                  <option value="">{companies.data ? "Choose a company…" : "Loading…"}</option>
                  {watchable.map((c) => (
                    <option key={c.slug} value={c.slug}>
                      {c.name} {c.kind === "stock_report" ? "(listed)" : "(IPO)"}
                    </option>
                  ))}
                </select>
              </Field>
            </div>
            <Button size="md" onClick={add} disabled={!slug || busy} icon={<Eye className="size-4" />}>
              {busy ? "Adding…" : "Watch"}
            </Button>
          </div>
          {companies.data && !watchable.length && (
            <p className="mt-2 text-xs text-muted">
              Nothing to watch yet: add an IPO from <Link href="/ipos" className="text-brand hover:underline">IPOs</Link> or a stock from{" "}
              <Link href="/stocks" className="text-brand hover:underline">Stocks</Link> first.
            </p>
          )}
          {added && <p className="mt-2 text-xs text-gain animate-fade-in">Now watching {added}. Its checks are planned on the next monitor pass.</p>}
          <div className="mt-3"><ErrorNote error={error} /></div>
        </Card>
        <Callout tone="info" icon={<Info className="size-4" />} title="How monitoring works">
          <p className="text-xs leading-relaxed">
            Checks run inside <code className="num">finresearch serve</code> (or <code className="num">finresearch monitor run</code>):
            subscription six times a bidding day, allotment (T+1), listing open and close (T+3; retried until NSE lists the
            stock) and anchor lock-ins (30 and 90 days). Dates after the close are expected dates until NSE confirms them.
          </p>
        </Callout>
      </div>

      <section>
        <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
          <h2 className="text-sm font-semibold tracking-tight">Watches</h2>
          <Segmented value={show} onChange={setShow} options={[{ value: "active", label: "Active" }, { value: "all", label: "All" }]} />
        </div>
        <ErrorNote error={watches.error} onRetry={watches.reload} />
        {!watches.data && !watches.error ? (
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">{[0, 1, 2].map((i) => <Skeleton key={i} className="h-72 rounded-xl" />)}</div>
        ) : watches.data && list.length === 0 ? (
          <EmptyState icon={<Eye className="size-5" />} title={watches.data.length ? "No active watches" : "You are not watching anything yet"}>
            {watches.data.length
              ? "Switch to All to see stopped watches, or pick a company above to watch it."
              : "Pick a researched IPO above and press Watch: FinResearch tracks its subscription, your rules, allotment and listing for you."}
          </EmptyState>
        ) : (
          <div className="stagger grid gap-3 md:grid-cols-2 xl:grid-cols-3">
            {list.map((w) => <WatchCard key={w.id} w={w} now={now} onChange={watches.reload} onError={setError} />)}
          </div>
        )}
      </section>

      <Card title="Alerts" icon={<BellRing className="size-4" />} subtitle="Newest first, grouped by day. Red needs action, amber is a warning, blue is for information."
        actions={<Segmented value={alertView} onChange={setAlertView} options={[
          { value: "all", label: "All" }, { value: "unread", label: `Unread${unread.length ? ` (${unread.length})` : ""}` },
        ]} />}>
        <ErrorNote error={alerts.error} onRetry={alerts.reload} />
        {!alerts.data && !alerts.error ? <div className="space-y-2">{[0, 1, 2].map((i) => <Skeleton key={i} className="h-16" />)}</div> : alerts.data && (
          <AlertFeed alerts={shownAlerts} onRead={alerts.reload}
            {...(alertView === "unread" && alerts.data.length ? { emptyTitle: "All caught up", emptyHint: "Every alert has been read." } : {})} />
        )}
      </Card>
    </div>
  );
}

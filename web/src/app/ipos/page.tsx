"use client";

import {
  BarChart3, Building2, CalendarClock, Flame, LayoutGrid, List, RefreshCw, Rocket, Search, SearchX, Timer, Users,
} from "lucide-react";
import Link from "next/link";
import { useEffect, useMemo, useState } from "react";

import { LinkButton } from "@/components/dashboard/link-button";
import { KIND_LABEL, ResearchButton } from "@/components/ipo/actions";
import { BaseRatesCard } from "@/components/ipo/base-rates";
import { IssueCard, NoLot, PHASE_TONE, ResearchCell, issueTiming, windowText } from "@/components/ipo/issue-card";
import {
  LiveDot, SUB_HELP, SubMeter, TERMS, categories, categoryMins, daysUntil, fmtX, inr, int, isSme, lakh, lotCost,
  lotSourceText, parseBand, times, useNow,
} from "@/components/ipo/lib";
import { useWatchDetails } from "@/components/monitor/hooks";
import { BarsChart } from "@/components/charts";
import {
  Badge, Button, Card, EmptyState, ErrorNote, InfoTip, PageHeader, Segmented, Skeleton, Stat, Table, cx, inputClass,
} from "@/components/ui";
import { LiveStamp, useLive } from "@/components/live";
import { API_URL, type Company, type Issue, type WatchSummary, useApi } from "@/lib/api";

type Radar = { fetched_at: string; issues: Issue[]; errors: string[]; notes?: string[] };
type Phase = "all" | "open" | "upcoming" | "closed";
type Exchange = "all" | "NSE" | "BSE";
type Board = "all" | "main" | "sme";
type Sort = "close" | "sub" | "cost" | "name";
type View = "cards" | "table";

const VIEW_KEY = "finresearch.ipos.view";
const phaseOf = (i: Issue) => (i.phase === "current" ? "open" : i.phase);

export default function IposPage() {
  // subscription moves during bidding hours: refresh every minute then, every 5 minutes otherwise
  const radar = useLive<Radar>("/api/ipos", { session: "ipo", everyMs: 60000, idleMs: 5 * 60000 });
  const companies = useApi<Company[]>("/api/companies");
  const watches = useApi<WatchSummary[]>("/api/watches");
  const details = useWatchDetails(watches.data);
  const now = useNow(30000);

  const [q, setQ] = useState("");
  const [phase, setPhase] = useState<Phase>("all");
  const [exchange, setExchange] = useState<Exchange>("all");
  const [board, setBoard] = useState<Board>("all");
  const [sort, setSort] = useState<Sort>("close");
  const [view, setView] = useState<View>("cards");
  const [refreshing, setRefreshing] = useState(false);
  const [more, setMore] = useState(false);
  const [refreshed, setRefreshed] = useState<Radar | null>(null);
  const [refreshError, setRefreshError] = useState<string | null>(null);

  useEffect(() => {
    try {
      const v = localStorage.getItem(VIEW_KEY);
      // eslint-disable-next-line react-hooks/set-state-in-effect -- restore the saved view after hydration
      if (v === "table" || v === "cards") setView(v);
    } catch { /* storage blocked */ }
  }, []);
  const changeView = (v: View) => {
    setView(v);
    try { localStorage.setItem(VIEW_KEY, v); } catch { /* storage blocked */ }
  };

  const data = refreshed && radar.data && refreshed.fetched_at > radar.data.fetched_at ? refreshed : radar.data;
  const issues = useMemo(() => data?.issues ?? [], [data]);

  const refresh = async () => {
    setRefreshing(true);
    try {
      const r = await fetch(`${API_URL}/api/ipos?refresh=true`, { cache: "no-store" });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      setRefreshed(await r.json());
      setRefreshError(null);
    } catch (e) {
      setRefreshError(`Could not refresh from NSE/BSE: ${(e as Error).message}`);
    } finally {
      setRefreshing(false);
    }
  };

  const kpi = useMemo(() => {
    const open = issues.filter((i) => phaseOf(i) === "open");
    return {
      open: open.length,
      closingToday: now == null ? null : open.filter((i) => daysUntil(i.issue_end, now) === 0).length,
      upcoming: issues.filter((i) => i.phase === "upcoming").length,
      hot: open.filter((i) => (times(i.times_subscribed) ?? 0) >= 1).length,
      withSub: open.filter((i) => times(i.times_subscribed) != null).length,
      researched: issues.filter((i) => i.latest_run).length,
    };
  }, [issues, now]);

  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase();
    const r = issues.filter(
      (i) =>
        (phase === "all" || phaseOf(i) === phase) &&
        (exchange === "all" || i.exchange === exchange) &&
        (board === "all" || (board === "sme") === isSme(i)) &&
        (!needle || i.company.toLowerCase().includes(needle) || i.symbol.toLowerCase().includes(needle)),
    );
    const cmp: Record<Sort, (a: Issue, b: Issue) => number> = {
      close: (a, b) => (a.issue_end ?? "9").localeCompare(b.issue_end ?? "9"),
      sub: (a, b) => (times(b.times_subscribed) ?? -1) - (times(a.times_subscribed) ?? -1),
      cost: (a, b) => (lotCost(a)?.min ?? Infinity) - (lotCost(b)?.min ?? Infinity),
      name: (a, b) => a.company.localeCompare(b.company),
    };
    const order = { open: 0, upcoming: 1, closed: 2 } as Record<string, number>;
    return [...r].sort((a, b) => (sort === "close" ? order[phaseOf(a)] - order[phaseOf(b)] || cmp.close(a, b) : cmp[sort](a, b)));
  }, [issues, q, phase, exchange, board, sort]);

  const leaderboard = useMemo(
    () =>
      issues
        .filter((i) => phaseOf(i) === "open" && times(i.times_subscribed) != null)
        .map((i) => ({ name: i.symbol, times: Number(times(i.times_subscribed)!.toFixed(2)) }))
        .sort((a, b) => b.times - a.times)
        .slice(0, 10),
    [issues],
  );

  const catRows = useMemo(
    () =>
      (details.data ?? [])
        .filter((w) => w.subscription.length)
        .map((w) => {
          const c = categories(w.subscription[w.subscription.length - 1]);
          const g = (k: string) => c.find((x) => x.key === k)?.times ?? null;
          return { name: w.nse_symbol, qib: g("qib"), nii: g("nii"), retail: g("retail"), total: g("total") };
        }),
    [details.data],
  );

  const filtered = phase !== "all" || exchange !== "all" || board !== "all" || q;
  const reset = () => { setPhase("all"); setExchange("all"); setBoard("all"); setQ(""); };

  return (
    <div className="space-y-6">
      <PageHeader
        icon={<Rocket className="size-5" />}
        eyebrow="Research"
        title="IPOs"
        description="Open and upcoming issues on NSE (mainboard and SME) and BSE SME, with lot costs, live subscription and one-click research."
        actions={
          <>
            <Button variant="secondary" onClick={refresh} disabled={refreshing}
              icon={<RefreshCw className={cx("size-3.5", refreshing && "animate-spin")} />}>
              {refreshing ? "Refreshing…" : "Refresh"}
            </Button>
          </>
        }
      />
      <LiveStamp session="ipo" live={radar.live} status={radar.status} updatedAt={radar.updatedAt} everyMs={60000}
        asOf={data?.fetched_at} asOfLabel="Exchange data fetched" className="-mt-4 mb-5" />

      <div className="stagger grid grid-cols-2 gap-3 lg:grid-cols-5">
        <Stat label="Open now" format={int} value={radar.data ? kpi.open : null} icon={<LiveDot className="size-1.5" />} tone="gain"
          hint="accepting bids" />
        <Stat label="Closing today" format={int} value={radar.data ? kpi.closingToday : null} icon={<Timer className="size-4" />} tone="warn"
          hint="UPI cut-off 5 PM IST" help={TERMS.upi} />
        <Stat label="Upcoming" format={int} value={radar.data ? kpi.upcoming : null} icon={<CalendarClock className="size-4" />} tone="info"
          hint="opening soon" />
        <Stat label="Oversubscribed" format={int} value={radar.data ? kpi.hot : null} icon={<Flame className="size-4" />} tone="accent"
          hint={radar.data ? `of ${kpi.withSub} with live data` : undefined} help={SUB_HELP} />
        <Stat label="Researched" format={int} value={radar.data ? kpi.researched : null} icon={<Building2 className="size-4" />}
          hint="have a report run" />
      </div>

      <ErrorNote error={radar.error} onRetry={radar.reload} />
      <ErrorNote error={refreshError} onRetry={refresh} />
      {data?.errors.map((e) => (
        <ErrorNote key={e} error={e.startsWith("bse:") ? `BSE: ${e.slice(5)}` : `NSE: ${e}`} onRetry={refresh} />
      ))}

      <Card
        title="Screener"
        subtitle="Filter, search and sort every issue. Costs use the upper price band (the cut-off price retail bids at); lots come from NSE's issue page, checked against BSE."
        icon={<Search className="size-4" />}
      >
        <div className="mb-4 flex flex-col gap-3">
          <div className="flex flex-wrap items-center gap-2">
            <div className="relative min-w-0 flex-1 basis-56">
              <Search className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-muted" />
              <input className={cx(inputClass, "w-full pl-9")} placeholder="Search company or symbol…" value={q}
                onChange={(e) => setQ(e.target.value)} aria-label="Search issues" />
            </div>
            <label className="flex items-center gap-2 text-xs text-muted">
              Sort
              <select className={cx(inputClass, "h-8 text-xs")} value={sort} onChange={(e) => setSort(e.target.value as Sort)}>
                <option value="close">Closing soonest</option>
                <option value="sub">Most subscribed</option>
                <option value="cost">Cheapest to apply</option>
                <option value="name">Name</option>
              </select>
            </label>
            <Segmented value={view} onChange={changeView} options={[
              { value: "cards", label: <span className="inline-flex items-center gap-1"><LayoutGrid className="size-3.5" /> Cards</span> },
              { value: "table", label: <span className="inline-flex items-center gap-1"><List className="size-3.5" /> Table</span> },
            ]} />
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Segmented value={phase} onChange={setPhase} options={[
              { value: "all", label: "All" }, { value: "open", label: "Open" }, { value: "upcoming", label: "Upcoming" },
              { value: "closed", label: "Closed" },
            ]} />
            <Segmented value={exchange} onChange={setExchange} options={[
              { value: "all", label: "NSE + BSE" }, { value: "NSE", label: "NSE" }, { value: "BSE", label: "BSE" },
            ]} />
            <Segmented value={board} onChange={setBoard} options={[
              { value: "all", label: "All boards" }, { value: "main", label: "Mainboard" }, { value: "sme", label: "SME" },
            ]} />
            <InfoTip>
              Mainboard IPOs are larger companies with a ₹14–15k minimum bid. SME IPOs are small companies on NSE Emerge or
              BSE SME: fewer disclosures, thin trading after listing, and a much larger minimum application (usually two lots).
            </InfoTip>
            {data && (
              <span className="ml-auto text-xs text-muted">
                <span className="num">{rows.length}</span> of <span className="num">{issues.length}</span> issues
              </span>
            )}
          </div>
        </div>

        {!data && !radar.error ? (
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
            {Array.from({ length: 6 }, (_, i) => <Skeleton key={i} className="h-60 rounded-xl" />)}
          </div>
        ) : data && issues.length === 0 ? (
          <EmptyState icon={<Rocket className="size-5" />} title="No open or upcoming issues right now"
            action={<Button variant="secondary" onClick={refresh}>Check again</Button>}>
            NSE and BSE have nothing open or scheduled. New issues usually appear a few days before they open; check back
            tomorrow or research a listed stock meanwhile.
          </EmptyState>
        ) : data && rows.length === 0 ? (
          <EmptyState icon={<SearchX className="size-5" />} title="No issue matches these filters"
            action={<Button variant="secondary" onClick={reset}>Clear filters</Button>}>
            Try another search or widen the phase, exchange or board filters.
          </EmptyState>
        ) : view === "cards" ? (
          <div key={`${phase}${exchange}${board}${sort}`} className="stagger grid gap-3 sm:grid-cols-2 xl:grid-cols-3 [&>*]:min-w-0">
            {(more ? rows : rows.slice(0, 12)).map((i) => <IssueCard key={`${i.exchange}-${i.phase}-${i.symbol}`} i={i} now={now} />)}
          </div>
        ) : (
          <IssueTable rows={rows} now={now} />
        )}
        {view === "cards" && rows.length > 12 && (
          <div className="mt-4 flex justify-center">
            <Button variant="secondary" onClick={() => setMore((m) => !m)}>
              {more ? "Show fewer" : `Show all ${rows.length} issues`}
            </Button>
          </div>
        )}
        {filtered && rows.length > 0 && (
          <button type="button" onClick={reset} className="mt-3 text-xs text-muted underline-offset-2 hover:text-foreground hover:underline">
            Clear filters
          </button>
        )}
      </Card>

      <div className="grid gap-4 lg:grid-cols-2 [&>*]:min-w-0">
        <Card title="Subscription leaderboard" subtitle="Open issues by times subscribed (NSE live book). The dashed line is 1x."
          icon={<BarChart3 className="size-4" />} help={SUB_HELP}>
          {!data ? <Skeleton className="h-64" /> : leaderboard.length ? (
            <BarsChart data={leaderboard} x="name" layout="vertical" height={Math.max(160, leaderboard.length * 30 + 30)}
              series={[{ key: "times", label: "Subscribed" }]} format={fmtX} reference={{ value: 1, label: "1x" }}
              colorBy={(r) => (Number(r.times) >= 1 ? "var(--gain)" : "var(--warn)")} />
          ) : (
            <EmptyState title="No live subscription figures yet">
              NSE publishes the bid book once an issue opens. BSE SME figures are not in the list; open the issue on BSE.
            </EmptyState>
          )}
        </Card>
        <Card title="By investor category" subtitle="Latest snapshot for the IPOs you watch (QIB, NII, retail)."
          icon={<Users className="size-4" />}
          help={<>{TERMS.QIB} {TERMS.NII}</>}
          actions={<Link href="/monitor" className="text-xs font-medium text-brand hover:underline">Monitor →</Link>}>
          {details.error ? <ErrorNote error={details.error} /> : !details.data ? <Skeleton className="h-64" /> : catRows.length ? (
            <BarsChart data={catRows} x="name" height={260} format={fmtX} reference={{ value: 1, label: "1x" }}
              series={[
                { key: "qib", label: "QIB", color: "var(--chart-2)" },
                { key: "nii", label: "NII", color: "var(--chart-3)" },
                { key: "retail", label: "Retail", color: "var(--chart-1)" },
              ]} />
          ) : (
            <EmptyState icon={<Users className="size-5" />} title="Watch an IPO to see its categories"
              action={<LinkButton href="/monitor">Add a watch</LinkButton>}>
              NSE only lists the total here. Watch an IPO and FinResearch records QIB, NII and retail demand six times a bidding day.
            </EmptyState>
          )}
        </Card>
      </div>

      <BaseRatesCard />

      <Card title="Researched companies" subtitle="Everything in your research store, with the kind of report it gets."
        icon={<Building2 className="size-4" />} padded>
        <ErrorNote error={companies.error} onRetry={companies.reload} />
        {!companies.data && !companies.error ? <SkeletonTable /> : companies.data && companies.data.length === 0 ? (
          <EmptyState title="Nothing researched yet">
            Pick an open issue above and press Research, or add a stock, fund or bond from its page.
          </EmptyState>
        ) : companies.data ? (
          <Table>
            <thead>
              <tr>
                <th>Company</th>
                <th>NSE</th>
                <th className="!text-right">Documents</th>
                <th>Kind</th>
                <th>Latest run</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {companies.data.map((c) => (
                <tr key={c.slug}>
                  <td className="max-w-72 truncate font-medium">{c.name}</td>
                  <td className="num text-xs text-muted">{c.nse_symbol ?? "—"}</td>
                  <td className="num text-right">{c.documents}</td>
                  <td><Badge tone={c.kind === "ipo_report" ? "brand" : c.kind === "stock_report" ? "info" : "accent"}>{KIND_LABEL[c.kind] ?? c.kind}</Badge></td>
                  <td>
                    {c.latest_run ? (
                      <Link className="text-xs font-medium text-brand hover:underline" href={`/runs/${c.latest_run}`}>Run #{c.latest_run}</Link>
                    ) : <span className="text-xs text-muted">none yet</span>}
                  </td>
                  <td className="text-right"><ResearchButton slug={c.slug} kind={c.kind} label={c.name} variant="secondary" /></td>
                </tr>
              ))}
            </tbody>
          </Table>
        ) : null}
      </Card>
    </div>
  );
}

function SkeletonTable() {
  return <div className="space-y-2">{Array.from({ length: 4 }, (_, i) => <Skeleton key={i} className="h-9" />)}</div>;
}

function IssueTable({ rows, now }: { rows: Issue[]; now: number | null }) {
  return (
    <Table>
      <thead>
        <tr>
          <th>Issue</th>
          <th>Window</th>
          <th className="!text-right"><span className="inline-flex items-center gap-1">Price band <InfoTip>{TERMS.band}</InfoTip></span></th>
          <th className="!text-right"><span className="inline-flex items-center gap-1">Lot · min. invest <InfoTip>{TERMS.lot}</InfoTip></span></th>
          <th className="!text-right"><span className="inline-flex items-center gap-1">Retail max · sHNI · bHNI min <InfoTip>{TERMS.categories}</InfoTip></span></th>
          <th className="w-36">Subscribed</th>
          <th>Research</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((i) => {
          const band = parseBand(i.price_band), cost = lotCost(i), t = issueTiming(i, now), cats = categoryMins(i);
          const open = phaseOf(i) === "open";
          return (
            <tr key={`${i.exchange}-${i.phase}-${i.symbol}`}>
              <td>
                <div className="flex items-center gap-2 font-medium">
                  {open && <LiveDot />}
                  <span className="max-w-56 truncate">{i.company}</span>
                </div>
                <div className="mt-0.5 flex items-center gap-1.5 text-xs text-muted">
                  <span className="num">{i.exchange} {i.symbol}</span>
                  <Badge tone={PHASE_TONE[i.phase]}>{phaseOf(i)}</Badge>
                  <Badge tone={isSme(i) ? "accent" : "brand"}>{isSme(i) ? "SME" : "Main"}</Badge>
                </div>
              </td>
              <td className="text-xs">
                <div className="num">{windowText(i)}</div>
                <div className={cx("num", t.urgent ? "font-medium text-warn" : "text-muted")}>{t.text}</div>
              </td>
              <td className="num text-right" title={i.price_band_note ?? undefined}>
                {band ? `₹${band[0]}–${band[1]}` : "—"}
                {i.price_band_note && <span className="ml-0.5 text-warn">*</span>}
              </td>
              <td className="num text-right">
                {cost ? inr(cost.lot) : <NoLot i={i} />}
                {cost && (
                  <div className="text-[10px] text-muted" title={lotSourceText(i)?.title}>
                    {i.lot_size} sh · min {inr(cost.min)}{cost.minLots > 1 ? ` (${cost.minLots} lots)` : ""}
                  </div>
                )}
              </td>
              <td className="num text-right text-xs">
                {cats ? (
                  <>
                    <div>{cats.map((c) => (c.amount == null ? "—" : lakh(c.amount))).join(" · ")}</div>
                    <div className="text-[10px] text-muted">{cats.map((c) => (c.lots == null ? "—" : `${c.lots}`)).join(" · ")} lots</div>
                  </>
                ) : <span className="text-muted">—</span>}
              </td>
              <td><SubMeter value={times(i.times_subscribed)} /></td>
              <td><ResearchCell i={i} compact /></td>
            </tr>
          );
        })}
      </tbody>
    </Table>
  );
}

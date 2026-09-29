"use client";

import {
  ArrowLeft, BarChart3, BellRing, CalendarCheck, ChartCandlestick, ChartLine, ListChecks, PartyPopper, Rocket, Table2,
} from "lucide-react";
import { useParams } from "next/navigation";
import { useMemo, useState } from "react";

import { LinkButton } from "@/components/dashboard/link-button";
import { BarsChart, TimeSeriesChart } from "@/components/charts";
import {
  SUB_HELP, Stepper, TERMS, Term, categories, countdown, currentStage, dayLabel, fmtX, inr, stagesFor, times, timeIST, useNow,
} from "@/components/ipo/lib";
import { AlertFeed } from "@/components/monitor/alert-feed";
import { NextCheck, RuleChips, StopWatch, jobLabel } from "@/components/monitor/watch-card";
import {
  Badge, Card, EmptyState, ErrorNote, PageHeader, Segmented, Skeleton, Stat, Table, cx,
} from "@/components/ui";
import { useApi, when, type WatchDetail } from "@/lib/api";

type Snap = WatchDetail["subscription"][number];
type Job = WatchDetail["jobs"][number];

const SERIES = [
  { key: "total", label: "Total", color: "var(--chart-1)" },
  { key: "qib", label: "QIB", color: "var(--chart-2)" },
  { key: "nii", label: "NII", color: "var(--chart-3)" },
  { key: "retail", label: "Retail", color: "var(--chart-6)" },
];

const stamp = (v: string | number) => {
  const s = String(v);
  return `${dayLabel(s, { day: "numeric", month: "short" })}, ${timeIST(s)}`;
};

/** Top-level and NII sub-categories of the latest snapshot, for the bar chart. */
function latestBars(s: Snap) {
  const names: Record<string, string> = { "1": "QIB", "2": "NII (all)", "2.1": "NII > ₹10L", "2.2": "NII ₹2–10L", "3": "Retail", "4": "Employees" };
  const rows = s.categories
    .filter((c) => c.code && names[c.code] && times(c.times) != null)
    .map((c) => ({ name: names[c.code!], times: Number(times(c.times)!.toFixed(2)) }));
  const t = times(s.total_times);
  if (t != null) rows.push({ name: "Total", times: Number(t.toFixed(2)) });
  return rows;
}

function jobDetail(j: Job) {
  const parts = j.slot.split(":");
  const r = j.result ?? {};
  if (j.error) return <span className="text-loss">{j.error}</span>;
  if (j.kind === "subscription" && r.total_times) return <span className="num">total {fmtX(Number(r.total_times))}</span>;
  if (j.kind === "listing") {
    if (r.price) return <span className="num">{String(r.which)} ₹{String(r.price)}{r.gain_pct ? ` (${Number(r.gain_pct) >= 0 ? "+" : ""}${r.gain_pct}%)` : ""}</span>;
    return <span>{parts[parts.length - 1]} price</span>;
  }
  if (j.kind === "lockin") return <span>{parts[parts.length - 1]}</span>;
  if (r.skipped) return <span>{String(r.skipped)}</span>;
  return null;
}

export default function WatchView() {
  const { id } = useParams<{ id: string }>();
  const { data, error, reload } = useApi<WatchDetail>(`/api/watches/${id}`, 60000);
  const now = useNow(30000);
  const [focus, setFocus] = useState<string>("all");
  const [stopError, setStopError] = useState<string | null>(null);

  const chart = useMemo(
    () =>
      (data?.subscription ?? []).map((s) => {
        const row: Record<string, unknown> = { at: s.as_of };
        for (const c of categories(s)) row[c.key] = c.times == null ? null : Number(c.times.toFixed(3));
        return row;
      }),
    [data],
  );

  if (!data)
    return error ? (
      <div className="space-y-4">
        <BackLink />
        <ErrorNote error={error} onRetry={reload} />
      </div>
    ) : (
      <div className="space-y-4">
        <Skeleton className="h-16 w-2/3" />
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">{[0, 1, 2, 3].map((i) => <Skeleton key={i} className="h-24 rounded-xl" />)}</div>
        <Skeleton className="h-80 rounded-xl" />
      </div>
    );

  const ipo = data.kind === "ipo";
  const snaps = data.subscription;
  const last = snaps[snaps.length - 1], prev = snaps[snaps.length - 2];
  const cat = (s: Snap | undefined, k: string) => (s ? categories(s).find((c) => c.key === k)?.times ?? null : null);
  const listingJobs = data.jobs.filter((j) => j.kind === "listing" && j.status === "done" && j.result?.price);
  const listingOpen = data.meta.listing_open as string | undefined;
  const listingClose = data.meta.listing_close as string | undefined;
  const openJob = listingJobs.find((j) => j.result.which === "open");
  const gain = openJob?.result.gain_pct != null ? Number(openJob.result.gain_pct) : null;
  const upper = gain != null && listingOpen ? Number(listingOpen) / (1 + gain / 100) : null;
  const pending = data.jobs.filter((j) => j.status === "pending");
  const series = focus === "all" ? SERIES : SERIES.filter((s) => s.key === focus);

  return (
    <div className="space-y-6">
      <BackLink />
      <PageHeader
        icon={ipo ? <Rocket className="size-5" /> : <ChartCandlestick className="size-5" />}
        eyebrow={ipo ? "IPO watch" : "Stock watch"}
        title={data.company_name ?? data.nse_symbol}
        description={
          <span className="inline-flex flex-wrap items-center gap-2">
            <span className="num">NSE {data.nse_symbol}</span>
            {data.active ? <Badge status="live">watching</Badge> : <Badge status="closed">stopped</Badge>}
            {data.active && <NextCheck next={pending[0] ? { kind: pending[0].kind, due_at: pending[0].due_at } : null} now={now} />}
          </span>
        }
        actions={data.active && (
          <StopWatch w={data} onDone={reload} onError={setStopError} />
        )}
      />
      <ErrorNote error={stopError} />
      <ErrorNote error={error} onRetry={reload} />

      {ipo ? (
        <Card title="Where this IPO is" icon={<CalendarCheck className="size-4" />}
          subtitle={data.meta.listing_confirmed ? "Listing date confirmed by NSE." : "Dates after the close are expected until NSE confirms them."}>
          <Stepper stages={stagesFor(data)} current={now == null ? -1 : currentStage(data, now)} className="mx-auto max-w-2xl" />
          <div className="mt-5 flex flex-wrap items-center justify-between gap-3 border-t border-border/70 pt-4 text-xs text-muted">
            <RuleChips status={data.meta.rule_status} />
            {data.anchor_shares && (
              <Term k="anchor">
                Anchor book <span className="num font-medium text-foreground">{Number(data.anchor_shares).toLocaleString("en-IN")}</span> shares
              </Term>
            )}
          </div>
        </Card>
      ) : (
        <Card title="Daily after-close check" icon={<ChartCandlestick className="size-4" />}>
          <p className="text-sm">
            Listed stock, checked after each close: results filings, corporate actions and ex-dates, promoter holding, moves of 5% or more.
            {typeof data.meta.promoter_pct === "string" && ` Last promoter holding ${data.meta.promoter_pct}%.`}
          </p>
        </Card>
      )}

      {ipo && (listingOpen || listingJobs.length > 0) && (
        <Card title="Listing result" icon={<PartyPopper className="size-4" />} className="animate-scale-in"
          help="Listing gain = (listing price − upper price band) ÷ upper band. Retail allottees paid the upper band.">
          <div className="grid gap-4 sm:grid-cols-3">
            <div>
              <p className="text-xs text-muted">Listing open</p>
              <p className="num text-2xl font-semibold">{listingOpen ? inr(Number(listingOpen), 2) : "—"}</p>
              {listingClose && <p className="num text-xs text-muted">close {inr(Number(listingClose), 2)}</p>}
            </div>
            <div>
              <p className="text-xs text-muted">Upper price band</p>
              <p className="num text-2xl font-semibold">{upper ? inr(upper, 2) : "—"}</p>
            </div>
            <div>
              <p className="text-xs text-muted">Listing gain</p>
              <p className={cx("num text-2xl font-semibold", gain == null ? "" : gain >= 0 ? "text-gain" : "text-loss")}>
                {gain == null ? "—" : `${gain >= 0 ? "+" : ""}${gain.toFixed(2)}%`}
              </p>
            </div>
          </div>
        </Card>
      )}

      {ipo && (
        <>
          <div className="stagger grid grid-cols-2 gap-3 lg:grid-cols-4">
            {SERIES.map((s) => {
              const v = cat(last, s.key), p = cat(prev, s.key);
              const help = s.key === "total" ? SUB_HELP : TERMS[s.key === "qib" ? "QIB" : s.key === "nii" ? "NII" : "Retail"];
              return (
                <Stat key={s.key} label={s.key === "total" ? "Total subscribed" : `${s.label} subscribed`} value={v}
                  format={fmtX} help={help} tone={v == null ? "neutral" : v >= 1 ? "gain" : "warn"}
                  hint={v != null && p != null ? `${v - p >= 0 ? "+" : ""}${(v - p).toFixed(2)}x since ${timeIST(prev!.as_of)}` : last ? `as of ${timeIST(last.as_of)}` : "no snapshot yet"} />
              );
            })}
          </div>

          <div className="grid gap-4 xl:grid-cols-5 [&>*]:min-w-0">
            <Card className="xl:col-span-3" title="Subscription over time" icon={<ChartLine className="size-4" />}
              subtitle="NSE combined book (NSE + BSE), times subscribed at each check. The dashed line is 1x."
              help={SUB_HELP}>
              {snaps.length > 1 && (
                <div className="mb-3 overflow-x-auto">
                  <Segmented value={focus} onChange={setFocus} options={[{ value: "all", label: "All" }, ...SERIES.map((s) => ({ value: s.key, label: s.label }))]} />
                </div>
              )}
              {snaps.length === 0 ? (
                <EmptyState icon={<ChartLine className="size-5" />} title="No snapshots yet">
                  The first subscription check records one; they run six times a bidding day
                  {pending[0] && now != null ? `, next in ${countdown(Date.parse(pending[0].due_at), now)}` : ""}.
                </EmptyState>
              ) : snaps.length === 1 ? (
                <EmptyState icon={<ChartLine className="size-5" />} title="One snapshot so far">
                  The trend line appears after the next check. The latest breakdown is on the right.
                </EmptyState>
              ) : (
                <TimeSeriesChart data={chart} x="at" series={series} area={false} height={300} format={fmtX}
                  xFormat={stamp} showChange={false} references={[{ y: 1, label: "1x" }]} curve="linear" dots />
              )}
            </Card>
            <Card className="xl:col-span-2" title="Latest by category" icon={<BarChart3 className="size-4" />}
              subtitle={last ? `As of ${when(last.as_of)}` : undefined} help={<>{TERMS.QIB} {TERMS.NII}</>}>
              {last ? (
                <BarsChart data={latestBars(last)} x="name" layout="vertical" height={Math.max(180, latestBars(last).length * 34 + 30)}
                  series={[{ key: "times", label: "Subscribed" }]} format={fmtX} reference={{ value: 1, label: "1x" }}
                  colorBy={(r) => (Number(r.times) >= 1 ? "var(--gain)" : "var(--warn)")} />
              ) : (
                <EmptyState title="Waiting for the first check">Category demand shows here once NSE publishes the bid book.</EmptyState>
              )}
            </Card>
          </div>

          {snaps.length > 0 && (
            <Card title="All snapshots" icon={<Table2 className="size-4" />} subtitle="Every check, oldest first (times subscribed).">
              <Table>
                <thead>
                  <tr>
                    <th>As of (NSE)</th>
                    <th className="!text-right">QIB</th>
                    <th className="!text-right">NII</th>
                    <th className="!text-right">Retail</th>
                    <th className="!text-right">Total</th>
                  </tr>
                </thead>
                <tbody>
                  {snaps.map((s) => (
                    <tr key={s.as_of}>
                      <td className="num text-xs">{when(s.as_of)}</td>
                      {["qib", "nii", "retail"].map((k) => {
                        const v = cat(s, k);
                        return <td key={k} className={cx("num text-right", v != null && v < 1 && "text-warn")}>{v == null ? "—" : fmtX(v)}</td>;
                      })}
                      <td className="num text-right font-semibold">{fmtX(Number(s.total_times))}</td>
                    </tr>
                  ))}
                </tbody>
              </Table>
            </Card>
          )}
        </>
      )}

      <div className="grid gap-4 xl:grid-cols-2 [&>*]:min-w-0">
        <Card title="Alerts" icon={<BellRing className="size-4" />}>
          <AlertFeed alerts={data.alerts} onRead={reload} showSymbol={false}
            emptyHint="Alerts for this watch appear here: rule changes, allotment reminders, listing prices and lock-in ends." />
        </Card>
        <Card title="Scheduled checks" icon={<ListChecks className="size-4" />} subtitle="What the monitor will do and what it did.">
          {data.jobs.length === 0 ? (
            <EmptyState title="No checks yet">Checks are planned on the next monitor pass.</EmptyState>
          ) : (
            <Table>
              <thead>
                <tr>
                  <th>Due</th>
                  <th>Check</th>
                  <th>Status</th>
                  <th>Result</th>
                </tr>
              </thead>
              <tbody>
                {data.jobs.map((j) => (
                  <tr key={j.id} className={cx(j.id === pending[0]?.id && "bg-brand-soft/40")}>
                    <td className="num text-xs whitespace-nowrap">{when(j.due_at)}</td>
                    <td className="text-xs">{jobLabel(j.kind)}{j.attempts > 1 && <span className="text-muted"> · {j.attempts} tries</span>}</td>
                    <td><Badge status={j.id === pending[0]?.id ? "current" : j.status}>{j.id === pending[0]?.id ? "next" : j.status}</Badge></td>
                    <td className="max-w-56 truncate text-xs text-muted">{jobDetail(j)}</td>
                  </tr>
                ))}
              </tbody>
            </Table>
          )}
        </Card>
      </div>
    </div>
  );
}

function BackLink() {
  return (
    <LinkButton href="/monitor" variant="ghost" icon={<ArrowLeft className="size-3.5" />}>All watches</LinkButton>
  );
}

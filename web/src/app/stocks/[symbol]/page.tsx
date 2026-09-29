"use client";

import {
  ArrowLeft, BellPlus, Building2, CalendarClock, ChartCandlestick, ExternalLink, FileText, FlaskConical, Landmark, Loader2, Megaphone,
  PieChart, Receipt, TrendingUp, Users,
} from "lucide-react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useState } from "react";

import { DonutChart, TimeSeriesChart, shortDate } from "@/components/charts";
import { ensureStock, startResearch, watchStock } from "@/components/markets/actions";
import { ResultsChart } from "@/components/markets/charts";
import { Metric, PERIOD_DAYS, type Period, PeriodChart, RangeBar, Timeline, crore, inr, pctOf, signedPct, toneOf } from "@/components/markets/common";
import type { StockHistory, StockOverview, StockResults } from "@/components/markets/types";
import {
  Badge, Button, Callout, Card, Delta, EmptyState, ErrorNote, PageHeader, Skeleton, SkeletonRows, Stat, Table,
} from "@/components/ui";
import { LiveStamp, useLive } from "@/components/live";
import { day, useApi, when, type WatchSummary } from "@/lib/api";

const PERIODS: Period[] = ["1M", "3M", "6M", "1Y", "3Y", "5Y"];

export default function StockDetail() {
  const { symbol: raw } = useParams<{ symbol: string }>();
  const symbol = decodeURIComponent(raw).toUpperCase();
  const router = useRouter();
  const [period, setPeriod] = useState<Period>("1Y");
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);

  const ov = useApi<StockOverview>(`/api/stocks/${encodeURIComponent(symbol)}/overview`);
  const days = PERIOD_DAYS[period] > 366 ? PERIOD_DAYS[period] : 366;
  const hist = useApi<StockHistory>(`/api/stocks/${encodeURIComponent(symbol)}/history?days=${Math.min(days, 1827)}`);
  const results = useApi<StockResults>(`/api/stocks/${encodeURIComponent(symbol)}/results`);
  const watches = useApi<WatchSummary[]>("/api/watches");
  const watching = (watches.data ?? []).some((w) => w.kind === "stock" && w.active && w.nse_symbol === symbol);

  // the price refreshes every 30 s while NSE is open; the rest of the page is fetched once
  const live = useLive<{ quote: NonNullable<StockOverview["quote"]>; fetched_at: string }>(
    `/api/stocks/${encodeURIComponent(symbol)}/quote`, { session: "equity", everyMs: 30000 });
  const q = live.data?.quote ? { ...ov.data?.quote, ...live.data.quote } : ov.data?.quote;
  const last = q?.last_price ? Number(q.last_price) : null;
  const bars = hist.data?.bars ?? [];
  const lastBar = bars[bars.length - 1];
  const histLoadingMore = hist.data && hist.data.days < Math.min(days, 1827);

  const research = async () => {
    if (!confirm(`Start a full stock research run for ${symbol}? It uses your Claude plan window.`)) return;
    setBusy(true);
    try {
      router.push(`/runs/${await startResearch(await ensureStock(symbol), "stock_report")}`);
    } catch (e) {
      setActionError((e as Error).message);
      setBusy(false);
    }
  };
  const watch = async () => {
    setBusy(true);
    try {
      await watchStock(symbol);
      setNote(`Watching ${symbol}: results filings, corporate actions, holding changes and big moves are checked after each close.`);
      watches.reload();
    } catch (e) {
      setActionError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const holding = ov.data?.shareholding ?? [];
  const latestHolding = holding.find((h) => h.promoter_pct != null);
  const quarters = results.data?.quarters ?? [];

  return (
    <div>
      <Link href="/stocks" className="mb-3 inline-flex items-center gap-1 text-xs font-medium text-muted transition hover:text-brand">
        <ArrowLeft className="size-3.5" /> All stocks
      </Link>
      <PageHeader
        icon={<ChartCandlestick className="size-5" />}
        eyebrow={
          <span className="inline-flex items-center gap-2">
            NSE · {symbol}
            {q?.status && <Badge tone={q.status === "Listed" ? "gain" : "warn"}>{q.status.toLowerCase()}</Badge>}
          </span>
        }
        title={q?.company ?? (ov.error ? symbol : <Skeleton className="h-7 w-64" />)}
        description={
          q ? (
            <>
              {q.industry ?? "Industry not stated"}
              {q.listing_date && <> · listed {day(q.listing_date)}</>}
            </>
          ) : undefined
        }
        actions={
          <>
            <Button variant="secondary" size="md" disabled={busy || watching} onClick={watch} icon={<BellPlus className="size-4" />}>
              {watching ? "Watching" : "Watch"}
            </Button>
            <Button size="md" disabled={busy} onClick={research} icon={<FlaskConical className="size-4" />}>
              Research
            </Button>
          </>
        }
      />

      <LiveStamp session="equity" live={live.live} status={live.status} updatedAt={live.updatedAt ?? ov.updatedAt} everyMs={30000}
        asOf={q?.as_of} onRefresh={live.reload} className="-mt-3 mb-5" />
      <div className="space-y-5">
        <ErrorNote error={actionError} />
        {note && <Callout tone="gain">{note}</Callout>}
        {ov.error && <ErrorNote error={`Could not load ${symbol} from NSE: ${ov.error}`} onRetry={ov.reload} />}
        {ov.data && ov.data.errors.length > 0 && (
          <Callout tone="warn" title="Some sections could not be loaded from NSE">
            {ov.data.errors.map((e) => e.split(":")[0]).join(", ")}: NSE refused or timed out. The rest of the page is current; reload in a minute to retry.
          </Callout>
        )}

        {/* headline numbers */}
        <div className="grid [&>*]:min-w-0 grid-cols-2 gap-3 lg:grid-cols-4 stagger">
          {!ov.data && !ov.error ? (
            Array.from({ length: 4 }, (_, i) => <Skeleton key={i} className="h-[104px] rounded-xl" />)
          ) : (
            <>
              <Stat label="Last price" icon={<TrendingUp className="size-4" />} value={last} format={(n) => inr(n)}
                delta={q?.change_pct} deltaLabel={q?.change ? `${Number(q.change) > 0 ? "+" : "−"}${inr(Math.abs(Number(q.change)))} today` : undefined} />
              <Stat label="Market cap" icon={<Building2 className="size-4" />} tone="accent"
                display={<span className="num">{q?.market_cap ? crore(q.market_cap) : "—"}</span>}
                hint={q?.issued_shares ? `${Number(q.issued_shares).toLocaleString("en-IN")} shares` : undefined}
                help="Market capitalisation: shares issued × last price. The market's price tag for the whole company." />
              <Stat label="52-week position" icon={<ChartCandlestick className="size-4" />} tone="info"
                display={<span className="num">{q?.week52_position != null ? `${Math.round(q.week52_position * 100)}%` : "—"}</span>}
                hint={q?.week52_position != null ? (q.week52_position < 0.2 ? "near its 1-year low" : q.week52_position > 0.8 ? "near its 1-year high" : "mid-range") : undefined}
                help="Where today's price sits between the lowest (0%) and highest (100%) price of the past 52 weeks." />
              <Stat label="Dividend yield (12m)" icon={<Receipt className="size-4" />} tone="gain"
                display={<span className="num">{ov.data?.dividends.ttm_yield != null ? pctOf(ov.data.dividends.ttm_yield) : "—"}</span>}
                hint={ov.data?.dividends.ttm_per_share ? `₹${ov.data.dividends.ttm_per_share}/share with ex-date in the last year` : "no cash dividend in the last year"}
                help="Cash dividends per share with an ex-date in the last 12 months, divided by today's price." />
            </>
          )}
        </div>

        {/* price chart + range */}
        <div className="grid [&>*]:min-w-0 gap-5 lg:grid-cols-3">
          <Card className="lg:col-span-2" title="Price" subtitle="Daily closing price on NSE" icon={<TrendingUp className="size-4" />}
            help="Each point is the day's official closing price. Pick a period to see how much the price moved over it.">
            {hist.error && !hist.data ? (
              <ErrorNote error={hist.error} onRetry={hist.reload} />
            ) : !hist.data ? (
              <Skeleton className="h-[320px] w-full rounded-lg" />
            ) : bars.length < 2 ? (
              <EmptyState title="No price history">NSE returned no trading days for {symbol}. It may be suspended or newly listed.</EmptyState>
            ) : (
              <>
                <PeriodChart data={bars} series={[{ key: "close", label: "Close" }]} periods={PERIODS} period={period} onPeriod={setPeriod}
                  format={(v) => `₹${v.toLocaleString("en-IN", { maximumFractionDigits: 0 })}`}
                  extra={histLoadingMore ? <Loader2 className="size-4 animate-spin text-muted" aria-label="Loading longer history" /> : null} />
                {hist.data.partial && (
                  <p className="mt-2 text-xs text-warn">NSE refused part of the older history; the chart starts at {shortDate(bars[0].date)}.</p>
                )}
                <div className="mt-4 grid grid-cols-3 gap-2">
                  <Metric label={`Return (${hist.data.days >= 1827 ? "5Y" : hist.data.days >= 1096 ? "3Y" : "1Y"})`} value={signedPct(hist.data.stats.return)} tone={toneOf(hist.data.stats.return)} />
                  <Metric label="Volatility (yearly)" value={pctOf(hist.data.stats.annualised_volatility, 1)}
                    help="How much the price swings, annualised (standard deviation of daily returns × √252). Higher = bumpier ride." />
                  <Metric label="Max drawdown" value={pctOf(hist.data.stats.max_drawdown, 1)} tone="text-loss"
                    sub={hist.data.stats.drawdown_peak ? `${shortDate(hist.data.stats.drawdown_peak)} → ${shortDate(hist.data.stats.drawdown_trough!)}` : undefined}
                    help="The biggest fall from a peak to a later low in this period: the worst loss a buyer at the top would have sat through." />
                </div>
              </>
            )}
          </Card>

          <div className="space-y-5">
            <Card title="52-week range" icon={<ChartCandlestick className="size-4" />} help="The lowest and highest prices of the last 52 weeks, with today's price marked.">
              {q?.week52_low && q?.week52_high && last != null ? (
                <RangeBar low={Number(q.week52_low)} high={Number(q.week52_high)} value={last} lowLabel="52w low" highLabel="52w high" />
              ) : ov.data ? (
                <p className="text-sm text-muted">NSE did not give a 52-week range.</p>
              ) : (
                <SkeletonRows rows={2} />
              )}
            </Card>
            <Card title="Today" icon={<CalendarClock className="size-4" />}>
              {!ov.data ? (
                <SkeletonRows rows={4} />
              ) : (
                <dl className="grid [&>*]:min-w-0 grid-cols-2 gap-2">
                  <Metric label="Open" value={inr(q?.open)} />
                  <Metric label="Previous close" value={inr(q?.previous_close)} />
                  <Metric label="Day change" value={<Delta value={q?.change_pct} />} />
                  <Metric label="Last session volume" value={lastBar?.volume != null ? lastBar.volume.toLocaleString("en-IN") : "—"}
                    sub={lastBar ? shortDate(lastBar.date) : undefined} />
                </dl>
              )}
            </Card>
          </div>
        </div>

        {/* holding + results */}
        <div className="grid [&>*]:min-w-0 gap-5 lg:grid-cols-2">
          <Card title="Shareholding pattern" icon={<Users className="size-4" />}
            subtitle={latestHolding?.as_of ? `Quarter ended ${day(latestHolding.as_of)}` : "Latest filing with NSE"}
            help="Who owns the company. Promoters are the founders / controlling group; public includes institutions (FIIs, mutual funds, insurers) and retail. A falling promoter share can mean selling or dilution.">
            {!ov.data ? (
              <SkeletonRows rows={5} />
            ) : !latestHolding ? (
              <EmptyState icon={<PieChart className="size-5" />} title="No shareholding filing">NSE has no shareholding pattern for {symbol} yet.</EmptyState>
            ) : (
              <div className="space-y-5">
                <DonutChart
                  height={170}
                  format={(v) => `${v.toFixed(2)}%`}
                  data={[
                    { name: "Promoters", value: latestHolding.promoter_pct ?? 0, color: "var(--chart-1)" },
                    { name: "Public", value: latestHolding.public_pct ?? 0, color: "var(--chart-2)" },
                    { name: "Employee trusts", value: latestHolding.employee_trusts_pct ?? 0, color: "var(--chart-3)" },
                  ].filter((d) => d.value > 0)}
                  center={
                    <div>
                      <p className="num text-lg font-semibold">{latestHolding.promoter_pct?.toFixed(1)}%</p>
                      <p className="text-[11px] text-muted">promoters</p>
                    </div>
                  }
                />
                {holding.filter((h) => h.promoter_pct != null).length > 1 && (
                  <div>
                    <p className="mb-2 text-xs font-medium text-muted">Promoter holding by quarter</p>
                    <TimeSeriesChart
                      height={140}
                      area={false}
                      showChange={false}
                      data={[...holding].reverse().filter((h) => h.promoter_pct != null && h.as_of).map((h) => ({ date: h.as_of, promoter: h.promoter_pct }))}
                      series={[{ key: "promoter", label: "Promoter %", color: "var(--chart-1)" }]}
                      format={(v) => `${v.toFixed(1)}%`}
                    />
                  </div>
                )}
                <p className="text-[11px] text-muted">Public = institutions (FIIs, mutual funds, insurers) plus retail. NSE&apos;s summary gives only promoter, public and employee trusts; the FII / DII split is in each quarter&apos;s full filing.</p>
              </div>
            )}
          </Card>

          <Card title="Quarterly results" icon={<Landmark className="size-4" />}
            subtitle={quarters.length ? `${quarters[0].consolidated ? "Consolidated" : "Standalone"}, from each filing's XBRL` : "Revenue and profit from results filings"}
            help="Revenue from operations and profit attributable to shareholders for each quarter, read from the company's own results filing on NSE.">
            {results.error ? (
              <ErrorNote error={results.error} onRetry={results.reload} />
            ) : !results.data ? (
              <Skeleton className="h-[240px] w-full rounded-lg" />
            ) : quarters.length === 0 ? (
              <EmptyState icon={<FileText className="size-5" />} title="No results in NSE's index">
                NSE&apos;s results index has no machine-readable filings for {symbol}. Check the announcements below for the latest results PDF.
              </EmptyState>
            ) : (
              <>
                <ResultsChart rows={quarters.map((r) => ({ label: shortDate(r.period_end), revenue: r.revenue != null ? r.revenue / 1e7 : null, profit: r.profit != null ? r.profit / 1e7 : null }))} />
                <Table className="mt-3">
                  <thead>
                    <tr>
                      <th>Quarter</th>
                      <th className="text-right!">Margin</th>
                      <th className="text-right!">EPS</th>
                      <th className="text-right!">Filing</th>
                    </tr>
                  </thead>
                  <tbody>
                    {[...quarters].reverse().slice(0, 4).map((r) => (
                      <tr key={r.period_end}>
                        <td className="num text-xs">{day(r.period_end)}</td>
                        <td className="num text-right text-xs">{pctOf(r.margin, 1)}</td>
                        <td className="num text-right text-xs">{r.eps != null ? `₹${r.eps.toFixed(2)}` : "—"}</td>
                        <td className="text-right">
                          <a href={r.xbrl} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-xs text-brand hover:underline">
                            XBRL <ExternalLink className="size-3" />
                          </a>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </Table>
                <p className="mt-2 text-[11px] text-muted">
                  Latest in NSE&apos;s results index: quarter ended {day(quarters[quarters.length - 1].period_end)}. The index can lag; newer results appear first as announcements.
                </p>
              </>
            )}
          </Card>
        </div>

        {/* actions + announcements */}
        <div className="grid [&>*]:min-w-0 gap-5 lg:grid-cols-2">
          <Card title="Corporate actions" icon={<CalendarClock className="size-4" />} subtitle="Dividends, bonuses, splits and buybacks"
            help="The ex-date is the first day the share trades without the benefit: buy before it to get the dividend or bonus. The record date is when the company checks who holds the shares.">
            {!ov.data ? (
              <SkeletonRows rows={5} />
            ) : ov.data.corporate_actions.length === 0 ? (
              <EmptyState icon={<CalendarClock className="size-5" />} title="No corporate actions">NSE lists no dividends, bonuses or splits for {symbol}.</EmptyState>
            ) : (
              <div className="max-h-[420px] overflow-y-auto pr-1 pl-1.5">
                <Timeline
                  items={ov.data.corporate_actions.map((a, i) => ({
                    key: `${a.ex_date}-${i}`,
                    date: a.ex_date,
                    highlight: a.upcoming,
                    badge: (
                      <>
                        {a.upcoming && <Badge tone="brand" dot>upcoming ex-date</Badge>}
                        {a.dividend_per_share && <Badge tone="gain">₹{a.dividend_per_share}/share</Badge>}
                      </>
                    ),
                    title: a.subject,
                    meta: a.record_date ? `Record date ${day(a.record_date)}` : undefined,
                  }))}
                />
              </div>
            )}
          </Card>

          <Card title="Announcements" icon={<Megaphone className="size-4" />} subtitle="Latest filings with NSE">
            {!ov.data ? (
              <SkeletonRows rows={6} />
            ) : ov.data.announcements.length === 0 ? (
              <EmptyState icon={<Megaphone className="size-5" />} title="No announcements loaded">
                {ov.data.errors.some((e) => e.startsWith("announcements")) ? "NSE refused the announcements list just now; reload to retry." : `NSE lists no recent announcements for ${symbol}.`}
              </EmptyState>
            ) : (
              <ul className="-mx-2 max-h-[420px] space-y-0.5 overflow-y-auto stagger">
                {ov.data.announcements.map((a, i) => (
                  <li key={`${a.at}-${i}`} className="rounded-lg px-2 py-2 transition hover:bg-card-hover">
                    <div className="flex flex-wrap items-center gap-2">
                      <Badge tone={a.results_period_end ? "accent" : "neutral"}>{a.results_period_end ? "results" : a.category || "update"}</Badge>
                      <span className="text-[11px] text-muted">{when(a.at)}</span>
                      {a.attachment && (
                        <a href={a.attachment} target="_blank" rel="noreferrer" className="ml-auto inline-flex items-center gap-1 text-xs text-brand hover:underline">
                          PDF <ExternalLink className="size-3" />
                        </a>
                      )}
                    </div>
                    <p className="mt-1 line-clamp-2 text-sm text-foreground/90">{a.text || a.category}</p>
                  </li>
                ))}
              </ul>
            )}
          </Card>
        </div>

        {ov.data && (
          <p className="text-[11px] text-muted">
            Source: <a href={ov.data.source} target="_blank" rel="noreferrer" className="underline underline-offset-2">NSE quote page</a>, fetched {when(ov.data.fetched_at)} (cached up to 10 minutes). Not investment advice.
          </p>
        )}
      </div>
    </div>
  );
}

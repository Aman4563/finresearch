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
import { MarginChart, ResultsChart } from "@/components/markets/charts";
import { ShareholdingSplit } from "@/components/markets/shareholding";
import { Metric, PERIOD_DAYS, type Period, PeriodChart, RangeBar, Timeline, crore, inr, pctOf, signedPct, toneOf } from "@/components/markets/common";
import type { StockHistory, StockOverview, StockResults, StockShareholding } from "@/components/markets/types";
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
  const shp = useApi<StockShareholding>(`/api/stocks/${encodeURIComponent(symbol)}/shareholding`);
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
  const split = shp.data && shp.data.quarters.length > 0 ? shp.data : null;
  const splitLoading = !shp.data && !shp.error;
  const quarters = results.data?.quarters ?? [];
  const annual = results.data?.annual ?? [];
  const latestQ = quarters[quarters.length - 1];
  const prevQ = quarters[quarters.length - 2];

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
            subtitle={split ? `Quarter ended ${day(split.quarters[split.quarters.length - 1].as_of)}, from the filed pattern` : latestHolding?.as_of ? `Quarter ended ${day(latestHolding.as_of)}` : "Latest filing with NSE"}
            help="Who owns the company: promoters (founders / controlling group), foreign institutions (FII / FPI), domestic institutions (DII: mutual funds, insurers, banks) and individuals. A falling promoter share can mean selling or dilution; rising institutional ownership often follows improving fundamentals.">
            {split ? (
              <ShareholdingSplit data={split} />
            ) : !ov.data || splitLoading ? (
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
                <p className="text-[11px] text-muted">Public = institutions (FIIs, mutual funds, insurers) plus retail. NSE&apos;s summary gives only promoter, public and employee trusts; the FII / DII split could not be read from the filed pattern{shp.error ? ` (${shp.error})` : ""}.</p>
              </div>
            )}
          </Card>

          <Card title="Quarterly results" icon={<Landmark className="size-4" />}
            subtitle={latestQ ? `${latestQ.consolidated ? "Consolidated" : "Standalone"}, read from each filing's XBRL` : "Revenue and profit from results filings"}
            actions={latestQ ? <Badge tone="accent">Latest: {latestQ.label}{latestQ.filed_at ? `, filed ${day(latestQ.filed_at)}` : ""}</Badge> : undefined}
            help="Revenue from operations and profit attributable to shareholders, read from the company's own results filing on NSE. Since the March 2025 quarter SEBI has companies file results as Integrated Filing (Financials); older quarters come from NSE's financial results index.">
            {results.error ? (
              <ErrorNote error={results.error} onRetry={results.reload} />
            ) : !results.data ? (
              <Skeleton className="h-[240px] w-full rounded-lg" />
            ) : quarters.length === 0 ? (
              <EmptyState icon={<FileText className="size-5" />} title="No machine-readable results on NSE">
                NSE has no results XBRL for {symbol} in its integrated filing or financial results indexes. Check the announcements below for the latest results PDF.
              </EmptyState>
            ) : (
              <>
                {latestQ && (
                  <div className="mb-3 grid grid-cols-1 gap-2 sm:grid-cols-3">
                    <Metric label={latestQ.bank ? "Interest earned" : "Revenue"} value={crore(latestQ.revenue)}
                      help={latestQ.bank ? "Banks report interest earned in place of revenue from operations." : "Revenue from operations for the quarter."}
                      sub={<GrowthSub qoq={latestQ.growth.revenue_qoq} yoy={latestQ.growth.revenue_yoy} />} />
                    <Metric label="Net profit" value={crore(latestQ.profit)}
                      help="Profit for the quarter attributable to the company's shareholders (after minority interests)."
                      sub={<GrowthSub qoq={latestQ.growth.profit_qoq} yoy={latestQ.growth.profit_yoy} />} />
                    <Metric label="Net margin" value={pctOf(latestQ.margin, 1)}
                      help="Net profit as a share of revenue. A falling margin means costs or taxes grew faster than sales."
                      sub={prevQ?.margin != null && latestQ.margin != null && prevQ.consolidated === latestQ.consolidated
                        ? `${((latestQ.margin - prevQ.margin) * 100 >= 0 ? "+" : "")}${((latestQ.margin - prevQ.margin) * 100).toFixed(1)} pp QoQ` : "vs previous quarter: —"} />
                  </div>
                )}
                <ResultsChart rows={quarters.map((r) => ({ label: r.label, title: `${r.label}, quarter ended ${day(r.period_end)}`,
                  revenue: r.revenue != null ? r.revenue / 1e7 : null, profit: r.profit != null ? r.profit / 1e7 : null }))} />
                <p className="mt-3 mb-1 text-[11px] font-medium text-muted">Net margin trend</p>
                <MarginChart rows={quarters.map((r) => ({ label: r.label, title: `${r.label}, quarter ended ${day(r.period_end)}`, margin: r.margin }))} />
                <Table className="mt-3">
                  <thead>
                    <tr>
                      <th>Quarter</th>
                      <th className="text-right!">{latestQ?.bank ? "Interest earned" : "Revenue"}</th>
                      <th className="text-right!">Net profit</th>
                      <th className="text-right!">Profit YoY</th>
                    </tr>
                  </thead>
                  <tbody>
                    {[...quarters].reverse().slice(0, 4).map((r) => (
                      <tr key={r.period_end}>
                        <td className="text-xs">
                          <span className="font-medium">{r.label}</span>
                          <span className="block whitespace-nowrap text-[11px] text-muted">
                            {r.filed_at ? `filed ${day(r.filed_at)}` : day(r.period_end)}{" · "}
                            <a href={r.ixbrl ?? r.xbrl} target="_blank" rel="noreferrer" className="inline-flex items-center gap-0.5 text-brand hover:underline">
                              {r.ixbrl ? "iXBRL" : "XBRL"} <ExternalLink className="size-3" />
                            </a>
                          </span>
                        </td>
                        <td className="num text-right text-xs">{crore(r.revenue)}</td>
                        <td className="num text-right text-xs">{crore(r.profit)}</td>
                        <td className="text-right text-xs"><Delta value={r.growth.profit_yoy != null ? r.growth.profit_yoy * 100 : null} digits={1} /></td>
                      </tr>
                    ))}
                  </tbody>
                </Table>
                <details className="mt-3 text-xs">
                  <summary className="cursor-pointer text-muted hover:text-foreground">Full P&amp;L lines{annual.length ? " and annual figures" : ""}</summary>
                  <div className="mt-2 overflow-x-auto">
                    <Table>
                      <thead>
                        <tr>
                          <th>Period</th>
                          <th className="text-right!">Revenue</th>
                          <th className="text-right!">Other income</th>
                          <th className="text-right!">Expenses</th>
                          <th className="text-right!">PBT</th>
                          <th className="text-right!">Tax</th>
                          <th className="text-right!">Net profit</th>
                          <th className="text-right!">EPS</th>
                        </tr>
                      </thead>
                      <tbody>
                        {[...[...quarters].reverse(), ...[...annual].reverse()].map((r) => (
                          <tr key={`${r.label}-${r.period_end}`}>
                            <td className="whitespace-nowrap text-xs font-medium" title={`${r.consolidated ? "Consolidated" : "Standalone"}, ${day(r.period_start)} to ${day(r.period_end)}`}>
                              {r.label}{r.consolidated ? "" : " (S)"}
                            </td>
                            <td className="num text-right text-xs">{crore(r.revenue)}</td>
                            <td className="num text-right text-xs">{crore(r.other_income)}</td>
                            <td className="num text-right text-xs">{crore(r.total_expenses)}</td>
                            <td className="num text-right text-xs">{crore(r.profit_before_tax)}</td>
                            <td className="num text-right text-xs">{crore(r.tax)}</td>
                            <td className="num text-right text-xs">{crore(r.profit)}</td>
                            <td className="num text-right text-xs">{r.eps != null ? `₹${r.eps.toFixed(2)}` : "—"}</td>
                          </tr>
                        ))}
                      </tbody>
                    </Table>
                  </div>
                  <p className="mt-1 text-[11px] text-muted">(S) = standalone (the company filed no consolidated XBRL for that period). PBT includes exceptional items. EPS is as reported, not adjusted for later bonuses or splits.</p>
                </details>
                <p className="mt-2 text-[11px] text-muted">
                  Source: {results.data.sources.map((x, i) => (
                    <span key={x.url}>{i ? " and " : ""}<a href={x.url} target="_blank" rel="noreferrer" className="text-brand hover:underline">{x.name}</a></span>
                  ))} (NSE), each quarter&apos;s XBRL. Checked {when(results.data.as_of)}.
                  {results.data.errors.length > 0 && ` ${results.data.errors.length} filing${results.data.errors.length > 1 ? "s" : ""} could not be read.`}
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

/** QoQ and YoY change under a results KPI (fractions from the API). */
function GrowthSub({ qoq, yoy }: { qoq: number | null | undefined; yoy: number | null | undefined }) {
  return (
    <span className="inline-flex flex-wrap gap-x-2">
      <span>QoQ <Delta value={qoq != null ? qoq * 100 : null} digits={1} /></span>
      <span>YoY <Delta value={yoy != null ? yoy * 100 : null} digits={1} /></span>
    </span>
  );
}

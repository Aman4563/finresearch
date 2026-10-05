"use client";

import {
  ArrowLeft, BellPlus, Building2, CalendarClock, ChartCandlestick, ExternalLink, FileText, FlaskConical, Landmark, Loader2, Megaphone,
  PieChart, Receipt, TrendingUp, Users,
} from "lucide-react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useCallback, useState } from "react";

import { DonutChart, TimeSeriesChart, shortDate } from "@/components/charts";
import { PriceChart } from "@/components/markets/price-chart";
import { ensureStock, startResearch, watchStock } from "@/components/markets/actions";
import { ExchangeBadge, ExchangeSwitch, PriceComparison } from "@/components/markets/exchange";
import { QualityNote, displayPrice, lastTradedNote, priceCaption, qualityFlags } from "@/components/markets/price-quality";
import { MarginChart, ResultsChart } from "@/components/markets/charts";
import { ShareholdingSplit } from "@/components/markets/shareholding";
import { ForensicCard, SinceReport, StockSignalCard } from "@/components/markets/stock-signal";
import { StockDisclosuresCard } from "@/components/markets/disclosures";
import { StockPeersCard } from "@/components/markets/peers";
import { StockSurpriseCard } from "@/components/markets/surprise";
import { Metric, RangeBar, Timeline, crore, inr, pctOf, signedPct, toneOf } from "@/components/markets/common";
import type { StockHistory, StockOverview, StockResults, StockShareholding } from "@/components/markets/types";
import {
  Badge, Button, Callout, Card, Delta, EmptyState, ErrorNote, Unreachable, PageHeader, ScrollArea, Skeleton, SkeletonRows, Stat, Table,
} from "@/components/ui";
import { DataRequest, LiveStamp, sessionOpen, useLive } from "@/components/live";
import { day, useApi, useRetryApi, when, type WatchSummary } from "@/lib/api";
import { RANGE_DAYS, useTimeFrames } from "@/lib/timeframes";

export default function StockDetail() {
  const { symbol: raw } = useParams<{ symbol: string }>();
  // the NSE symbol, or "BSE:<scrip code>" for the BSE view of a stock (the only view of a BSE-only one)
  const symbol = decodeURIComponent(raw).toUpperCase();
  const onBse = symbol.startsWith("BSE:");
  const ex = onBse ? "BSE" : "NSE";
  const router = useRouter();
  const [histDays, setHistDays] = useState(366);
  const onRange = useCallback((r: keyof typeof RANGE_DAYS) => setHistDays((d) => Math.max(d, RANGE_DAYS[r], 366)), []);
  const { tf } = useTimeFrames();
  const quoteMs = tf.quote_refresh_s * 1000;
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);

  const ov = useRetryApi<StockOverview>(`/api/stocks/${encodeURIComponent(symbol)}/overview`);
  const days = histDays;
  const hist = useRetryApi<StockHistory>(`/api/stocks/${encodeURIComponent(symbol)}/history?days=${Math.min(days, 1827)}`);
  const results = useRetryApi<StockResults>(`/api/stocks/${encodeURIComponent(symbol)}/results`);
  const shp = useRetryApi<StockShareholding>(`/api/stocks/${encodeURIComponent(symbol)}/shareholding`);
  const watches = useApi<WatchSummary[]>("/api/watches");
  const listing = ov.data?.listing;
  // watches, research runs and the signal follow the stock's key: the NSE symbol when it trades on NSE (also from
  // the BSE view of a dual-listed stock), else "BSE:<code>" for a BSE-only stock, read from BSE throughout
  const stockKey = onBse ? (ov.data ? (listing?.nse_symbol ?? symbol) : null) : symbol;
  const keyExchange = stockKey?.startsWith("BSE:") ? "BSE" : "NSE";
  const keyLabel = stockKey?.startsWith("BSE:") ? `BSE ${stockKey.slice(4)}` : stockKey;
  const watching = (watches.data ?? []).some((w) => w.kind === "stock" && w.active && (w.key ?? w.nse_symbol) === stockKey);

  // the price refreshes while the market is open (NSE and BSE keep the same hours; every 30 s unless the profile
  // says otherwise); the rest is fetched once
  const live = useLive<{ quote: NonNullable<StockOverview["quote"]>; fetched_at: string }>(
    `/api/stocks/${encodeURIComponent(symbol)}/quote`, { session: "equity", everyMs: quoteMs, active: quoteMs > 0 });
  const q = live.data?.quote ? { ...ov.data?.quote, ...live.data.quote } : ov.data?.quote;
  const last = displayPrice(q);
  const bars = hist.data?.bars ?? [];
  const lastBar = bars[bars.length - 1];
  const histLoadingMore = hist.data && hist.data.days < Math.min(days, 1827);
  // benchmark for the headline return (roadmap §B0.3): NIFTY 50 through its ETF (NIFTYBEES) over the same number of
  // days, asked for only after the stock's own history has arrived so it never delays the page
  const benchDays = hist.data && !histLoadingMore && symbol.toUpperCase() !== "NIFTYBEES" ? hist.data.days : null;
  const bench = useApi<StockHistory>(benchDays ? `/api/stocks/NIFTYBEES/history?days=${benchDays}` : null);

  const research = async () => {
    if (!stockKey) return;
    if (!confirm(`Start a full stock research run for ${keyLabel} (${keyExchange} data and filings)? It uses your Claude plan window.`)) return;
    setBusy(true);
    try {
      router.push(`/runs/${await startResearch(await ensureStock(stockKey), "stock_report")}`);
    } catch (e) {
      setActionError((e as Error).message);
      setBusy(false);
    }
  };
  const watch = async () => {
    if (!stockKey) return;
    setBusy(true);
    try {
      await watchStock(stockKey);
      setNote(`Watching ${keyLabel} on ${keyExchange}: results filings, corporate actions, holding changes and big moves are checked after each close.`);
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
          <span className="inline-flex flex-wrap items-center gap-2">
            {onBse ? <>BSE · {q?.symbol ?? ""} {symbol.slice(4)}</> : <>NSE · {symbol}</>}
            <ExchangeSwitch listing={listing} current={ex} />
            {listing && listing.exchange !== "both" && <ExchangeBadge exchange={listing.exchange} group={listing.bse_group} />}
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
            <Button variant="secondary" size="md" disabled={busy || watching || !stockKey} onClick={watch} icon={<BellPlus className="size-4" />}
              title={stockKey ? `Checks ${keyExchange} filings, actions and big moves after each close` : undefined}>
              {watching ? "Watching" : "Watch"}
            </Button>
            <Button size="md" disabled={busy || !stockKey} onClick={research} icon={<FlaskConical className="size-4" />}
              title={stockKey ? `Full research run on ${keyExchange} data and filings (uses your Claude plan)` : undefined}>
              Research
            </Button>
          </>
        }
      />

      <LiveStamp session="equity" live={sessionOpen(live.status, "equity")} autoRefresh={quoteMs > 0} status={live.status} updatedAt={live.updatedAt ?? ov.updatedAt} everyMs={quoteMs || 30000}
        asOf={q?.as_of} asOfLabel={`${ex} as of`} onRefresh={live.reload} className="-mt-3 mb-5" />
      <div className="space-y-5">
        <ErrorNote error={actionError} />
        {note && <Callout tone="gain">{note}</Callout>}
        {ov.error && <ErrorNote error={`Could not load ${symbol} from ${ex}: ${ov.error}`} onRetry={ov.reload} />}
        {ov.data && (ov.data.unreachable?.length ?? 0) > 0 && (
          <Callout tone="warn" title={`Couldn't reach ${ex} just now`}>
            <span className="flex flex-wrap items-center gap-2">
              {ov.data.unreachable!.join(", ")} did not load (a connection problem, not missing data). The rest of the page is current.
              <Button size="sm" variant="secondary" onClick={ov.retry}>Retry</Button>
            </span>
          </Callout>
        )}
        {ov.data && ov.data.errors.some((e) => !(ov.data!.unreachable ?? []).includes(e.split(":")[0])) && (
          <Callout tone="warn" title={`Some sections could not be read from ${ex}`}>
            {ov.data.errors.filter((e) => !(ov.data!.unreachable ?? []).includes(e.split(":")[0])).map((e) => e.split(":")[0]).join(", ")}: {ex} answered, but the data could not be read.
          </Callout>
        )}

        <QualityNote flags={qualityFlags(q, lastBar ? { date: lastBar.date, close: lastBar.close } : undefined, ex)} />

        {/* headline numbers */}
        <div className="grid [&>*]:min-w-0 grid-cols-2 gap-3 lg:grid-cols-4 stagger">
          {!ov.data && !ov.error ? (
            Array.from({ length: 4 }, (_, i) => <Skeleton key={i} className="h-[104px] rounded-xl" />)
          ) : (
            <>
              <Stat label={q?.price_label ?? "Last price"} icon={<TrendingUp className="size-4" />} value={last} format={(n) => inr(n)}
                delta={q?.change_pct}
                deltaLabel={q?.change ? `${Number(q.change) > 0 ? "+" : "−"}${inr(Math.abs(Number(q.change)))} today` : undefined}
                note={lastTradedNote(q)}
                help={<>
                  {priceCaption(q)}. In session this is the last traded price; after the close it is the exchange&apos;s official closing price, which is computed from the closing trades and can differ from the last trade (both are shown when they do).
                  {q?.reference_label && <> Day change is measured from the {q.reference_label} ({inr(q.reference_price)}).</>}
                  {(q?.price_notes ?? []).map((n) => <span key={n} className="mt-1 block">{n}</span>)}
                </>} />
              <Stat label="Market cap" icon={<Building2 className="size-4" />} tone="accent"
                display={<span className="num">{q?.market_cap ? crore(q.market_cap) : "—"}</span>}
                hint={q?.issued_shares ? `${Number(q.issued_shares).toLocaleString("en-IN")} shares × ${(q?.price_label ?? "last price").toLowerCase()}` : q?.market_cap_basis ?? undefined}
                help={`Market capitalisation: the market's price tag for the whole company. Basis: ${q?.market_cap_basis ?? "shares issued × price"}.`} />
              <Stat label="52-week position" icon={<ChartCandlestick className="size-4" />} tone="info"
                display={<span className="num">{q?.week52_position != null ? `${Math.round(q.week52_position * 100)}%` : "—"}</span>}
                hint={q?.week52_position != null ? (q.week52_position < 0.2 ? "near its 1-year low" : q.week52_position > 0.8 ? "near its 1-year high" : "mid-range") : undefined}
                help="Where today's price sits between the lowest (0%) and highest (100%) price of the past 52 weeks." />
              <Stat label="Dividend yield (12m)" icon={<Receipt className="size-4" />} tone="gain"
                display={<span className="num">{ov.data?.dividends.ttm_yield != null ? pctOf(ov.data.dividends.ttm_yield) : "—"}</span>}
                hint={ov.data?.dividends.ttm_per_share ? `₹${ov.data.dividends.ttm_per_share}/share with ex-date in the last year` : ov.data?.unreachable?.some((u) => u === "quote" || u === "corporate actions") ? `${ex} unreachable just now` : "no cash dividend in the last year"}
                help="Cash dividends per share with an ex-date in the last 12 months, divided by today's price." />
            </>
          )}
        </div>

        {/* signal + forensic scorecard (computed in Python; screening flags, not advice). The BSE view of a dual-listed
            stock shows its NSE symbol's cards; a BSE-only stock's are computed from BSE history and BSE XBRL */}
        {stockKey && (
          <>
            <SinceReport symbol={stockKey} />
            <div className="grid [&>*]:min-w-0 gap-5 lg:grid-cols-2">
              <StockSignalCard symbol={stockKey} />
              <ForensicCard symbol={stockKey} />
            </div>
            <StockDisclosuresCard symbol={stockKey} isin={ov.data?.quote?.isin} />
            {!onBse && <StockPeersCard symbol={stockKey} />}
            {!onBse && <StockSurpriseCard symbol={stockKey} />}
          </>
        )}

        {/* price chart + range */}
        <div className="grid [&>*]:min-w-0 gap-5 lg:grid-cols-3">
          <Card className="lg:col-span-2" title="Price" subtitle={`Intraday (1D, 5D) and daily bars on ${ex}`} icon={<TrendingUp className="size-4" />}
            help={`1D and 5D show today's (and archived) 1-minute ${ex} prices as candles of your chosen size; 1M and longer show ${ex}'s official daily bars. Your default range, interval and chart type are set on the profile page; this page remembers your last choice.`}>
            {/* the chart renders at once: intraday ranges do not wait for the (slower) daily history */}
            <PriceChart kind="stock" symbol={symbol} exchange={ex} defaults={tf.stock} refreshS={tf.quote_refresh_s} onRange={onRange}
              day={q ? { pct: q.change_pct ?? null, ref: q.reference_price != null ? Number(q.reference_price) : q.previous_close != null ? Number(q.previous_close) : null, label: q.reference_label ?? null } : undefined}
              daily={bars.map((b) => ({ t: b.date, o: b.open, h: b.high, l: b.low, c: b.close, v: b.volume }))}
              dailyLoading={!hist.data || !!histLoadingMore} dailyError={hist.error && !hist.data ? hist.error : null} onDailyRetry={hist.reload}
              dailyEmpty={hist.data && bars.length < 2 ? `${ex} returned no trading days for ${symbol}. It may be suspended or newly listed.` : null} />
            {histLoadingMore && <p className="mt-1 inline-flex items-center gap-1 text-[11px] text-muted"><Loader2 className="size-3 animate-spin" /> Loading longer history…</p>}
            {hist.data?.partial && bars.length > 0 && (
              <div className="mt-2"><Unreachable compact exchange={ex} what={`the older history (the daily chart starts at ${shortDate(bars[0].date)})`} onRetry={hist.retry} /></div>
            )}
            {hist.data && bars.length >= 2 && (
              <div className="mt-4 grid grid-cols-3 gap-2">
                <Metric label={`Return (${hist.data.days >= 1827 ? "5Y" : hist.data.days >= 1096 ? "3Y" : "1Y"})`} value={signedPct(hist.data.stats.return)} tone={toneOf(hist.data.stats.return)}
                  sub={benchReturn(hist.data.stats.return, bench.data?.stats.return)}
                  help="Price return over the period (dividends left out). Below it: the NIFTY 50 over the same dates, through its ETF NIFTYBEES, and the gap in percentage points (pp)." />
                <Metric label="Volatility (yearly)" value={pctOf(hist.data.stats.annualised_volatility, 1)}
                  help="How much the price swings, annualised (standard deviation of daily returns × √252). Higher = bumpier ride." />
                <Metric label="Max drawdown" value={pctOf(hist.data.stats.max_drawdown, 1)} tone="text-loss"
                  sub={hist.data.stats.drawdown_peak ? `${shortDate(hist.data.stats.drawdown_peak)} → ${shortDate(hist.data.stats.drawdown_trough!)}` : undefined}
                  help="The biggest fall from a peak to a later low in this period: the worst loss a buyer at the top would have sat through." />
              </div>
            )}
          </Card>

          <div className="space-y-5">
            {listing?.exchange === "both" && <PriceComparison listing={listing} />}
            <Card title="52-week range" icon={<ChartCandlestick className="size-4" />} help="The lowest and highest prices of the last 52 weeks, with today's price marked.">
              {q?.week52_low && q?.week52_high && last != null ? (
                <RangeBar low={Number(q.week52_low)} high={Number(q.week52_high)} value={last} lowLabel="52w low" highLabel="52w high" />
              ) : ov.data ? (
                <p className="text-sm text-muted">{ov.data?.unreachable?.includes("quote") ? `Couldn't reach ${ex} just now for the quote.` : `${ex} did not give a 52-week range.`}</p>
              ) : (
                <SkeletonRows rows={2} />
              )}
            </Card>
            <Card title="Today" icon={<CalendarClock className="size-4" />}>
              {!ov.data ? (
                <SkeletonRows rows={4} />
              ) : (
                <div className="grid [&>*]:min-w-0 grid-cols-2 gap-2">
                  <Metric label="Open" value={inr(q?.open)} />
                  <Metric label={q?.reference_kind === "listing_price" ? "Issue price" : "Previous close"} value={inr(q?.previous_close)}
                    sub={q?.reference_kind === "base_price" && q.reference_price !== q.previous_close ? `ex-date: base ${inr(q.reference_price)}`
                      : q?.reference_kind === "listing_price" ? `listing day: discovered ${inr(q.reference_price)}` : undefined}
                    help="The last session's close. On an ex-date (dividend, split, bonus) the exchange adjusts it into a base price, and the day change is measured from that." />
                  <Metric label="Day change" value={<Delta value={q?.change_pct} />} sub={q?.change_pct != null ? `vs ${q?.reference_kind === "listing_price" ? "listing price" : q?.reference_kind === "base_price" ? "adjusted base" : q?.reference_kind === "previous_close_less_dividend" ? "prev. close − dividend" : "previous close"}` : undefined} />
                  {q?.price_kind === "official_close" && (
                    <Metric label="Last traded" value={inr(q.last_price)} sub={q.last_differs ? "≠ official close" : "= official close"}
                      help="The last trade of the session. The official close is computed by the exchange from the closing trades, so the two can differ." />
                  )}
                  <Metric label="Last session volume" value={lastBar?.volume != null ? lastBar.volume.toLocaleString("en-IN") : "—"}
                    sub={lastBar ? shortDate(lastBar.date) : undefined} />
                </div>
              )}
            </Card>
          </div>
        </div>

        {/* holding + results */}
        <div className="grid [&>*]:min-w-0 gap-5 lg:grid-cols-2">
          <Card title="Shareholding pattern" icon={<Users className="size-4" />}
            subtitle={split ? `Quarter ended ${day(split.quarters[split.quarters.length - 1].as_of)}, from the filed pattern` : latestHolding?.as_of ? `Quarter ended ${day(latestHolding.as_of)}` : `Latest filing with ${ex}`}
            help="Who owns the company: promoters (founders / controlling group), foreign institutions (FII / FPI), domestic institutions (DII: mutual funds, insurers, banks) and individuals. A falling promoter share can mean selling or dilution; rising institutional ownership often follows improving fundamentals.">
            {split ? (
              <ShareholdingSplit data={split} />
            ) : !ov.data || splitLoading ? (
              <SkeletonRows rows={5} />
            ) : !latestHolding && ov.data.unreachable?.includes("shareholding") ? (
              <Unreachable exchange={ex} what="shareholding pattern" onRetry={ov.retry} />
            ) : !latestHolding ? (
              <EmptyState icon={<PieChart className="size-5" />} title="No shareholding filing">{ex} has no shareholding pattern for {symbol} yet.</EmptyState>
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
                <p className="text-[11px] text-muted">Public = institutions (FIIs, mutual funds, insurers) plus retail. {ex}&apos;s summary gives only promoter, public and employee trusts; the FII / DII split could not be read from the filed pattern{shp.error ? ` (${shp.error})` : ""}.</p>
                {(shp.error || (shp.data?.unreachable?.length ?? 0) > 0) && <Unreachable compact exchange={ex} what="the filed shareholding patterns" onRetry={shp.retry} />}
              </div>
            )}
          </Card>

          <Card title="Quarterly results" icon={<Landmark className="size-4" />}
            subtitle={latestQ ? `${latestQ.consolidated ? "Consolidated" : "Standalone"}, read from each filing's XBRL` : "Revenue and profit from results filings"}
            actions={latestQ ? <Badge tone="accent">Latest: {latestQ.label}{latestQ.filed_at ? `, filed ${day(latestQ.filed_at)}` : ""}</Badge> : undefined}
            help={onBse
              ? "Revenue from operations and profit attributable to shareholders, read from the company's own results filing on BSE. BSE indexes machine-readable results from the March 2025 quarter, when SEBI moved results into Integrated Filing (Financials)."
              : "Revenue from operations and profit attributable to shareholders, read from the company's own results filing on NSE. Since the March 2025 quarter SEBI has companies file results as Integrated Filing (Financials); older quarters come from NSE's financial results index."}>
            {results.error ? (
              <ErrorNote error={results.error} onRetry={results.reload} />
            ) : !results.data ? (
              <Skeleton className="h-[240px] w-full rounded-lg" />
            ) : quarters.length === 0 && (results.data.unreachable?.length ?? 0) > 0 ? (
              <Unreachable exchange={ex} what="results filings" onRetry={results.retry} />
            ) : quarters.length === 0 ? (
              <EmptyState icon={<FileText className="size-5" />} title={`No machine-readable results filed on ${ex}`}>
                {onBse ? `BSE has no integrated-filing results XBRL for ${symbol} (they start with the March 2025 quarter).` : `NSE has no results XBRL for ${symbol} in its integrated filing or financial results indexes.`} Check the announcements below for the latest results PDF.
              </EmptyState>
            ) : (
              <>
                {latestQ && (
                  <div className="mb-3 grid grid-cols-1 gap-2 sm:grid-cols-3">
                    <Metric label={REVENUE_LABEL[latestQ.revenue_basis ?? ""] ?? "Revenue"} value={crore(latestQ.revenue)}
                      help={REVENUE_HELP[latestQ.revenue_basis ?? ""] ?? "Revenue from operations for the quarter."}
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
                <ResultsChart revenueLabel={REVENUE_LABEL[latestQ?.revenue_basis ?? ""] ?? "Revenue"} rows={quarters.map((r) => ({ label: r.label, title: `${r.label}, quarter ended ${day(r.period_end)}`,
                  revenue: r.revenue != null ? r.revenue / 1e7 : null, profit: r.profit != null ? r.profit / 1e7 : null }))} />
                <p className="mt-3 mb-1 text-[11px] font-medium text-muted">Net margin trend</p>
                <MarginChart rows={quarters.map((r) => ({ label: r.label, title: `${r.label}, quarter ended ${day(r.period_end)}`, margin: r.margin }))} />
                <Table label="Quarterly results" className="mt-3">
                  <thead>
                    <tr>
                      <th>Quarter</th>
                      <th className="text-right!">{REVENUE_LABEL[latestQ?.revenue_basis ?? ""] ?? "Revenue"}</th>
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
                            <a href={r.ixbrl ?? r.xbrl} target="_blank" rel="noreferrer" className="inline-flex items-center gap-0.5 text-brand underline underline-offset-2">
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
                    <Table label="Full P&L lines">
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
                    <span key={x.url}>{i ? " and " : ""}<a href={x.url} target="_blank" rel="noreferrer" className="text-brand underline underline-offset-2">{x.name}</a></span>
                  ))} ({ex}), each quarter&apos;s XBRL. Checked {when(results.data.as_of)}.
                  {results.data.errors.length > 0 && ` ${results.data.errors.length} filing${results.data.errors.length > 1 ? "s" : ""} could not be read.`}
                </p>
                {(results.data.unreachable?.length ?? 0) > 0 && (
                  <div className="mt-1"><Unreachable compact exchange={ex} what={results.data.unreachable!.join(", ")} onRetry={results.retry} /></div>
                )}
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
            ) : ov.data.corporate_actions.length === 0 && ov.data.unreachable?.includes("corporate actions") ? (
              <Unreachable exchange={ex} what="corporate actions" onRetry={ov.retry} />
            ) : ov.data.corporate_actions.length === 0 ? (
              <EmptyState icon={<CalendarClock className="size-5" />} title="No corporate actions">{ex} lists no dividends, bonuses or splits for {symbol}.</EmptyState>
            ) : (
              <ScrollArea axis="y" focusable role="region" aria-label="Corporate actions" className="max-h-[420px] pt-1 pr-1 pb-3 pl-1.5">
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
              </ScrollArea>
            )}
          </Card>

          <Card title="Announcements" icon={<Megaphone className="size-4" />} subtitle={`Latest filings with ${ex}`}>
            {!ov.data ? (
              <SkeletonRows rows={6} />
            ) : ov.data.announcements.length === 0 && ov.data.unreachable?.includes("announcements") ? (
              <Unreachable exchange={ex} what="announcements" onRetry={ov.retry} />
            ) : ov.data.announcements.length === 0 ? (
              <EmptyState icon={<Megaphone className="size-5" />} title="No announcements">
                {ov.data.errors.some((e) => e.startsWith("announcements")) ? `${ex}'s announcements list could not be read just now.` : `${ex} lists no recent announcements for ${symbol}.`}
              </EmptyState>
            ) : (
              <ScrollArea axis="y" focusable role="region" aria-label="Announcements" className="-mx-2 max-h-[420px] pb-2">
              <ul className="space-y-0.5 stagger">
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
              </ScrollArea>
            )}
          </Card>
        </div>

        {ov.data && (
          <p className="text-[11px] text-muted">
            Source: {(() => {
              // a person opens the exchange's stock page; the exact JSON request is a separate, labelled link
              const page = ov.data.quote_page === undefined ? ov.data.source : ov.data.quote_page;
              const request = ov.data.sources?.quote;
              return <>
                {page ? <a href={page} target="_blank" rel="noreferrer" className="underline underline-offset-2">{ex} stock page</a> : <>{ex}</>}
                {request && <> (<DataRequest href={request} label={page ? "data request" : "quote data request"} />)</>}
              </>;
            })()}, fetched {when(ov.data.fetched_at)} (cached up to 10 minutes). Not investment advice.
          </p>
        )}
      </div>
    </div>
  );
}

/** What stands in for revenue in a bank's or insurer's results filing. */
const REVENUE_LABEL: Record<string, string> = {
  interest_earned: "Interest earned", net_premium_income: "Net premium", premium_earned: "Premium earned",
};
const REVENUE_HELP: Record<string, string> = {
  interest_earned: "Banks report interest earned in place of revenue from operations.",
  net_premium_income: "Life insurers report net premium income (after reinsurance) in place of revenue.",
  premium_earned: "General insurers report net premium earned in the period in place of revenue.",
};

/** QoQ and YoY change under a results KPI (fractions from the API). */
function GrowthSub({ qoq, yoy }: { qoq: number | null | undefined; yoy: number | null | undefined }) {
  return (
    <span className="inline-flex flex-wrap gap-x-2">
      <span>QoQ <Delta value={qoq != null ? qoq * 100 : null} digits={1} /></span>
      <span>YoY <Delta value={yoy != null ? yoy * 100 : null} digits={1} /></span>
    </span>
  );
}

/** "NIFTY 50 −4.1% · −27.0 pp": the benchmark's price return and the gap, or nothing until it has loaded. */
function benchReturn(own: number | string | null | undefined, nifty: number | string | null | undefined) {
  if (own == null || nifty == null) return undefined;
  const a = Number(own) * 100, b = Number(nifty) * 100;
  if (!Number.isFinite(a) || !Number.isFinite(b)) return undefined;
  const gap = a - b;
  return `NIFTY 50 ${b >= 0 ? "+" : "−"}${Math.abs(b).toFixed(1)}% · ${gap >= 0 ? "+" : "−"}${Math.abs(gap).toFixed(1)} pp ${gap >= 0 ? "ahead" : "behind"}`;
}

"use client";

import { ArrowUpRight, BellPlus, BellRing, ChartCandlestick, Eye, FileText, FlaskConical, Star } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { Sparkline } from "@/components/charts";
import { ensureStock, startResearch, watchStock } from "@/components/markets/actions";
import { LinkRow, SearchBox, inr, signedPct, toneOf } from "@/components/markets/common";
import { ExchangeBadge } from "@/components/markets/exchange";
import type { StockHistory, StockHit } from "@/components/markets/types";
import { Badge, Button, Callout, Card, EmptyState, ErrorNote, PageHeader, SkeletonRows, Table, cx } from "@/components/ui";
import { api, type Company, day, useApi, type WatchSummary } from "@/lib/api";

export default function Stocks() {
  const router = useRouter();
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<StockHit[] | null>(null);
  const [searching, setSearching] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const companies = useApi<Company[]>("/api/companies");
  const watches = useApi<WatchSummary[]>("/api/watches");

  const stockWatches = (watches.data ?? []).filter((w) => w.kind === "stock" && w.active);
  const watched = new Set(stockWatches.map((w) => w.key ?? w.nse_symbol));
  const researched = (companies.data ?? []).filter((c) => c.latest_run && c.kind === "stock_report");

  const search = async () => {
    if (!q.trim()) return;
    setSearching(true);
    try {
      setHits(await api<StockHit[]>(`/api/stocks/search?q=${encodeURIComponent(q.trim())}`));
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSearching(false);
    }
  };

  const research = async (symbol: string) => {
    const where = symbol.startsWith("BSE:") ? `BSE ${symbol.slice(4)}` : symbol;
    if (!confirm(`Start a full stock research run for ${where}? It uses your Claude plan window.`)) return;
    setBusy(symbol);
    try {
      const runId = await startResearch(await ensureStock(symbol), "stock_report");
      router.push(`/runs/${runId}`);
    } catch (e) {
      setError((e as Error).message);
      setBusy(null);
    }
  };

  const watch = async (symbol: string) => {
    setBusy(symbol);
    try {
      await watchStock(symbol);
      const where = symbol.startsWith("BSE:") ? `BSE ${symbol.slice(4)} on BSE` : `${symbol} on NSE`;
      setNote(`Watching ${where}: results filings, corporate actions, holding changes and big moves are checked after each close.`);
      companies.reload();
      watches.reload();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  return (
    <div>
      <PageHeader
        icon={<ChartCandlestick className="size-5" />}
        eyebrow="Markets"
        title="Stocks"
        description="Look up any company listed on NSE or BSE: live price, 52-week range, shareholding, results and corporate actions. Watch it for filings, or start a full research run."
      />

      <div className="space-y-5">
        <Card title="Find a listed stock" subtitle="Search NSE's and BSE's equity lists by symbol, BSE code or company name" icon={<ChartCandlestick className="size-4" />}>
          <SearchBox value={q} onChange={setQ} onSubmit={search} busy={searching} placeholder="Symbol or company name, e.g. INFY or Infosys" />
          <div className="mt-3 space-y-2">
            <ErrorNote error={error} onRetry={q.trim() ? search : undefined} />
            {note && (
              <Callout tone="gain" icon={<BellRing className="size-4" />}>
                {note}
              </Callout>
            )}
          </div>
          {hits && hits.length === 0 && (
            <div className="mt-4">
              <EmptyState title="No listed equity matches">
                Try the symbol (e.g. TCS), the 6-digit BSE code or one distinctive word of the company name. Listed equities on NSE and BSE appear here; for IPOs use the IPOs page.
              </EmptyState>
            </div>
          )}
          {hits && hits.length > 0 && hits[0].bse_error && (
            <p className="mt-3 text-xs text-warn">BSE&apos;s scrip list could not be loaded just now, so only NSE listings are shown ({hits[0].bse_error}).</p>
          )}
          {hits && hits.length > 0 && (
            <div className="mt-4">
              <Table label="Stock search results">
                <thead>
                  <tr>
                    <th>Stock</th>
                    <th className="hidden sm:table-cell">Listed since</th>
                    <th className="text-right!">Actions</th>
                  </tr>
                </thead>
                <tbody className="stagger">
                  {hits.map((h) => {
                    const href = `/stocks/${encodeURIComponent(h.key)}`;
                    // watches and research runs follow the NSE symbol when there is one, else the BSE key (BSE-only)
                    const k = h.nse_symbol ?? h.key;
                    const on = h.nse_symbol ? "NSE" : "BSE";
                    return (
                      <tr key={h.key}>
                        <td className="max-w-[16rem] sm:max-w-none">
                          <Link href={href} className="group block">
                            <span className="flex flex-wrap items-center gap-1.5 font-semibold group-hover:text-brand">
                              {h.symbol}
                              <ExchangeBadge exchange={h.exchange} group={h.bse_group} />
                              {watched.has(k) && <Badge tone="brand">watching</Badge>}
                              {h.slug && <Badge tone="accent">in library</Badge>}
                            </span>
                            <span className="block truncate text-xs text-muted">
                              {h.name}{h.bse_code && <span className="num"> · BSE {h.bse_code}</span>}
                            </span>
                          </Link>
                        </td>
                        <td className="hidden text-muted sm:table-cell">{h.listed ? day(h.listed) : "—"}</td>
                        <td>
                          <div className="flex justify-end gap-1.5">
                            <Link href={href}
                              className="inline-flex h-8 items-center gap-1.5 rounded-lg bg-card px-3 text-xs font-medium ring-1 ring-inset ring-border transition hover:bg-card-hover hover:ring-border-strong">
                              <Eye className="size-3.5" /> View
                            </Link>
                            <Button variant="secondary" disabled={busy === k || watched.has(k)} onClick={() => watch(k)}
                              icon={<BellPlus className="size-3.5" />}
                              title={`Check ${on} filings, actions and big moves after each close`}>
                              <span className="hidden sm:inline">{watched.has(k) ? "Watching" : "Watch"}</span>
                            </Button>
                            <Button disabled={busy === k} onClick={() => research(k)} icon={<FlaskConical className="size-3.5" />}
                              title={`Full research run on ${on} data and filings (uses your Claude plan)`}>
                              <span className="hidden sm:inline">Research</span>
                            </Button>
                          </div>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </Table>
            </div>
          )}
          {!hits && (
            <p className="mt-3 flex flex-wrap items-center gap-1.5 text-xs text-muted">
              Try
              {[["INFY", "INFY"], ["RELIANCE", "RELIANCE"], ["HDFCBANK", "HDFCBANK"], ["TCS", "TCS"], ["BSE:526433", "ASMTEC (BSE)"]].map(([key, label]) => (
                <Link key={key} href={`/stocks/${encodeURIComponent(key)}`} className="rounded-full bg-background-subtle px-2 py-0.5 font-medium text-foreground ring-1 ring-inset ring-border transition hover:ring-brand">
                  {label}
                </Link>
              ))}
            </p>
          )}
        </Card>

        <div className="grid [&>*]:min-w-0 gap-5 lg:grid-cols-2">
          <Card title="Watchlist" subtitle="Stocks checked after every market close · last daily price and 1-month change" icon={<Star className="size-4" />}
            actions={<Link href="/monitor" className="text-xs font-medium text-brand hover:underline">Monitor →</Link>}>
            {watches.error ? (
              <ErrorNote error={watches.error} onRetry={watches.reload} />
            ) : !watches.data ? (
              <SkeletonRows rows={3} />
            ) : stockWatches.length === 0 ? (
              <EmptyState icon={<BellPlus className="size-5" />} title="No stocks watched yet">
                Search above and press Watch: FinResearch then checks results filings, corporate actions, holding changes and big price moves after each close.
              </EmptyState>
            ) : (
              <ul className="-mx-2 stagger">
                {stockWatches.map((w) => (
                  <WatchRow key={w.id} w={w} />
                ))}
              </ul>
            )}
          </Card>

          <Card title="Researched stocks" subtitle="Companies with a stock research report" icon={<FileText className="size-4" />}>
            {companies.error ? (
              <ErrorNote error={companies.error} onRetry={companies.reload} />
            ) : !companies.data ? (
              <SkeletonRows rows={3} />
            ) : researched.length === 0 ? (
              <EmptyState icon={<FlaskConical className="size-5" />} title="No stock research yet">
                Find a stock above and press Research to run the full multi-agent analysis with cited sources.
              </EmptyState>
            ) : (
              <ul className="-mx-2 stagger">
                {researched.map((c) => (
                  <li key={c.slug} className="flex items-center gap-2 rounded-lg px-2 py-2 transition hover:bg-card-hover">
                    <Link href={c.key ? `/stocks/${encodeURIComponent(c.key)}` : `/runs/${c.latest_run}`} className="min-w-0 flex-1">
                      <p className="line-clamp-2 text-sm font-medium break-words hover:text-brand">{c.name}</p>
                      <p className="num text-xs text-muted">{c.nse_symbol ? `NSE ${c.nse_symbol}` : c.bse_code ? `BSE ${c.bse_code}` : ""}</p>
                    </Link>
                    <Link href={`/runs/${c.latest_run}/report`}
                      className="inline-flex items-center gap-1 rounded-lg px-2 py-1 text-xs font-medium text-brand ring-1 ring-inset ring-brand/25 transition hover:bg-brand-soft">
                      Report (run {c.latest_run}) <ArrowUpRight className="size-3" />
                    </Link>
                  </li>
                ))}
              </ul>
            )}
          </Card>
        </div>
      </div>
    </div>
  );
}

/** A watched stock with its last month of closes as a sparkline. */
function WatchRow({ w }: { w: WatchSummary }) {
  const key = w.key ?? w.nse_symbol ?? "";
  const h = useApi<StockHistory>(`/api/stocks/${encodeURIComponent(key)}/history?days=35`);
  const bars = h.data?.bars ?? [];
  const closes = bars.map((b) => b.close);
  // the price is the last daily bar, so its stamp is that bar's date (the history has no intraday time)
  const lastDay = bars.length ? bars[bars.length - 1].date : null;
  const last = closes[closes.length - 1];
  const change = closes.length > 1 ? last / closes[0] - 1 : null;
  return (
    <LinkRow
      href={`/stocks/${encodeURIComponent(key)}`}
      title={
        <span className="flex items-center gap-1.5">
          {w.label ?? w.nse_symbol}
          {w.exchange === "BSE" && <Badge tone="accent">BSE only</Badge>}
          {!!w.unread_alerts && <Badge tone="warn">{w.unread_alerts} new</Badge>}
        </span>
      }
      sub={w.company_name}
      trailing={
        <span className="flex items-center gap-3">
          {h.data ? <Sparkline values={closes} width={84} height={28} /> : <span className="skeleton h-7 w-[84px] rounded-md" />}
          <span className="w-24 text-right">
            <span className="num block text-sm font-medium">{last != null ? inr(last) : "—"}</span>
            <span className={cx("num block text-[11px]", toneOf(change))}>{change != null ? `${signedPct(change)} 1M` : h.error ? "no data" : " "}</span>
            {lastDay && <span className="block text-[10px] text-muted">as of {day(lastDay)}</span>}
          </span>
        </span>
      }
    />
  );
}

"use client";

// Stock peer table (issue #178): the company against up to 15 listed peers of its NSE industry, with the peers'
// median, IQR and the company's percentile per metric. Peers come from the nightly build (GET /api/stocks/{symbol}/peers);
// only the company's own row is fetched live. No metric is labelled better or worse.

import { ArrowDown, ArrowUp, ArrowUpDown, Info, Users } from "lucide-react";
import Link from "next/link";
import { useMemo, useState } from "react";

import { crore, inr, pctOf, signedPct } from "@/components/markets/common";
import type { PeerMetricKey, PeerRow, PeerSummary, PeerValue, StockPeers } from "@/components/markets/types";
import { Badge, Callout, Card, ErrorNote, Skeleton, Table, cx } from "@/components/ui";
import { day, useApi } from "@/lib/api";

const LABEL: Record<PeerMetricKey, string> = {
  price: "Price", market_cap: "Mkt cap", pe: "P/E", pb: "P/B", roe: "ROE", revenue_growth: "Rev growth",
  pat_growth: "PAT growth", pat_margin: "PAT margin", return_1y: "1y return",
};

const HELP: Record<PeerMetricKey, string> = {
  price: "Last traded price, or the official close after the session.",
  market_cap: "Issued shares × price.",
  pe: "Price ÷ trailing EPS (sum of the latest four quarters' basic EPS, one basis). n/m when that EPS is negative or zero.",
  pb: "Market cap ÷ equity attributable to owners on the latest filed balance sheet.",
  roe: "Trailing twelve months' profit to owners ÷ average owners' equity at the start and end of those months (to the latest balance-sheet date).",
  revenue_growth: "Trailing twelve months' revenue vs the twelve months before.",
  pat_growth: "Trailing twelve months' profit to owners vs the twelve months before; n/m when the earlier figure was a loss.",
  pat_margin: "Trailing twelve months' profit to owners ÷ revenue.",
  return_1y: "Price change over a year, adjusted for splits and bonuses; dividends not added.",
};

/** One metric value as text ("n/m" when not meaningful, "—" when missing). */
export function fmtPeer(m: PeerMetricKey, v: number | null | undefined): string {
  if (v == null) return "—";
  if (m === "price") return inr(v);
  if (m === "market_cap") return crore(v);
  if (m === "pe" || m === "pb") return `${v.toFixed(1)}×`;
  if (m === "revenue_growth" || m === "pat_growth" || m === "return_1y") return signedPct(v, 1);
  return pctOf(v, 1);
}

function cell(m: PeerMetricKey, x: PeerValue | undefined): { text: string; title: string } {
  if (!x || x.value == null) {
    const r = x?.reason ?? "not available";
    return { text: r.startsWith("n/m") ? "n/m" : "—", title: r };
  }
  return { text: fmtPeer(m, x.value), title: [x.period, x.basis].filter(Boolean).join(" · ") };
}

type SortKey = PeerMetricKey | "name";

function sortRows(rows: PeerRow[], key: SortKey, desc: boolean): PeerRow[] {
  const out = [...rows];
  out.sort((a, b) => {
    if (key === "name") return (a.name ?? a.symbol).localeCompare(b.name ?? b.symbol) * (desc ? -1 : 1);
    const va = a.metrics[key]?.value, vb = b.metrics[key]?.value;
    if (va == null && vb == null) return a.symbol.localeCompare(b.symbol);
    if (va == null) return 1; // missing values always last
    if (vb == null) return -1;
    return (va - vb) * (desc ? -1 : 1) || a.symbol.localeCompare(b.symbol);
  });
  return out;
}

function SortIcon({ on, desc }: { on: boolean; desc: boolean }) {
  if (!on) return <ArrowUpDown className="size-3 opacity-50" />;
  return desc ? <ArrowDown className="size-3" /> : <ArrowUp className="size-3" />;
}

/** "Q1–Q3" of the peers, or why there is none. */
function iqr(m: PeerMetricKey, s: PeerSummary | undefined): string {
  if (!s || s.median == null) return s?.reason ?? "—";
  return `${fmtPeer(m, s.q1)} – ${fmtPeer(m, s.q3)}`;
}

export function StockPeersCard({ symbol }: { symbol: string }) {
  const r = useApi<StockPeers>(`/api/stocks/${encodeURIComponent(symbol)}/peers`);
  const [sort, setSort] = useState<{ key: SortKey; desc: boolean }>({ key: "market_cap", desc: true });
  const d = r.data;
  const metrics: PeerMetricKey[] = d?.metrics ?? [];
  const rows = useMemo(
    () => (d?.company && d.peers ? sortRows([d.company, ...d.peers], sort.key, sort.desc) : []),
    [d, sort],
  );
  const toggle = (key: SortKey) => setSort((s) => (s.key === key ? { key, desc: !s.desc } : { key, desc: key !== "name" }));
  const ariaSort = (key: SortKey) => (sort.key !== key ? "none" : sort.desc ? "descending" : "ascending");
  const ok = d?.status === "ok" && d.company && d.peers && d.summary;
  const subtitle = ok
    ? <>{d.peers!.length} of {d.candidates} {d.universe_note ? "" : `${d.universe} `}stocks in NSE {d.level_label} “{d.industry}” · peers as of {day(d.as_of!)}</>
    : "Same NSE industry, nearest by market cap";

  return (
    <Card title="Peers" icon={<Users className="size-4" />} subtitle={subtitle}
      help="Peers share the company's NSE industry classification at the finest level that has at least 5 listed peers; the 15 nearest in market cap are shown. Median and IQR (middle half) are the peers' own, without the company. The percentile says where the company sits among them; for P/E and P/B a high percentile means priced higher, not better.">
      {r.error && <ErrorNote error={r.error} onRetry={r.reload} />}
      {!d && !r.error && <Skeleton className="h-[200px] w-full rounded-lg" />}
      {d && d.status !== "ok" && <Callout tone="info" icon={<Info className="size-4" />}>{d.message}</Callout>}
      {ok && (
        <div className="space-y-3">
          {d.company_error && (
            <p className="text-xs text-muted"><Badge tone="warn">Stored row</Badge> The company could not be read live ({d.company_error}); its nightly row is shown.</p>
          )}
          <Table label={`${symbol}: peer comparison`} className="!mx-0">
            <thead>
              <tr>
                <th aria-sort={ariaSort("name")} className="sticky left-0 z-10 bg-card">
                  <button type="button" onClick={() => toggle("name")} className="inline-flex items-center gap-1 uppercase hover:text-foreground">Company <SortIcon on={sort.key === "name"} desc={sort.desc} /></button>
                </th>
                {metrics.map((m) => (
                  <th key={m} className="text-right" aria-sort={ariaSort(m)}>
                    <button type="button" title={HELP[m]} onClick={() => toggle(m)} className="inline-flex items-center gap-1 uppercase hover:text-foreground">
                      {LABEL[m]} <SortIcon on={sort.key === m} desc={sort.desc} />
                    </button>
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((p) => {
                const me = p.symbol === d.company!.symbol;
                return (
                  <tr key={p.symbol} className={cx(me && "bg-brand-soft/60 font-medium")} aria-current={me ? "true" : undefined}>
                    <td className={cx("sticky left-0 z-10 max-w-[12rem] truncate", me ? "bg-brand-soft" : "bg-card")}>
                      {me ? <span title={p.name ?? p.symbol}>{p.symbol}</span>
                        : <Link href={`/stocks/${p.symbol}`} className="hover:text-brand" title={p.name ?? p.symbol}>{p.symbol}</Link>}
                      {p.stale && <span className="ml-1 text-[10px] text-muted" title="This stock could not be read in the last nightly build; its previous row is shown.">stale</span>}
                    </td>
                    {metrics.map((m) => {
                      const c = cell(m, p.metrics[m]);
                      return <td key={m} className="num text-right text-xs" title={c.title}>{c.text}</td>;
                    })}
                  </tr>
                );
              })}
            </tbody>
            <tfoot className="border-t border-border text-xs text-muted [&_td]:px-4 [&_td]:py-2 sm:[&_td]:px-5">
              <tr>
                <td className="sticky left-0 bg-card">Peer median</td>
                {metrics.map((m) => <td key={m} className="num text-right" title={`${d.summary![m]?.n ?? 0} peers with a value`}>{d.summary![m]?.median == null ? "—" : fmtPeer(m, d.summary![m].median)}</td>)}
              </tr>
              <tr>
                <td className="sticky left-0 bg-card">IQR (Q1 – Q3)</td>
                {metrics.map((m) => <td key={m} className="num whitespace-nowrap text-right">{iqr(m, d.summary![m])}</td>)}
              </tr>
              <tr>
                <td className="sticky left-0 bg-card">{d.company!.symbol} percentile</td>
                {metrics.map((m) => {
                  const pc = d.summary![m]?.percentile;
                  return <td key={m} className="num text-right">{pc == null ? "—" : Math.round(pc)}</td>;
                })}
              </tr>
            </tfoot>
          </Table>
          <div className="space-y-1 text-[11px] text-muted">
            <p>
              {d.company!.symbol}: price {d.company_live ? "live" : "stored"} as of {d.company!.price_as_of}; results read {d.company!.results_read ?? "—"}.
              Hover a value for its period and basis, a dash for why it is missing. n/m = not meaningful (e.g. a loss makes P/E meaningless).
            </p>
            <ul className="list-disc pl-4">{(d.caveats ?? []).map((c) => <li key={c}>{c}</li>)}</ul>
            <p>Source: NSE quotes and each company&apos;s results XBRL (NSE Integrated Filing / Financial Results); universe: <a className="text-brand hover:underline" href={d.universe_source} target="_blank" rel="noreferrer">{d.universe} constituents</a>{d.universe_note ? `, ${d.universe_note}` : ""}. Computed by FinResearch. Not investment advice.</p>
          </div>
        </div>
      )}
    </Card>
  );
}

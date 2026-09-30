"use client";

// The fund page's look-through card: this scheme's month-end portfolio (file status), its overlap with the user's
// other funds and with any chosen fund, active share against an index-fund proxy of its benchmark, and how much its
// holdings moved between the stored months. Arithmetic on published portfolios; personal, non-advisory.

import { Layers, Search } from "lucide-react";
import Link from "next/link";
import { useEffect, useState } from "react";

import { Badge, Card, InfoTip, Skeleton, cx, inputClass } from "@/components/ui";
import { api, useApi } from "@/lib/api";

import { FileBadge, type FileStatus, HoldingsFileActions, type OverlapPair, overlapFill } from "./lookthrough-panel";

type Held = { code: string | null; name: string; key: string | null; available: boolean } & Partial<OverlapPair>;
type Style = { month: string; equity_pct: number | null; drift_pct: number | null; holdings: number; caps: Record<string, number | null> };
type ActiveShare = {
  active_share: number | null; benchmark: string | null; reason: string | null; method: string;
  proxy?: { key: string; name: string; month: string; chosen: boolean }; same_month?: boolean; candidates: { key: string; name: string }[];
};
type FundLook = {
  code: string; name: string | null; key: string | null; file: FileStatus; auto_fetch: boolean; url_mode: boolean; source_page: string | null;
  top?: { isin: string; name: string; industry: string | null; weight: number | null; cap: string }[];
  active_share?: ActiveShare; style?: Style[]; held: Held[]; is_held: boolean; disclaimer: string;
  with?: { code: string; name: string | null; available: boolean; auto_fetch: boolean; url_mode: boolean } & Partial<OverlapPair>;
};
type SchemeHit = { scheme_code: string; name: string; plan: string | null; option: string | null };

const pct = (v: number | null | undefined, d = 1) => (v == null ? "—" : `${v.toFixed(d)}%`);

function OverlapRow({ name, href, o }: { name: string; href?: string; o: Partial<OverlapPair> & { available: boolean } }) {
  return (
    <div className="flex items-center gap-3 py-1.5 text-sm">
      <div className="min-w-0 flex-1">
        {href ? <Link href={href} className="block truncate hover:text-brand" title={name}>{name}</Link> : <span className="block truncate" title={name}>{name}</span>}
        {o.available && o.top && o.top.length > 0 && (
          <p className="truncate text-[11px] text-muted" title={o.top.map((t) => t.name).join(", ")}>
            {o.common} common · largest: {o.top.slice(0, 3).map((t) => t.name.replace(/ Limited$/, "")).join(", ")}
          </p>
        )}
        {!o.available && <p className="text-[11px] text-muted">no holdings file for this fund yet</p>}
      </div>
      {o.available && (
        <span className="num w-16 shrink-0 rounded-[4px] py-1 text-center text-xs" style={{ background: overlapFill(o.overlap_pct) }}
          title="Σ min(weight) over the stocks both hold (SEBI Annexure 1A)">
          {pct(o.overlap_pct, 0)}
        </span>
      )}
    </div>
  );
}

export function FundOverlapCard({ code }: { code: string }) {
  const [withCode, setWithCode] = useState<string | null>(null);
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<SchemeHit[]>([]);
  const r = useApi<FundLook>(`/api/lookthrough/funds/${code}${withCode ? `?with=${withCode}` : ""}`);
  const d = r.data;

  useEffect(() => {
    const term = q.trim();
    if (term.length < 3) return;
    let alive = true;
    const t = setTimeout(() => {
      api<SchemeHit[]>(`/api/funds/search?q=${encodeURIComponent(term)}`).then((x) => alive && setHits(x.slice(0, 6))).catch(() => alive && setHits([]));
    }, 300);
    return () => { alive = false; clearTimeout(t); };
  }, [q]);

  const style = d?.style ?? [];
  const lastDrift = style.length > 1 ? style[style.length - 1] : null;
  const as = d?.active_share;

  return (
    <Card title="Holdings overlap" icon={<Layers className="size-4" />}
      subtitle="From the fund house's month-end portfolio"
      help="Overlap = for each stock both funds hold, the smaller weight, added up (SEBI Master Circular for MFs, Annexure 1A). A description of shared holdings, not a recommendation."
      actions={d ? <FileBadge file={d.file} /> : undefined}>
      {!d ? <Skeleton className="h-40" /> : (
        <div className="space-y-4">
          {!d.file.available ? (
            <div className="space-y-2">
              <p className="text-sm text-muted">No month-end portfolio stored for this scheme yet.</p>
              <HoldingsFileActions code={code} auto={d.auto_fetch} urlMode={d.url_mode} page={d.source_page} onDone={r.reload} />
            </div>
          ) : (
            <>
              <div className="grid grid-cols-2 gap-3 text-xs sm:grid-cols-3">
                <div>
                  <p className="text-muted">Equity in the fund</p>
                  <p className="num text-base font-semibold">{pct(d.file.equity_pct)}</p>
                </div>
                <div>
                  <p className="flex items-center gap-1 text-muted">Active share
                    <InfoTip>{as?.method ?? ""} Descriptive only: whether it predicts returns is disputed.</InfoTip></p>
                  <p className="num text-base font-semibold">{pct(as?.active_share)}</p>
                  <p className="text-[11px] text-muted">{as?.active_share != null ? `vs ${as.proxy?.name} (proxy for ${as.benchmark})` : as?.reason}</p>
                </div>
                <div>
                  <p className="flex items-center gap-1 text-muted">Holdings moved
                    <InfoTip>½ Σ |weight this month − weight last month| over the fund&apos;s stocks (equity re-scaled to 100%). 0% = same portfolio; includes price moves, so it is not the reported turnover ratio.</InfoTip></p>
                  <p className="num text-base font-semibold">{lastDrift ? pct(lastDrift.drift_pct) : "—"}</p>
                  <p className="text-[11px] text-muted">{lastDrift ? `${style[style.length - 2].month} → ${lastDrift.month}` : "needs two stored months"}</p>
                </div>
              </div>

              <div>
                <p className="mb-1 text-xs font-medium text-muted">{d.is_held ? "With your other funds" : "With the funds you hold"}</p>
                {d.held.length === 0 ? <p className="text-sm text-muted">No other funds in your portfolio.</p> : (
                  <div className="divide-y divide-border/60">
                    {d.held.map((h) => <OverlapRow key={h.key ?? h.name} name={h.name} href={h.code ? `/funds/${h.code}` : undefined} o={h} />)}
                  </div>
                )}
              </div>

              <div>
                <p className="mb-1 text-xs font-medium text-muted">Compare with another fund</p>
                <div className="relative">
                  <Search className="pointer-events-none absolute left-2.5 top-2.5 size-3.5 text-muted" />
                  <input className={cx(inputClass, "h-8 w-full pl-8 text-xs")} placeholder="Search a scheme (3+ letters)" value={q}
                    onChange={(e) => { setQ(e.target.value); if (e.target.value.trim().length < 3) setHits([]); }} aria-label="Search a scheme to compare" />
                </div>
                {hits.length > 0 && (
                  <div className="mt-1 flex flex-wrap gap-1.5">
                    {hits.map((h) => (
                      <button key={h.scheme_code} type="button" onClick={() => { setWithCode(h.scheme_code); setHits([]); setQ(""); }}
                        className="rounded-md px-2 py-1 text-[11px] ring-1 ring-inset ring-border hover:bg-card-hover">
                        {h.name} <span className="text-muted">{h.plan ?? ""}</span>
                      </button>
                    ))}
                  </div>
                )}
                {d.with && (
                  <div className="mt-2 rounded-lg border border-border p-2">
                    <OverlapRow name={d.with.name ?? d.with.code} href={`/funds/${d.with.code}`} o={d.with} />
                    {!d.with.available && <HoldingsFileActions code={d.with.code} auto={d.with.auto_fetch} urlMode={d.with.url_mode} onDone={r.reload} compact />}
                  </div>
                )}
              </div>

              {d.top && d.top.length > 0 && (
                <details className="text-xs">
                  <summary className="cursor-pointer text-muted hover:text-foreground">Top holdings ({d.file.month})</summary>
                  <ul className="mt-2 space-y-1">
                    {d.top.slice(0, 10).map((t) => (
                      <li key={t.isin} className="flex justify-between gap-2">
                        <span className="truncate">{t.name} <span className="text-muted">· {t.cap}</span></span>
                        <span className="num">{pct(t.weight, 2)}</span>
                      </li>
                    ))}
                  </ul>
                </details>
              )}
              <div className="flex flex-wrap items-center justify-between gap-2 text-[11px] text-muted">
                <span>{d.file.stale && <Badge tone="warn">older than 45 days</Badge>} {d.disclaimer}</span>
                <Link href="/portfolio/lookthrough" className="hover:text-brand">Portfolio look-through →</Link>
              </div>
            </>
          )}
        </div>
      )}
    </Card>
  );
}

"use client";

// Fund category rank and percentile (issue #177): where a scheme sits among the direct-growth funds of its SEBI
// category on each metric, and the whole category as a sortable table. Reads the stored nightly ranking only
// (GET /api/funds/{code}/category-rank); nothing here makes the API fetch NAV histories.

import { ArrowDown, ArrowUp, ArrowUpDown, Info, Trophy } from "lucide-react";
import Link from "next/link";
import { useEffect, useMemo, useRef, useState } from "react";

import { Metric, pctOf } from "@/components/markets/common";
import type { FundCategoryRank, RankMetric, RankMetricKey, RankSummary, RankedFund } from "@/components/markets/types";
import { Badge, Callout, Card, ErrorNote, Skeleton, Table, cx } from "@/components/ui";
import { day, useApi } from "@/lib/api";

export const METRIC_SHORT: Record<RankMetricKey, string> = {
  cagr_1y: "1y", cagr_3y: "3y", cagr_5y: "5y", consistency_3y: "consistency", sortino_3y: "Sortino",
  max_drawdown_3y: "drawdown", ter: "TER",
};

const HELP: Record<RankMetricKey, string> = {
  cagr_1y: "Return over the year to the as-of month-end.",
  cagr_3y: "Yearly growth rate (CAGR) over the 3 years to the as-of month-end.",
  cagr_5y: "Yearly growth rate (CAGR) over the 5 years to the as-of month-end.",
  consistency_3y: "Of the 25 rolling 1-year periods ending each month over the last 3 years, the share in which the fund's return was above the category median for the same period.",
  sortino_3y: "Return above the minimum acceptable return (MAR) per unit of downside risk, from 36 monthly returns, annualised. Infinite (∞) when no month fell short of the MAR.",
  max_drawdown_3y: "Worst fall from a month-end peak to a later month-end low over 3 years. Closer to 0 is better; month-end NAVs miss falls that recovered within a month.",
  ter: "Direct plan's total expense ratio, % a year, from AMFI's TER file. Lower is better.",
};

/** One metric's value as text. */
export function fmtRankValue(f: RankedFund, m: RankMetricKey) {
  const v = f.values[m];
  if (m === "sortino_3y" && f.no_downside) return "∞";
  if (v == null) return "—";
  if (m === "sortino_3y") return v.toFixed(2);
  if (m === "ter") return `${v.toFixed(2)}%`;
  if (m === "consistency_3y") return `${Math.round(v * 100)}%`;
  return pctOf(v, 1);
}

type SortKey = RankMetricKey | "name";

export function FundCategoryRankCard({ code }: { code: string }) {
  const r = useApi<FundCategoryRank>(`/api/funds/${code}/category-rank`);
  const [sort, setSort] = useState<{ key: SortKey; desc: boolean }>({ key: "cagr_3y", desc: false });
  const [showExcluded, setShowExcluded] = useState(false);
  const d = r.data;
  const cat = d?.category;
  const metrics: RankMetric[] = d?.metrics ?? [];

  const rows = useMemo(() => {
    if (!cat) return [];
    const out = [...cat.funds];
    const k = sort.key;
    out.sort((a, b) => {
      if (k === "name") return a.name.localeCompare(b.name) * (sort.desc ? -1 : 1);
      // by rank (1 = best); funds without the metric always go last
      const ra = a.ranks[k], rb = b.ranks[k];
      if (ra == null && rb == null) return a.name.localeCompare(b.name);
      if (ra == null) return 1;
      if (rb == null) return -1;
      return (ra - rb) * (sort.desc ? -1 : 1) || a.name.localeCompare(b.name);
    });
    return out;
  }, [cat, sort]);

  // the viewed fund's row sits inside a scrolling table: bring it into view when the table loads or re-sorts
  const mine = useRef<HTMLTableRowElement>(null);
  useEffect(() => {
    const row = mine.current, box = row?.closest<HTMLElement>("[role=region]");
    if (row && box) box.scrollTop = Math.max(0, row.offsetTop - box.clientHeight / 2);
  }, [rows]);

  const toggle = (key: SortKey) => setSort((s) => (s.key === key ? { key, desc: !s.desc } : { key, desc: false }));
  // metric columns sort by rank (ascending = best first), the name column alphabetically
  const ariaSort = (key: SortKey) => (sort.key !== key ? "none" : sort.desc ? "descending" : "ascending");

  const subtitle = d?.status === "ok" && cat
    ? <>Among {cat.size} direct-growth funds in {cat.label} · as of {day(d.as_of)} (month-end NAVs){cat.excluded.length > 0 && <> · {cat.excluded.length} excluded</>}</>
    : "Rank and percentile within the fund's SEBI category, per metric";

  return (
    <Card title="Category rank" icon={<Trophy className="size-4" />} subtitle={subtitle}
      help="Computed by FinResearch from AMFI NAVs once a day: every direct-plan growth option in the same SEBI category, measured between the same month-end dates. Rank 1 is the best; the percentile is the share of the category the fund beats (ties count half). There is no blended score: pick the column that matters to you.">
      {r.error && <ErrorNote error={r.error} onRetry={r.reload} />}
      {!d && !r.error && <Skeleton className="h-[160px] w-full rounded-lg" />}
      {d && d.status === "not_computed" && <Callout tone="info" icon={<Info className="size-4" />}>{d.message}</Callout>}
      {d && (d.status === "not_ranked" || d.status === "unknown" || d.status === "excluded") && (
        <Callout tone="neutral" icon={<Info className="size-4" />} title={d.label ? `Not ranked in ${d.label}` : "Not ranked"}>
          {d.reason}{d.as_of && <> (ranking as of {day(d.as_of)})</>}.
        </Callout>
      )}
      {d?.status === "ok" && d.fund && cat && (
        <div className="space-y-4">
          {d.via && (
            <p className="text-xs text-muted">
              Ranked through the direct-plan growth option of this scheme (<Link className="text-brand hover:underline" href={`/funds/${d.via}`}>{d.via}</Link>): regular plans and IDCW options are not ranked separately. A regular plan returns less than this by its extra expense ratio.
            </p>
          )}
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-4 lg:grid-cols-7 [&>*]:min-w-0">
            {metrics.map((m) => {
              const rank = d.fund!.ranks[m.key];
              const of = cat.counts[m.key];
              const pct = d.fund!.percentiles[m.key];
              return (
                <Metric key={m.key} label={m.label} help={HELP[m.key]}
                  value={rank != null ? `${rank} / ${of}` : "—"}
                  sub={rank != null ? `${fmtRankValue(d.fund!, m.key)} · beats ${Math.round((pct ?? 0) * 100)}%`
                    : d.fund!.missing[m.key] ?? (cat.ranked[m.key] ? "not available" : "too few funds to rank")} />
              );
            })}
          </div>

          <Table label={`${cat.label}: category table`} className="!mx-0 max-h-[28rem] overflow-y-auto">
            <thead className="sticky top-0 bg-card">
              <tr>
                <th aria-sort={ariaSort("name")}>
                  <button type="button" onClick={() => toggle("name")} className="inline-flex items-center gap-1 uppercase hover:text-foreground">Fund <SortIcon on={sort.key === "name"} desc={sort.desc} /></button>
                </th>
                {metrics.map((m) => (
                  <th key={m.key} className="text-right" aria-sort={ariaSort(m.key)}>
                    <button type="button" title={HELP[m.key]} onClick={() => toggle(m.key)} className="inline-flex items-center gap-1 uppercase hover:text-foreground">
                      {METRIC_SHORT[m.key]} <SortIcon on={sort.key === m.key} desc={sort.desc} />
                    </button>
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((f) => (
                <tr key={f.code} ref={f.code === d.fund!.code ? mine : undefined} className={cx(f.code === d.fund!.code && "bg-brand-soft/60")}>
                  <td className="max-w-[16rem] truncate">
                    <Link href={`/funds/${f.code}`} className="hover:text-brand" title={f.name}>{f.name}</Link>
                  </td>
                  {metrics.map((m) => (
                    <td key={m.key} className="num text-right text-xs" title={f.missing[m.key] ?? undefined}>
                      {fmtRankValue(f, m.key)}
                      {f.ranks[m.key] != null && <span className="ml-1 text-[10px] text-muted">#{f.ranks[m.key]}</span>}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </Table>

          {cat.excluded.length > 0 && (
            <div>
              <button type="button" className="text-xs text-brand hover:underline" onClick={() => setShowExcluded((s) => !s)}>
                {showExcluded ? "Hide" : "Show"} {cat.excluded.length} fund{cat.excluded.length > 1 ? "s" : ""} left out
              </button>
              {showExcluded && (
                <ul className="mt-1 list-disc pl-4 text-[11px] text-muted">
                  {cat.excluded.map((x) => <li key={x.code}>{x.name}: {x.reason}</li>)}
                </ul>
              )}
            </div>
          )}

          <div className="space-y-1 text-[11px] text-muted">
            <p>
              <Badge tone="warn">Past returns</Badge>{" "}
              Funds with a shorter history are ranked only on the periods they cover (counts per column above). Sortino uses a minimum
              acceptable return of {pctOf(d.mar ?? null, 1)} a year ({pctOf((d.mar ?? 0) / 12, 3)} a month). TER {d.ter_day ? `as of ${day(d.ter_day)}` : "unavailable"}{d.ter_error ? ` (${d.ter_error})` : ""}.
              {cat.raw_labels.length > 1 && <> AMFI headings grouped here: {cat.raw_labels.join("; ")}.</>}
            </p>
            <ul className="list-disc pl-4">{(d.caveats ?? []).map((c) => <li key={c}>{c}</li>)}</ul>
            <p>Source: AMFI NAV history and NAVAll, AMFI TER file; ranks computed by FinResearch. Not investment advice.</p>
          </div>
        </div>
      )}
    </Card>
  );
}

function SortIcon({ on, desc }: { on: boolean; desc: boolean }) {
  if (!on) return <ArrowUpDown className="size-3 opacity-50" />;
  return desc ? <ArrowDown className="size-3" /> : <ArrowUp className="size-3" />;
}

/** "12 / 34 in Flexi Cap, 3y" for a portfolio row; the summary comes from one batch request for every fund held. */
export function RankBadge({ s }: { s: RankSummary | undefined }) {
  if (!s) return null;
  if (s.rank == null || s.of == null) {
    return s.status === "ok" || s.status === "excluded" || s.status === "not_ranked"
      ? <span title={`${s.reason ?? "not ranked"}${s.as_of ? ` (as of ${s.as_of})` : ""}`}><Badge tone="neutral">not ranked</Badge></span>
      : null;
  }
  const top = (s.percentile ?? 0) >= 0.75, bottom = (s.percentile ?? 1) < 0.25;
  return (
    <span title={`Rank among direct-growth funds of the category on ${METRIC_SHORT[s.metric]} return, as of ${s.as_of}${s.via ? " (via the direct plan)" : ""}. Past returns; ranks among surviving schemes.`}>
      <Badge tone={top ? "gain" : bottom ? "warn" : "neutral"}>{s.rank} / {s.of} in {s.label}, {METRIC_SHORT[s.metric]}</Badge>
    </span>
  );
}
